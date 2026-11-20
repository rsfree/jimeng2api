/* ============================================================================
   jimeng2api 控制台
   纯静态、无构建步骤：hash 路由 + 模板字符串渲染 + 事件委托。
   数据只来自 /panel/api/*，所有写操作显式提示后果。
   ========================================================================== */
(() => {
  'use strict';

  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
  const LS_KEY = 'j2a_key';
  const LS_THEME = 'j2a_theme';

  const S = {
    key: '', view: 'overview', loaded: false, busy: false,
    overview: null, accounts: null, tasks: null, models: null,
    keys: null, settings: null, usage: null, logs: null,
    tasksStatus: '', tasksQ: '', tasksPage: 0, tasksTotal: 0,
    logsTab: 'request', logsErrors: false, usageDays: 7,
    autoTimer: null, lastRefresh: 0, play: { model: '', size: '', count: 1, dry: null },
  };

  // ------------------------------------------------------------------ 工具
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const num = (v) => (v == null || v === '') ? '—' : Number(v).toLocaleString('zh-CN');
  const pad = (n) => String(n).padStart(2, '0');

  function fmtTime(ts, withSec = false) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const hm = pad(d.getHours()) + ':' + pad(d.getMinutes()) + (withSec ? ':' + pad(d.getSeconds()) : '');
    return sameDay ? hm : (pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + hm);
  }
  function rel(ts) {
    if (!ts) return '—';
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return s + ' 秒前';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  }
  function dur(a, b) {
    if (!a) return '—';
    const s = Math.max(0, (b || Math.floor(Date.now() / 1000)) - a);
    if (s < 60) return s + 's';
    return Math.floor(s / 60) + 'm' + pad(s % 60) + 's';
  }
  const pct = (v) => v == null ? '—' : Math.round(v * 100) + '%';
  function fmtDate(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  const TASK_STATE = {
    queued: ['排队中', 'mute'], submitting: ['提交中', 'warn'],
    running: ['生成中', 'warn'], succeeded: ['成功', 'ok'],
    failed: ['失败', 'bad'], canceled: ['已取消', 'mute'],
  };
  const taskPill = (st) => {
    const [label, tone] = TASK_STATE[st] || [st || '未知', 'mute'];
    const pulse = (st === 'running' || st === 'submitting' || st === 'queued') ? ' pulse' : '';
    return `<span class="pill ${tone}"><span class="dot${pulse}"></span>${label}</span>`;
  };
  const ACC_STATE = {
    ok: ['可用', 'ok'], cooling: ['冷却中', 'warn'], invalid: ['登录失效', 'bad'],
    disabled: ['已禁用', 'mute'], quota: ['积分不足', 'warn'], unknown: ['待检查', 'mute'],
  };
  const accPill = (state) => {
    const [label, tone] = ACC_STATE[state] || [state, 'mute'];
    return `<span class="pill ${tone}"><span class="dot"></span>${label}</span>`;
  };

  function toast(msg, kind = '') {
    const el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .25s'; }, 3600);
    setTimeout(() => el.remove(), 3950);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制', 'ok');
    } catch {
      toast('复制失败，请手动选择', 'bad');
    }
  }

  // -------------------------------------------------------------------- API
  async function api(path, opts = {}) {
    const headers = { 'Authorization': 'Bearer ' + S.key };
    if (opts.body) headers['Content-Type'] = 'application/json';
    const res = await fetch('/panel/api' + path, {
      method: opts.method || 'GET',
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok) {
      // 网关的错误信封是 {error:{code,message}}；直接 JSON.stringify 会把
      // "sessionid 已在池子里" 变成一串对象，运维看不出发生了什么。
      const msg = (data && (data.detail
        || (data.error && (data.error.message || data.error.code))
        || data.message)) || ('HTTP ' + res.status);
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------------- 登录
  function applyTheme() {
    const theme = localStorage.getItem(LS_THEME) || 'dark';
    document.documentElement.dataset.theme = theme;
  }
  function toggleTheme() {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem(LS_THEME, next);
    document.documentElement.dataset.theme = next;
  }

  function showGate(msg = '') {
    $('#app').hidden = true;
    $('#gate').hidden = false;
    $('#gateErr').textContent = msg;
    setTimeout(() => $('#gateKey').focus(), 30);
  }
  function hideGate() {
    $('#gate').hidden = true;
    $('#app').hidden = false;
  }

  async function login(key) {
    key = (key || '').trim();
    if (!key) { $('#gateErr').textContent = '请输入 Key'; return; }
    $('#gateErr').textContent = '';
    try {
      const res = await fetch('/panel/api/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || 'Key 无效');
      }
      S.key = key;
      localStorage.setItem(LS_KEY, key);
      hideGate();
      if (!location.hash) location.hash = '#overview';
      await refresh(true);
      startAuto();
    } catch (e) {
      $('#gateErr').textContent = e.message || '登录失败';
    }
  }

  function logout() {
    localStorage.removeItem(LS_KEY);
    S.key = '';
    stopAuto();
    showGate('');
  }

  // ------------------------------------------------------------------ 路由
  const ROUTES = {
    overview: { title: '概览', sub: '账号池、任务与上游状态的实时快照', render: renderOverview },
    accounts: { title: '账号池', sub: 'sessionid 即账号；积分与健康状态来自上游只读接口', render: renderAccounts },
    tasks: { title: '任务中心', sub: '受理只落库，协调器负责提交与轮询', render: renderTasks },
    usage: { title: '用量', sub: '按模型 / 账号 / 密钥聚合的积分与请求', render: renderUsage },
    logs: { title: '日志', sub: '对外请求与上游调用；失败原因按错误类型归类', render: renderLogs },
    models: { title: '模型与档位', sub: '能力表由 get_common_config 实时下发，快照仅作兜底', render: renderModels },
    keys: { title: '访问密钥', sub: '业务 Key 与管理员 Key 共用一张表；明文只出现一次', render: renderKeys },
    settings: { title: '设置', sub: '运行期参数即时生效；上游站点参数在环境变量里', render: renderSettings },
  };

  function currentRoute() {
    const name = (location.hash || '#overview').replace(/^#\/?/, '');
    return ROUTES[name] ? name : 'overview';
  }

  function setChrome() {
    const name = currentRoute();
    const r = ROUTES[name];
    $('#pageTitle').textContent = r.title;
    $('#pageSub').textContent = r.sub;
    $$('.nav-i a').forEach((a) => a.classList.toggle('on', a.dataset.view === name));
    $('#topActions').innerHTML = topActions(name);
    const sel = $('#taskStatusFilter');
    if (sel) sel.value = S.tasksStatus;
    const auto = $('#logAuto');
    if (auto) auto.checked = true;
  }

  function topActions(name) {
    if (name === 'accounts') {
      return `<button class="btn" data-act="probe-all">批量测试登录</button>
              <button class="btn" data-act="qr-login">扫码登录</button>
              <button class="btn" data-act="remote-browser">远程浏览器</button>
              <button class="btn primary" data-act="add-account">添加账号</button>`;
    }
    if (name === 'models') {
      return `<button class="btn" data-act="refresh-models">重新读取能力表</button>`;
    }
    if (name === 'keys') {
      return `<button class="btn primary" data-act="new-key">创建密钥</button>`;
    }
    if (name === 'tasks') {
      return `<select class="select" id="taskStatusFilter">
        <option value="">全部状态</option>
        <option value="queued">排队中</option><option value="running">生成中</option>
        <option value="succeeded">成功</option><option value="failed">失败</option>
      </select>`;
    }
    if (name === 'logs') {
      return `<label class="check"><input type="checkbox" id="logAuto" checked> 自动刷新</label>`;
    }
    return '';
  }

  async function refresh(force = false) {
    if (!S.key) return;
    const name = currentRoute();
    if (S.busy && !force) return;
    S.busy = true;
    try {
      if (name === 'overview') {
        S.overview = await api('/overview');
        if (!S.models) { try { S.models = await api('/models'); } catch { S.models = null; } }
        $('#navAccCount').textContent = S.overview.accounts.total || '';
        $('#navTaskCount').textContent = S.overview.tasks.total || '';
      } else if (name === 'accounts') {
        S.accounts = await api('/accounts');
        $('#navAccCount').textContent = S.accounts.stats.total || '';
      } else if (name === 'tasks') {
        const q = new URLSearchParams({
          status: S.tasksStatus, q: S.tasksQ,
          limit: '50', offset: String(S.tasksPage * 50),
        });
        S.tasks = await api('/tasks?' + q.toString());
        S.tasksTotal = S.tasks.total;
        $('#navTaskCount').textContent = S.tasks.total || '';
      } else if (name === 'usage') {
        S.usage = await api('/usage?days=' + S.usageDays);
      } else if (name === 'logs') {
        S.logs = await api('/logs/' + (S.logsTab === 'request' ? 'requests' : 'upstream')
          + '?limit=100' + (S.logsErrors ? '&only_error=true' : ''));
      } else if (name === 'models') {
        S.models = await api('/models');
        ensurePlayDefaults();
      } else if (name === 'keys') {
        S.keys = await api('/keys');
      } else if (name === 'settings') {
        S.settings = await api('/settings');
      }
      S.loaded = true;
      renderView();
    } catch (e) {
      if (e.status === 401 || e.status === 403) { logout(); return; }
      toast(e.message || '加载失败', 'bad');
    } finally {
      S.busy = false;
      S.lastRefresh = Date.now();
    }
  }

  function renderView() {
    const name = currentRoute();
    ROUTES[name].render();
    setChrome();
  }

  function startAuto() {
    stopAuto();
    S.autoTimer = setInterval(() => {
      if (document.hidden) return;
      if (['overview', 'accounts', 'tasks', 'logs'].includes(currentRoute())) refresh(false);
    }, 5000);
  }
  function stopAuto() {
    if (S.autoTimer) clearInterval(S.autoTimer);
    S.autoTimer = null;
  }

  // --------------------------------------------------------------- 组件
  function stats(items) {
    return `<div class="stats">${items.map((s) => `
      <div class="stat ${s.tone || ''}"><div class="v">${s.value}</div><div class="k">${s.label}</div></div>`).join('')}</div>`;
  }

  function empty(title, sub) {
    return `<div class="empty"><div class="big">${title}</div>${sub || ''}</div>`;
  }

  function areaChart(rows) {
    const w = 640, h = 168, padL = 30, padR = 26, padB = 22, padT = 12;
    const max = Math.max(1, ...rows.map((r) => r.total));
    const stepX = rows.length > 1 ? (w - padL - padR) / (rows.length - 1) : 0;
    const y = (v) => padT + (h - padT - padB) * (1 - v / max);
    const pts = rows.map((r, i) => [padL + i * stepX, y(r.total)]);
    const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const area = line + ` L ${pts[pts.length - 1][0].toFixed(1)} ${h - padB} L ${padL} ${h - padB} Z`;
    const grid = [0, 0.5, 1].map((f) => {
      const yy = (padT + (h - padT - padB) * f).toFixed(1);
      return `<line class="grid" x1="${padL}" y1="${yy}" x2="${w - padR}" y2="${yy}"></line>`;
    }).join('');
    return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img">
      ${grid}
      <path class="area" d="${area}"></path>
      <path class="line" d="${line}"></path>
      ${pts.map((p, i) => {
        const anchor = i === 0 ? 'start' : (i === pts.length - 1 ? 'end' : 'middle');
        return `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.6" fill="var(--accent)"></circle>
        <text class="axis" x="${p[0].toFixed(1)}" y="${h - 6}" text-anchor="${anchor}">${rows[i].date}</text>`;
      }).join('')}
    </svg>`;
  }

  function dayBars(rows) {
    const w = 640, h = 180, padB = 24, padT = 10;
    const max = Math.max(1, ...rows.map((r) => r.ok + r.failed));
    const bw = (w - 20) / rows.length;
    return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img">
      ${rows.map((r, i) => {
        const total = r.ok + r.failed;
        const bh = (h - padT - padB) * (total / max);
        const x = 10 + i * bw + bw * 0.18;
        const okH = total ? bh * (r.ok / total) : 0;
        return `<rect class="bar ok" x="${x.toFixed(1)}" y="${(h - padB - okH).toFixed(1)}"
          width="${(bw * 0.64).toFixed(1)}" height="${okH.toFixed(1)}" rx="2"></rect>
          <rect class="bar bad" x="${x.toFixed(1)}" y="${(h - padB - bh).toFixed(1)}"
          width="${(bw * 0.64).toFixed(1)}" height="${Math.max(0, bh - okH).toFixed(1)}" rx="2"></rect>
          <text class="axis" x="${(x + bw * 0.32).toFixed(1)}" y="${h - 6}" text-anchor="middle">${r.date}</text>`;
      }).join('')}
    </svg>`;
  }

  function hbars(items, valueKey = 'n') {
    if (!items.length) return empty('暂无数据');
    const max = Math.max(1, ...items.map((x) => Number(x[valueKey]) || 0));
    return `<div class="hbars">${items.map((x) => `
      <div class="hbar">
        <span class="label" title="${esc(x.label)}">${esc(x.label)}</span>
        <span class="track"><span class="fill" style="width:${Math.max(2, (Number(x[valueKey]) || 0) / max * 100)}%"></span></span>
        <span class="val">${num(x[valueKey])}${x.extra ? ' · ' + esc(x.extra) : ''}</span>
      </div>`).join('')}</div>`;
  }

  function taskTable(items) {
    if (!items.length) return empty('还没有任务', '在「模型与档位」里可以先用 dry-run 构造草稿，零计费。');
    return `<div class="table-wrap"><table class="tbl">
      <thead><tr><th>时间</th><th>状态</th><th>模型</th><th>尺寸 × 张数</th>
      <th>提示词</th><th class="num">积分</th><th class="num">耗时</th><th></th></tr></thead>
      <tbody>${items.map((t) => `
        <tr>
          <td class="nowrap num">${fmtTime(t.created_at)}</td>
          <td class="nowrap">${taskPill(t.status)}</td>
          <td class="nowrap"><div class="cell-2"><span>${esc((S.models && modelName(t.model)) || t.model)}</span>
            ${(S.models && modelName(t.model) !== t.model) ? `<span class="s">${esc(t.model)}</span>` : ''}</div></td>
          <td class="nowrap num">${esc(t.size)} × ${t.count}</td>
          <td><div class="prompt-cell" title="${esc(t.prompt)}">${esc(t.prompt) || '<span class="muted">（无提示词）</span>'}</div></td>
          <td class="num">${t.charged_credits != null ? num(t.charged_credits) : (t.forecast_credits != null ? '≈' + num(t.forecast_credits) : '—')}</td>
          <td class="num">${dur(t.started_at, t.finished_at)}</td>
          <td><button class="btn xs" data-act="task-open" data-id="${t.id}">详情</button></td>
        </tr>`).join('')}</tbody></table></div>`;
  }

  function modelName(key) {
    if (!S.models) return key;
    const m = (S.models.models || []).find((x) => x.key === key);
    return m ? m.name : key;
  }

  // --------------------------------------------------------------- 概览
  function renderOverview() {
    const d = S.overview;
    if (!d) { $('#view').innerHTML = empty('加载中…'); return; }
    const a = d.accounts, t = d.today;
    const alerts = (d.alerts || []).map((x) => `<span>${esc(x.text)}</span>`).join('<span class="muted">·</span>');
    $('#view').innerHTML = `
      ${d.alerts && d.alerts.length ? `<div class="alertbar ${d.alerts.some((x) => x.level === 'bad') ? 'bad' : ''}">
        <span class="dot pulse"></span><span class="text">${alerts}</span></div>` : ''}
      ${stats([
        { label: '账号总数', value: num(a.total) },
        { label: '可用', value: num(a.ok), tone: 'good' },
        { label: '冷却中', value: num(a.cooling), tone: a.cooling ? 'warn' : '' },
        { label: '登录失效', value: num(a.invalid), tone: a.invalid ? 'bad' : '' },
        { label: '可用积分', value: num(a.credits), tone: 'accent' },
        { label: '在途任务', value: num(a.inflight) },
        { label: '今日任务', value: num(t.total) },
        { label: '今日成功率', value: pct(t.success_rate) },
      ])}
      <div class="split">
        <div class="box">
          <header><h3>近 7 天任务<span class="hint">按创建时间</span></h3>
            <span class="grow"></span><span class="note">${num(d.trend.reduce((s, r) => s + r.total, 0))} 条</span></header>
          <div class="pad">${areaChart(d.trend)}</div>
        </div>
        <div class="box">
          <header><h3>运行状态</h3><span class="grow"></span>
            <span class="pill ${d.coordinator.running ? 'ok' : 'bad'}">
              <span class="dot ${d.coordinator.running ? 'pulse' : ''}"></span>
              ${d.coordinator.running ? '协调器运行中' : '协调器未运行'}</span></header>
          <div class="pad">
            <dl class="kv">
              <dt>协调器</dt><dd>${d.coordinator.running ? '运行中 · ' + num(d.coordinator.ticks) + ' ticks' : '已停止'}</dd>
              <dt>今日成功/失败</dt><dd>${num(t.ok)} / ${num(t.failed)}</dd>
              <dt>队列</dt><dd>${num(d.tasks.queued || 0)} 排队 · ${num(d.tasks.running || 0)} 生成中</dd>
              <dt>能力表</dt><dd>${num(d.models.count)} 个模型 · ${esc(d.models.source)}</dd>
              <dt>上游</dt><dd>${esc(d.upstream.base)} · da ${esc(d.upstream.da_version)}</dd>
              <dt>今日积分</dt><dd>${num(t.credits)}</dd>
            </dl>
            <div class="divider"></div>
            <div class="row">
              <button class="btn xs" data-act="add-account">添加账号</button>
              <button class="btn xs" data-act="goto-models">试玩 / 构造草稿</button>
              <button class="btn xs" data-act="refresh">刷新</button>
            </div>
          </div>
        </div>
      </div>
      <div class="box">
        <header><h3>最近任务</h3><span class="grow"></span>
          <button class="btn xs" data-act="goto-tasks">全部任务</button></header>
        ${taskTable(d.recent)}
      </div>`;
  }

  // --------------------------------------------------------------- 账号池
  function renderAccounts() {
    const d = S.accounts;
    if (!d) { $('#view').innerHTML = empty('加载中…'); return; }
    const s = d.stats;
    $('#view').innerHTML = `
      ${stats([
        { label: '账号总数', value: num(s.total) },
        { label: '可用', value: num(s.ok), tone: 'good' },
        { label: '冷却中', value: num(s.cooling), tone: s.cooling ? 'warn' : '' },
        { label: '登录失效', value: num(s.invalid), tone: s.invalid ? 'bad' : '' },
        { label: '已禁用', value: num(s.disabled) },
        { label: '池子积分', value: num(s.credits), tone: 'accent' },
        { label: '在途', value: num(s.inflight) },
      ])}
      <div class="box">
        <header><h3>账号池<span class="hint">sessionid 只显示尾 6 位；换号请删除后重加</span></h3>
          <span class="grow"></span>
          <span class="note">${num(d.items.length)} 个</span></header>
        ${d.items.length ? `<div class="table-wrap"><table class="tbl">
          <thead><tr><th>账号</th><th>状态</th><th class="num">积分</th>
          <th class="num">成功 / 失败</th><th class="num">在途</th><th>最近检查</th><th>备注</th><th></th></tr></thead>
          <tbody>${d.items.map((x) => `
            <tr>
              <td><div class="cell-2"><span class="strong">${esc(x.name || x.uid || '未命名账号')}</span>
                <span class="s">${esc(x.session_hint)}${x.member_level ? ' · ' + esc(x.member_level) : ''}${x.expires_at ? ' · ' + (x.expires_at * 1000 < Date.now() ? '<em class="neg">已到期</em>' : '到期 ' + fmtDate(x.expires_at)) : ''}</span></div></td>
              <td><div class="status-stack">${accPill(x.state)}
                ${x.cooldown_left ? `<span class="s">剩 ${Math.ceil(x.cooldown_left / 60)} 分钟</span>` : ''}
                ${x.last_error ? `<span class="s" title="${esc(x.last_error)}">${esc(x.last_error.slice(0, 40))}</span>` : ''}</div></td>
              <td class="num">${num(x.credits)}</td>
              <td class="num">${num(x.success_count)} / <span class="${x.fail_count ? 'neg' : ''}">${num(x.fail_count)}</span></td>
              <td class="num">${num(x.inflight)}</td>
              <td class="nowrap muted">${x.last_checked ? rel(x.last_checked) : '从未'}</td>
              <td class="muted">${esc(x.remark) || '—'}</td>
              <td class="nowrap">
                <button class="btn xs" data-act="probe-account" data-id="${x.id}">测试登录</button>
                <button class="btn xs" data-act="toggle-account" data-id="${x.id}" data-on="${x.enabled ? 1 : 0}">${x.enabled ? '禁用' : '启用'}</button>
                ${x.cooldown_left ? `<button class="btn xs" data-act="clear-cooldown" data-id="${x.id}">解冷却</button>` : ''}
                <button class="btn xs ghost danger" data-act="del-account" data-id="${x.id}">删除</button>
              </td>
            </tr>`).join('')}</tbody></table></div>`
        : empty('账号池是空的', '点右上角「添加账号」，粘贴浏览器 cookie 里的 sessionid。')}
      </div>`;
  }

  // --------------------------------------------------------------- 任务
  function renderTasks() {
    const d = S.tasks;
    if (!d) { $('#view').innerHTML = empty('加载中…'); return; }
    const pages = Math.max(1, Math.ceil(d.total / 50));
    $('#view').innerHTML = `
      <div class="box">
        <header><h3>任务列表<span class="hint">共 ${num(d.total)} 条</span></h3>
          <span class="grow"></span>
          <input class="input" id="taskSearch" placeholder="搜 task id / 提示词 / 模型"
            value="${esc(S.tasksQ)}" style="width:260px">
          <button class="btn xs" data-act="task-search">搜索</button>
          ${S.tasksQ || S.tasksStatus ? `<button class="btn xs ghost" data-act="task-clear">清空筛选</button>` : ''}
        </header>
        ${taskTable(d.items)}
        <div class="foot">
          <span class="note">第 ${S.tasksPage + 1} / ${pages} 页</span>
          <span class="grow"></span>
          <button class="btn xs" data-act="task-prev" ${S.tasksPage <= 0 ? 'disabled' : ''}>上一页</button>
          <button class="btn xs" data-act="task-next" ${S.tasksPage + 1 >= pages ? 'disabled' : ''}>下一页</button>
        </div>
      </div>`;
    const sel = $('#taskStatusFilter');
    if (sel) sel.value = S.tasksStatus;
  }

  // --------------------------------------------------------------- 用量
  function renderUsage() {
    const d = S.usage;
    if (!d) { $('#view').innerHTML = empty('加载中…'); return; }
    const t = d.totals;
    const models = (d.by_model || []).filter((x) => x.model).map((x) => ({
      label: (S.models && modelName(x.model)) || x.model,
      n: x.n, extra: x.ok + ' 成功 · ' + x.credits + ' 分',
    }));
    const accounts = (d.by_account || []).map((x) => ({
      label: x.name || x.uid || ('账号 #' + (x.account_id || '—')),
      n: x.n, extra: x.ok + ' 成功 · ' + x.credits + ' 分',
    }));
    $('#view').innerHTML = `
      <div class="row" style="margin-bottom:14px">
        <div class="btn-group">
          ${[7, 14, 30].map((n) => `<button class="btn ${S.usageDays === n ? 'on' : ''}" data-act="usage-days" data-id="${n}">近 ${n} 天</button>`).join('')}
        </div>
        <span class="grow"></span>
        <button class="btn xs" data-act="refresh">刷新</button>
      </div>
      ${stats([
        { label: '请求数', value: num(t.requests) },
        { label: '成功', value: num(t.ok), tone: 'good' },
        { label: '失败', value: num(t.failed), tone: t.failed ? 'bad' : '' },
        { label: '积分（实际优先）', value: num(t.credits), tone: 'accent' },
      ])}
      <div class="box"><header><h3>每日任务</h3><span class="grow"></span>
        <span class="note">绿=成功 红=失败</span></header>
        <div class="pad">${dayBars(d.trend)}</div></div>
      <div class="split">
        <div class="box"><header><h3>按模型</h3></header><div class="pad">${hbars(models)}</div></div>
        <div class="box"><header><h3>按账号</h3></header><div class="pad">${hbars(accounts)}</div></div>
      </div>
      <div class="box"><header><h3>按密钥</h3><span class="hint">额度扣减与 tasks.charged_credits 同源</span></header>
        ${(d.by_key || []).length ? `<div class="table-wrap"><table class="tbl">
          <thead><tr><th>密钥</th><th class="num">请求</th><th class="num">积分</th></tr></thead>
          <tbody>${d.by_key.map((x) => `<tr><td>${esc(x.name || ('Key #' + (x.api_key_id || '—')))}</td>
            <td class="num">${num(x.n)}</td><td class="num">${num(x.credits)}</td></tr>`).join('')}
          </tbody></table></div>` : empty('暂无数据')}
      </div>`;
  }

  // --------------------------------------------------------------- 日志
  function renderLogs() {
    const d = S.logs;
    const isReq = S.logsTab === 'request';
    const table = !d ? empty('加载中…') : (isReq ? requestLogTable(d.items) : upstreamLogTable(d.items));
    $('#view').innerHTML = `
      <div class="box">
        <header>
          <div class="btn-group">
            <button class="btn ${isReq ? 'on' : ''}" data-act="log-tab" data-id="request">请求日志</button>
            <button class="btn ${!isReq ? 'on' : ''}" data-act="log-tab" data-id="upstream">上游日志</button>
          </div>
          ${!isReq ? `<label class="check"><input type="checkbox" id="logErrOnly" ${S.logsErrors ? 'checked' : ''}> 只看失败</label>` : ''}
          <span class="grow"></span>
          <span class="note">共 ${d ? num(d.total) : '—'} 条</span>
          <button class="btn xs" data-act="refresh">刷新</button>
        </header>
        ${table}
      </div>`;
    const auto = $('#logAuto');
    if (auto) auto.checked = true;
  }

  function requestLogTable(items) {
    if (!items || !items.length) return empty('暂无请求记录', '调用一次 /v1/images/generations 就会出现。');
    return `<div class="table-wrap"><table class="tbl">
      <thead><tr><th>时间</th><th>方法</th><th>路径</th><th class="num">状态</th>
      <th class="num">耗时</th><th>来源 IP</th><th>User-Agent</th></tr></thead>
      <tbody>${items.map((x) => `<tr>
        <td class="nowrap num">${fmtTime(x.ts, true)}</td>
        <td class="nowrap">${esc(x.method)}</td>
        <td class="nowrap"><code>${esc(x.path)}</code></td>
        <td class="num ${x.status >= 400 ? 'neg' : 'pos'}">${x.status}</td>
        <td class="num">${x.latency_ms}ms</td>
        <td class="nowrap muted">${esc(x.ip) || '—'}</td>
        <td class="muted"><div class="prompt-cell" title="${esc(x.ua)}">${esc(x.ua) || '—'}</div></td>
      </tr>`).join('')}</tbody></table></div>`;
  }

  function upstreamLogTable(items) {
    if (!items || !items.length) return empty('暂无上游记录', '协调器提交任务后才有上游调用。');
    return `<div class="table-wrap"><table class="tbl">
      <thead><tr><th>时间</th><th>账号</th><th>路径</th><th class="num">HTTP</th>
      <th class="num">ret</th><th>类型</th><th class="num">耗时</th><th>错误</th></tr></thead>
      <tbody>${items.map((x) => `<tr>
        <td class="nowrap num">${fmtTime(x.ts, true)}</td>
        <td class="num">${x.account_id ? '#' + x.account_id : '—'}</td>
        <td class="nowrap"><code>${esc(x.path)}</code></td>
        <td class="num">${x.http_status || '—'}</td>
        <td class="num">${esc(x.ret) || '—'}</td>
        <td>${x.kind ? `<span class="tag ${x.kind === 'auth' || x.kind === 'risk' ? 'bad' : x.kind === 'rate' ? 'warn' : ''}">${esc(x.kind)}</span>` : '—'}</td>
        <td class="num">${x.latency_ms}ms</td>
        <td class="muted"><div class="prompt-cell" title="${esc(x.error)}">${esc(x.error) || '—'}</div></td>
      </tr>`).join('')}</tbody></table></div>`;
  }

  // --------------------------------------------------------------- 模型
  function ensurePlayDefaults() {
    if (!S.models) return;
    const list = S.models.models || [];
    if (!list.length) return;
    if (!S.play.model) S.play.model = S.models.default_model || list[0].key;
    const m = list.find((x) => x.key === S.play.model) || list[0];
    if (!S.play.size) {
      const sizes = m.sizes || [];
      const pref = sizes.find((s) => s.width === 2048 && s.height === 2048) || sizes[0];
      if (pref) { S.play.size = pref.width + 'x' + pref.height; S.play.resolution = pref.resolution; }
    }
    if (!S.play.count) S.play.count = m.default_count || 1;
  }

  // --------------------------------------------- 模型重定向（对外名 → 内部）
  function renderRedirects(d) {
    const rows = d.redirects || [];
    const targets = d.redirect_targets || [];
    const on = rows.filter((r) => r.enabled && r.ok).length;
    const bad = rows.filter((r) => !r.ok);
    const opts = targets.map((t) =>
      `<option value="${esc(t.value)}">${esc(t.label)}</option>`).join('');
    const table = rows.length ? `
      <div class="table-wrap"><table class="tbl">
        <thead><tr><th>对外模型名</th><th>落点</th><th>状态</th><th>备注</th><th></th></tr></thead>
        <tbody>${rows.map((r) => {
          const shown = r.public || r.target;
          // 落点下面那行只放**与落点不同**的信息：配置值 / 解析链 / 上游 key。
          // 全部相同就留空 —— 重复两遍同一串字符只会让人以为看错了列。
          const detail = (r.target !== shown)
            ? r.target + ((r.resolved && r.resolved !== r.target) ? ' → ' + r.resolved : '')
            : ((r.kind === 'model' && r.key && r.key !== shown) ? r.key : '');
          return `<tr>
          <td><div class="cell-2"><code class="strong">${esc(r.alias)}</code>
            <span class="s">${r.kind === 'postedit' ? '后编辑工具' : r.kind === 'model' ? '生成模型' : '—'}</span></div></td>
          <td><div class="cell-2"><span>${esc(shown)}</span>
            <span class="s">${esc(detail) || '—'}</span></div></td>
          <td class="nowrap">${!r.enabled ? '<span class="tag mute">已停用</span>'
            : r.ok ? '<span class="tag ok">生效</span>'
              : '<span class="tag bad">目标失效</span>'}</td>
          <td class="muted">${esc(r.note) || '—'}</td>
          <td class="nowrap">
            <button class="btn xs" data-act="redirect-toggle" data-id="${r.id}" data-on="${r.enabled ? 1 : 0}">${r.enabled ? '停用' : '启用'}</button>
            <button class="btn xs ghost danger" data-act="redirect-del" data-id="${r.id}" data-alias="${esc(r.alias)}">删除</button>
          </td></tr>`;
        }).join('')}</tbody></table></div>`
      : `<div class="hint-card">尚未配置。添加后，调用方可以直接用它熟悉的官方模型名请求，由网关映射到本网关模型。</div>`;
    return `
      <div class="box">
        <header><h3>模型重定向<span class="hint">调用方模型名 → 本网关模型 · 大小写不敏感</span></h3>
          <span class="grow"></span><span class="note">${rows.length} 条 · 生效 ${on}</span>
          <button class="btn xs" data-act="redirect-import">批量导入</button></header>
        <div class="pad">
          <div class="row" style="margin-bottom:10px">
            <input class="input" id="redirAlias" style="flex:1;min-width:240px" placeholder="对外模型名，如 gpt-image-1">
            <span class="note">→</span>
            <select class="select" id="redirTarget" style="min-width:260px">${opts}</select>
            <input class="input" id="redirNote" style="width:150px" placeholder="备注（可选）">
            <button class="btn primary" data-act="redirect-add">添加</button>
          </div>
          <div class="row" style="margin-bottom:12px">
            <span class="note">官方命名示例：</span>
            <button class="btn xs ghost" data-act="redirect-fill" data-id="doubao-seedream-4-0-250828">火山方舟</button>
            <button class="btn xs ghost" data-act="redirect-fill" data-id="gpt-image-1">OpenAI</button>
            <button class="btn xs ghost" data-act="redirect-fill" data-id="imagen-4.0-generate-001">Google</button>
            <span class="note">点一下填入输入框，落点按需选择</span>
          </div>
          ${table}
          ${bad.length ? `<div class="hint-card neg" style="margin-top:10px">${bad.map((r) =>
            `<code>${esc(r.alias)}</code>：${esc(r.error)}`).join('<br>')}</div>` : ''}
        </div>
      </div>`;
  }

  function renderModels() {
    const d = S.models;
    if (!d) { $('#view').innerHTML = empty('加载中…'); return; }
    ensurePlayDefaults();
    const cur = (d.models || []).find((x) => x.key === S.play.model) || (d.models || [])[0];
    const sizeOpts = cur ? (cur.sizes || []).map((s) =>
      `<option value="${s.width}x${s.height}" data-res="${s.resolution}" ${S.play.size === s.width + 'x' + s.height ? 'selected' : ''}>
        ${esc(s.label)} · ${esc(s.resolution)} · ${ratioLabel(s.ratio)}</option>`).join('') : '';
    const countOpts = cur ? (cur.counts || [cur.default_count]).map((n) =>
      `<option value="${n}" ${Number(S.play.count) === Number(n) ? 'selected' : ''}>${n} 张</option>`).join('') : '';
    const price = cur ? priceLine(cur, S.play.resolution || (cur.sizes[0] && cur.sizes[0].resolution)) : '—';
    const ageH = d.captured_at ? (Date.now() / 1000 - d.captured_at) / 3600 : 999;
    const fresh = d.live && ageH < 2;
    const srcBadge = d.live ? (fresh ? '实时' : '缓存') : '快照';

    $('#view').innerHTML = `
      <div class="box">
        <header><h3>能力表<span class="hint">${esc(d.source)}</span></h3>
          <span class="grow"></span>
          <span class="pill ${fresh ? 'ok' : 'warn'}"><span class="dot"></span>${srcBadge}</span>
          <span class="note">${d.captured_at ? fmtTime(d.captured_at, true) : '—'}</span></header>
        <div class="table-wrap"><table class="tbl">
          <thead><tr><th>模型</th><th>model_req_key</th><th>分辨率</th><th class="num">张数</th>
          <th class="num">单价（t2i / i2i）</th><th class="num">输入图上限</th></tr></thead>
          <tbody>${(d.models || []).map((m) => `
            <tr>
              <td><div class="cell-2"><span class="strong">${esc(m.name)} ${m.new ? '<span class="tag accent">NEW</span>' : ''}${m.key === d.default_model ? '<span class="tag ok">默认</span>' : ''}</span>
                <span class="s" title="${esc(m.tip)}">${esc((m.tip || '').slice(0, 42)) || '—'}</span></div></td>
              <td class="nowrap"><code>${esc(m.key)}</code></td>
              <td class="nowrap">${(m.resolutions ? Object.keys(m.resolutions) : []).map((r) => `<span class="tag">${esc(r)}</span>`).join(' ')}</td>
              <td class="num">${(m.counts || []).length ? esc((m.counts || []).join('/')) : '—'}</td>
              <td class="num">${priceCell(m)}</td>
              <td class="num">${m.max_images || '—'}</td>
            </tr>`).join('')}</tbody></table></div>
      </div>

      ${renderRedirects(d)}

      ${(d.tools && d.tools.length) ? `
      <div class="box">
        <header><h3>后编辑工具<span class="hint">同一端点，靠 model 选能力</span></h3>
          <span class="grow"></span>
          <span class="note">${d.tools.length} 项 · 面板可见内部标识，客户端不可见</span></header>
        <div class="table-wrap"><table class="tbl">
          <thead><tr><th>模型</th><th>形态</th><th>ability_code</th><th class="num">gen_type</th>
          <th>输入</th><th>档位 / 比例</th><th class="num">mask</th></tr></thead>
          <tbody>${d.tools.map((t) => `
            <tr>
              <td><div class="cell-2"><span class="strong">${esc(t.name)}</span>
                <span class="s"><code>${esc(t.id)}</code></span></div></td>
              <td class="nowrap"><span class="tag">${esc(t.shape)}</span></td>
              <td class="nowrap"><code>${esc(t.ability_code || t.ability)}</code></td>
              <td class="num">${t.gen_type == null ? '—' : esc(String(t.gen_type))}</td>
              <td class="nowrap">${esc(t.input)}</td>
              <td class="nowrap">${((t.resolutions || []).map((r) => `<span class="tag">${esc(r)}</span>`).join(' ')
                + ((t.ratios || []).map((r) => `<span class="tag accent">${esc(r)}</span>`).join(' '))) || '—'}</td>
              <td class="num">${t.needs_mask ? '要' : '—'}</td>
            </tr>`).join('')}</tbody></table></div>
        ${(d.unsupported && Object.keys(d.unsupported).length)
          ? `<div class="pad note">未接入：${Object.keys(d.unsupported).map(esc).join('、')}</div>`
          : '<div class="pad note">8 项能力全部接入</div>'}
      </div>` : ''}

      ${(d.videos && d.videos.length) ? `
      <div class="box">
        <header><h3>视频模型<span class="hint">第三套草稿：video_base_component</span></h3>
          <span class="grow"></span>
          <span class="note">${d.videos.length} 项 · ≈6 积分/秒（720p mini）· 单次约 5 分钟</span></header>
        <div class="table-wrap"><table class="tbl">
          <thead><tr><th>模型</th><th>model_req_key</th><th>时长档</th><th>分辨率</th><th>别名</th></tr></thead>
          <tbody>${d.videos.map((v) => `
            <tr>
              <td><div class="cell-2"><span class="strong">${esc(v.name)}</span>
                <span class="s"><code>${esc(v.id)}</code></span></div></td>
              <td class="nowrap"><code>${esc(v.model_req_key)}</code></td>
              <td class="nowrap">${(v.durations || []).map((x) => `<span class="tag">${esc(String(x))}s</span>`).join(' ') || '—'}</td>
              <td class="nowrap">${(v.resolutions || []).map((x) => `<span class="tag">${esc(x)}</span>`).join(' ') || '—'}</td>
              <td class="nowrap">${(v.aliases || []).map((x) => `<span class="tag">${esc(x)}</span>`).join(' ') || '—'}</td>
            </tr>`).join('')}</tbody></table></div>
      </div>` : ''}

      <div class="box">
        <header><h3>试玩 / 构造草稿<span class="hint">默认 dry-run：不请求上游、不计费</span></h3>
          <span class="grow"></span><span class="note">${esc(price)}</span></header>
        <div class="pad">
          <div class="grid2">
            <div class="field"><label>模型</label>
              <select class="select" id="playModel">
                ${(d.models || []).map((m) => `<option value="${esc(m.key)}" ${m.key === S.play.model ? 'selected' : ''}>${esc(m.name)} · ${esc(m.key)}</option>`).join('')}
              </select></div>
            <div class="field"><label>尺寸（比例会吸附到上游槽位）</label>
              <select class="select" id="playSize">${sizeOpts}</select></div>
            <div class="field"><label>张数</label>
              <select class="select" id="playCount">${countOpts}</select></div>
            <div class="field"><label>垫图（可选，URL / data URI / tos uri）</label>
              <input class="input" id="playImage" placeholder="https://… 留空 = 文生图"></div>
          </div>
          <div class="field"><label>提示词</label>
            <textarea class="textarea" id="playPrompt" placeholder="描述你想生成的画面 / 要怎么改这张图">一只橘猫趴在窗台上，午后逆光，胶片质感</textarea></div>
          <div class="row">
            <button class="btn" data-act="play-dry">构造草稿（免费）</button>
            <label class="check"><input type="checkbox" id="playConfirm"> 我确认这是计费动作</label>
            <button class="btn primary" data-act="play-real" id="playRealBtn" disabled>真实提交</button>
            <span class="grow"></span>
            <span class="note" id="playEstimate"></span>
          </div>
          ${S.play.dry ? `<div class="divider"></div>
            <div class="row" style="margin-bottom:8px">
              <span class="tag ${S.play.dry.dry_run ? 'ok' : 'accent'}">${S.play.dry.dry_run ? 'dry-run · 未发送' : '已受理 · ' + esc(S.play.dry.task_id || '')}</span>
              ${(S.play.dry.degradations || []).map((x) => `<span class="tag warn">${esc(x)}</span>`).join('')}
              ${S.play.dry.estimate_credits != null ? `<span class="tag">预计 ${S.play.dry.estimate_credits} 积分</span>` : ''}
            </div>
            <pre class="code">${esc(S.play.dry.draft || JSON.stringify(S.play.dry, null, 2))}</pre>` : ''}
        </div>
      </div>`;
    const est = $('#playEstimate');
    if (est) est.textContent = price;
  }

  const ratioLabel = (r) => ({ 1: '1:1', 2: '3:4', 3: '16:9', 4: '4:3', 5: '9:16', 6: '2:3', 7: '3:2', 8: '21:9' }[r] || (r || '—'));

  function priceCell(m) {
    const t2i = (m.prices && m.prices.t2i) || {};
    const i2i = (m.prices && m.prices.i2i) || {};
    const row = (label, t) => {
      const keys = Object.keys(t).filter((k) => k !== 'default');
      if (!keys.length) return `<span class="price-row"><span class="pl">${label}</span><span class="tag">${t.default != null ? t.default : '—'}</span></span>`;
      return `<span class="price-row"><span class="pl">${label}</span>${keys.map((k) =>
        `<span class="tag">${esc(k)} ${t[k]}</span>`).join('')}</span>`;
    };
    return `<div class="price-cell">${row('t2i', t2i)}${row('i2i', i2i)}</div>`;
  }

  function priceLine(m, resolution) {
    const t = (m.prices && m.prices.t2i) || {};
    const i = (m.prices && m.prices.i2i) || {};
    const a = t[resolution] != null ? t[resolution] : t.default;
    const b = i[resolution] != null ? i[resolution] : i.default;
    return `${m.name} · ${resolution || '—'} · t2i ${a != null ? a : '—'} / i2i ${b != null ? b : '—'} 积分/张`;
  }

  // --------------------------------------------------------------- 密钥
  function renderKeys() {
    const d = S.keys;
    if (!d) { $('#view').innerHTML = empty('加载中…'); return; }
    $('#view').innerHTML = `
      <div class="box">
        <header><h3>密钥<span class="hint">明文只在创建时返回一次</span></h3>
          <span class="grow"></span><span class="note">${d.items.length} 个</span></header>
        <div class="table-wrap"><table class="tbl">
          <thead><tr><th>名称</th><th>前缀</th><th>权限</th><th class="num">额度 / 已用</th>
          <th class="num">请求</th><th>最后使用</th><th>状态</th><th></th></tr></thead>
          <tbody>${d.items.map((k) => `<tr>
            <td class="strong">${esc(k.name)}</td>
            <td><code>${esc(k.key_prefix)}…</code></td>
            <td>${k.is_admin ? '<span class="tag accent">管理员</span>' : '<span class="tag">业务</span>'}</td>
            <td class="num">${k.quota_credits ? num(k.quota_credits) : '不限'} / ${num(k.used_credits)}</td>
            <td class="num">${num(k.request_count)}</td>
            <td class="nowrap muted">${k.last_used_at ? rel(k.last_used_at) : '从未'}</td>
            <td>${k.enabled ? '<span class="tag ok">启用</span>' : '<span class="tag mute">禁用</span>'}</td>
            <td class="nowrap">
              <button class="btn xs" data-act="toggle-key" data-id="${k.id}" data-on="${k.enabled ? 1 : 0}">${k.enabled ? '禁用' : '启用'}</button>
              ${k.is_admin ? '' : `<button class="btn xs ghost danger" data-act="del-key" data-id="${k.id}">删除</button>`}
            </td></tr>`).join('')}</tbody></table></div>
      </div>
      <div class="hint-card">
        <b>业务侧用法</b>（额度按任务实际/预估积分累加；0 = 不限额；管理员 Key 同时能进面板）
        <pre class="code" style="margin-top:8px">curl -s http://127.0.0.1:8300/v1/images/generations \
  -H "Authorization: Bearer sk-…" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedream-5.5","prompt":"一只猫","n":1,"size":"2048x2048"}'</pre>
      </div>`;
  }

  // --------------------------------------------------------------- 设置
  function renderSettings() {
    const d = S.settings;
    if (!d) { $('#view').innerHTML = empty('加载中…'); return; }
    const r = d.runtime;
    const f = (key, label, hint, step = '1') => `
      <div class="field"><label>${label}</label>
        <input class="input" id="set_${key}" type="number" step="${step}" value="${r[key]}">
        <span class="hint">${hint}</span></div>`;
    const modelOpts = (S.models && S.models.models || []).map((m) =>
      `<option value="${esc(m.key)}" ${r.default_model === m.key ? 'selected' : ''}>${esc(m.name)} · ${esc(m.key)}</option>`).join('');
    $('#view').innerHTML = `
      <div class="split">
        <div class="box">
          <header><h3>调度与闸门</h3><span class="grow"></span>
            <button class="btn primary xs" data-act="save-settings">保存</button></header>
          <div class="pad">
            <div class="grid2">
              ${f('concurrency', '全局并发上限', '同时在途的任务数；超出的留在 queued')}
              ${f('per_account', '单账号并发', '上游容忍 4，保守取 2')}
              ${f('poll_interval', '轮询间隔（秒）', '协调器合并查询在途任务', '0.5')}
              ${f('task_timeout', '任务超时（秒）', '超时判失败，不自动重发')}
              ${f('sync_timeout', '同步接口等待（秒）', '同步接口最长等待，超时返回 202')}
              ${f('max_retry', '提交重试次数', '仅对上游明确拒绝（限流/风控/额度）重试')}
              ${f('cooldown_rate', '限流冷却（秒）', 'ret=1010/1/1057 等')}
              ${f('cooldown_risk', '风控冷却（秒）', 'ret=1018/1019/1021 等')}
              ${f('credit_low_threshold', '低积分告警线', '可用积分低于 阈值×账号数 时提示')}
              ${f('models_live_refresh', '能力表刷新（秒）', '0 = 不自动刷新')}
            </div>
            <div class="field"><label>默认模型</label>
              <select class="select" id="set_default_model">${modelOpts}</select>
              <span class="hint">调用方不传 model 时用它</span></div>
          </div>
        </div>
        <div>
          <div class="box"><header><h3>上游站点</h3></header>
            <div class="pad"><dl class="kv">
              <dt>Base</dt><dd>${esc(d.upstream.base)}</dd>
              <dt>aid</dt><dd>${esc(d.upstream.aid)}</dd>
              <dt>da_version</dt><dd>${esc(d.upstream.da_version)}</dd>
              <dt>web_version</dt><dd>${esc(d.upstream.web_version)}</dd>
              <dt>appvr / pf</dt><dd>${esc(d.upstream.appvr)} / ${esc(d.upstream.pf)}</dd>
              <dt>代理</dt><dd>${d.upstream.proxy ? '已配置' : '直连'}</dd>
            </dl>
            <div class="divider"></div>
            <div class="hint-card">站点常量写在环境变量里（<code>JIMENG_*</code>）。改完要重启进程；
            运行期参数保存在库里，保存即生效。</div>
            </div>
          </div>
          <div class="box"><header><h3>关于</h3></header>
            <div class="pad"><dl class="kv">
              <dt>版本</dt><dd>${esc(d.version)}</dd>
              <dt>数据库</dt><dd>${esc(d.db.path)}</dd>
              <dt>账号数</dt><dd>${num(d.db.accounts)}</dd>
            </dl></div>
          </div>
        </div>
      </div>`;
  }

  // ------------------------------------------------------------- 弹窗工具
  const dlg = () => $('#dlg');

  let dlgCleanup = null;
  let dlgInstance = 0;

  function openDialog({ title, body, okText = '确定', okClass = 'primary', onOk,
                        after, onClose, wide = false, xwide = false }) {
    // 同一个 <dialog> 复用：上一个弹窗 close 事件可能在下一个已打开后才派发，
    // 若不加实例号，旧事件会把新弹窗的清理函数（如停止扫码轮询）跑掉。
    const inst = ++dlgInstance;
    dlgCleanup = onClose
      ? () => { if (inst === dlgInstance) onClose(); }
      : null;
    $('#dlgTitle').textContent = title;
    $('#dlgBody').innerHTML = body;
    $('#dlgFoot').innerHTML = `
      <button class="btn" type="button" data-dlg="cancel">取消</button>
      <button class="btn ${okClass}" type="button" data-dlg="ok">${esc(okText)}</button>`;
    if (xwide) dlg().style.width = 'min(1024px, calc(100vw - 32px))';
    else if (wide) dlg().style.width = 'min(720px, calc(100vw - 32px))';
    else dlg().style.width = '';
    const close = () => dlg().close();
    $('#dlgFoot').querySelector('[data-dlg=cancel]').onclick = close;
    $('#dlgFoot').querySelector('[data-dlg=ok]').onclick = async () => {
      if (!onOk) return close();
      const keep = await onOk();
      if (keep !== false) close();
    };
    dlg().showModal();
    if (after) setTimeout(after, 20);
  }

  function openDrawer(title, body, foot = '') {
    $('#drawerTitle').textContent = title;
    $('#drawerBody').innerHTML = body;
    $('#drawerFoot').innerHTML = foot;
    $('#drawer').classList.add('open');
    $('#scrim').classList.add('on');
  }
  function closeDrawer() {
    $('#drawer').classList.remove('open');
    $('#scrim').classList.remove('on');
  }

  // ------------------------------------------------------------- 业务动作
  async function addAccountDialog() {
    openDialog({
      title: '添加账号',
      wide: true,
      body: `
        <div class="tabs" role="tablist">
          <button class="tab on" type="button" data-tab="single">单个添加</button>
          <button class="tab" type="button" data-tab="batch">批量导入</button>
        </div>
        <div id="tab-single">
          <div class="field"><label>sessionid <span class="pos">*</span></label>
            <textarea class="textarea" id="accSid" placeholder="粘贴 sessionid，或整行 cookie（sessionid=…; sid_guard=…; ttwid=…）"></textarea>
            <span class="hint">取法：登录 jimeng.jianying.com → DevTools → Application → Cookies → 复制 <code>sessionid</code> 的值。
            整行 cookie 也能直接粘：会自动取出 <code>sessionid</code>，并从 <code>sid_guard</code> 解析到期日。<br>
            实测<b>只有 sessionid 是必需的</b>，其余 20 多个键都不参与请求。</span></div>
          <div class="field"><label>备注（可选）</label>
            <input class="input" id="accRemark" placeholder="例如：主号 / 测试号"></div>
          <label class="check"><input type="checkbox" id="accProbe" checked> 添加后立刻做一次零成本探活（用户信息 + 积分）</label>
        </div>
        <div id="tab-batch" hidden>
          <div class="field"><label>每行一个账号 <span class="pos">*</span></label>
            <textarea class="textarea" id="batchText" style="min-height:150px" placeholder="# 一行一个，支持整行 cookie&#10;sessionid=aaaa…; sid_guard=aaaa…|1789021926|31536000|…&#10;bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"></textarea>
            <span class="hint">支持 <code>#</code> 注释行；自动去重（批内 + 池内）；单次最多 200 行。
            每行的 <code>sid_guard</code> 会被解析成到期日。</span></div>
          <div class="grid2">
            <div class="field"><label>统一备注</label>
              <input class="input" id="batchRemark" placeholder="例如：11 月批次"></div>
            <div class="field"><label>探活</label>
              <label class="check" style="height:34px"><input type="checkbox" id="batchProbe" checked> 导入后并发探活（4 线程）</label></div>
          </div>
        </div>`,
      okText: '添加',
      after: () => {
        const tabs = $$('.tab', $('#dlgBody'));
        const okBtn = $('#dlgFoot').querySelector('[data-dlg=ok]');
        if (okBtn) okBtn.textContent = '添加';
        tabs.forEach((t) => {
          t.onclick = () => {
            tabs.forEach((x) => x.classList.toggle('on', x === t));
            $('#tab-single').hidden = t.dataset.tab !== 'single';
            $('#tab-batch').hidden = t.dataset.tab !== 'batch';
            $('#dlgFoot').querySelector('[data-dlg=ok]').textContent =
              t.dataset.tab === 'single' ? '添加' : '批量导入';
          };
        });
      },
      onOk: async () => {
        const batchMode = !$('#tab-batch').hidden;
        if (batchMode) return await submitBatchImport();
        const sid = $('#accSid').value.trim();
        if (!sid) { toast('请填写 sessionid', 'bad'); return false; }
        try {
          const res = await api('/accounts', {
            method: 'POST',
            body: { sessionid: sid, remark: $('#accRemark').value.trim(), probe: $('#accProbe').checked },
          });
          if (res.ok) toast('账号已加入，探活成功', 'ok');
          else toast('已加入，但探活失败：' + (res.error || ''), 'warn');
          S.accounts = await api('/accounts');
          renderView();
        } catch (e) { toast(e.message, 'bad'); return false; }
        return true;
      },
    });
  }

  async function submitBatchImport() {
    const text = $('#batchText').value.trim();
    if (!text) { toast('请粘贴账号', 'bad'); return false; }
    try {
      const r = await api('/accounts/batch', {
        method: 'POST',
        body: { text, remark: $('#batchRemark').value.trim(), probe: $('#batchProbe').checked },
      });
      S.accounts = await api('/accounts');
      renderView();
      dlg().close();
      showBatchResult(r);
    } catch (e) { toast(e.message, 'bad'); return false; }
    return false;
  }

  function showBatchResult(r) {
    const okRows = (r.items || []).filter((x) => x.ok !== false);
    const badRows = (r.items || []).filter((x) => x.ok === false);
    const body = `
      <div class="row" style="margin-bottom:12px">
        <span class="tag ok">新增 ${r.created}</span>
        <span class="tag ${badRows.length ? 'bad' : ''}">探活失败 ${badRows.length}</span>
        <span class="tag mute">跳过 ${r.skipped.length}</span>
      </div>
      ${badRows.length ? `<div class="field"><label>探活失败</label>
        <div class="hint-card neg">${badRows.map((x) => `第 ${x.line} 行：#${x.id} ${esc(x.error)}`).join('<br>')}</div></div>` : ''}
      ${r.skipped.length ? `<div class="field"><label>跳过</label>
        <div class="hint-card">${r.skipped.map((x) => `第 ${x.line} 行：${esc(x.reason)}`).join('<br>')}</div></div>` : ''}
      ${okRows.length ? `<div class="field"><label>已入池</label>
        <div class="hint-card">${okRows.map((x) => `#${x.id} ${esc(x.name) || '未命名'} · 积分 ${x.credits == null ? '—' : x.credits}`).join('<br>')}</div></div>` : ''}`;
    openDialog({ title: '批量导入结果', body, okText: '完成', onOk: () => true });
  }

  function confirmDialog(text, onOk, okText = '确认') {
    openDialog({
      title: '请确认', body: `<div class="hint-card">${text}</div>`,
      okText, okClass: 'primary',
      onOk: async () => { await onOk(); return true; },
    });
  }

  async function openTask(id) {
    try {
      const t = await api('/tasks/' + id);
      const imgs = (t.images || []).length
        ? `<div class="imgs">${t.images.map((i) => `<img src="${esc(i.url)}" data-zoom="${esc(i.url)}" alt="" loading="lazy">`).join('')}</div>`
        : `<div class="hint-card">暂无产物${t.status === 'failed' ? '：' + esc(t.error) : '，任务可能仍在生成'}</div>`;
      const body = `
        <div class="row" style="margin-bottom:12px">${taskPill(t.status)}
          ${t.error_kind ? `<span class="tag bad">${esc(t.error_kind)}</span>` : ''}
          <span class="grow"></span><span class="note">${esc(t.id)}</span></div>
        <dl class="kv">
          <dt>模型</dt><dd>${esc(t.model)} · ${esc(t.mode)}${(t.extras && t.extras.requested_model) ? ` <span class="tag accent">请求名 ${esc(t.extras.requested_model)}</span>` : ''}</dd>
          <dt>尺寸</dt><dd>${esc(t.size)} × ${t.count} 张 · ${esc(t.resolution)}</dd>
          <dt>账号</dt><dd>${t.account_id ? '#' + t.account_id : '尚未分配'}</dd>
          <dt>创建</dt><dd>${fmtTime(t.created_at, true)}</dd>
          <dt>开始</dt><dd>${t.started_at ? fmtTime(t.started_at, true) : '—'}</dd>
          <dt>完成</dt><dd>${t.finished_at ? fmtTime(t.finished_at, true) : '—'}</dd>
          <dt>耗时</dt><dd>${dur(t.started_at, t.finished_at)}</dd>
          <dt>进度</dt><dd>${esc(t.progress) || '—'}</dd>
          <dt>积分</dt><dd>预估 ${t.forecast_credits == null ? '—' : t.forecast_credits}
            / 实扣 ${t.charged_credits == null ? '未对账' : t.charged_credits}</dd>
        </dl>
        <div class="divider"></div>
        <div class="field"><label>提示词</label><div class="hint-card">${esc(t.prompt) || '—'}</div></div>
        ${t.negative_prompt ? `<div class="field"><label>负向提示词</label><div class="hint-card">${esc(t.negative_prompt)}</div></div>` : ''}
        ${t.error ? `<div class="field"><label>错误</label><div class="hint-card neg">${esc(t.error)}</div></div>` : ''}
        <div class="field"><label>产物</label>${imgs}</div>`;
      const foot = `
        <button class="btn" data-act="task-refresh" data-id="${t.id}">刷新图片链接</button>
        <button class="btn" data-act="task-reconcile" data-id="${t.id}">对账实扣</button>
        ${['queued', 'running', 'submitting'].includes(t.status) ? '' :
          `<button class="btn danger" data-act="task-delete" data-id="${t.id}">删除记录</button>`}`;
      openDrawer('任务详情', body, foot);
    } catch (e) { toast(e.message, 'bad'); }
  }

  // ------------------------------------------------------------- 扫码登录
  let qrPoll = null;
  let qrTimer = null;

  function stopQrPoll(cancel = false) {
    if (qrTimer) clearTimeout(qrTimer);
    qrTimer = null;
    if (cancel && qrPoll) {
      api('/login/qr/' + qrPoll.id + '/cancel', { method: 'POST' }).catch(() => {});
    }
    qrPoll = null;
  }

  async function startQrLogin(provider = 'douyin') {
    if (S.accounts && S.accounts.login_assistant === false) {
      openDialog({
        title: '登录助手未安装',
        body: `<div class="hint-card">扫码登录依赖 Playwright + 浏览器。<br><br>
          本机安装：<pre class="code">uv pip install -r requirements-login.txt
playwright install chromium</pre>
          服务器（Docker）里安装：<pre class="code">docker compose exec jimeng2api \
  playwright install --with-deps chromium</pre>
          装好后刷新本页即可。</div>`,
        okText: '知道了',
      });
      return;
    }
    let st;
    try {
      st = await api('/login/qr/start', {
        method: 'POST', body: { remark: '扫码登录', provider },
      });
    } catch (e) {
      toast(e.message, 'bad');
      return;
    }
    qrPoll = { id: st.session_id, version: -1, done: false };
    openDialog({
      title: '扫码登录即梦',
      body: `<div class="qr-wrap">
          <div class="qr-box" id="qrBox"><div class="skel" style="width:80%;height:80%"></div></div>
          <div class="qr-side">
            <div class="tabs" style="margin-bottom:10px">
              <button class="tab ${provider === 'douyin' ? 'on' : ''}" type="button" data-provider="douyin">抖音</button>
              <button class="tab ${provider === 'feishu' ? 'on' : ''}" type="button" data-provider="feishu">飞书</button>
            </div>
            <div class="qr-state" id="qrState">${esc(st.message)}</div>
            <div class="hint" style="margin-top:8px">用<b>${provider === 'feishu' ? '飞书' : '抖音'} App</b>
              扫码并在手机上确认，成功后账号自动入池。</div>
            <div class="hint" style="margin-top:8px">二维码 5 分钟内有效；过期会自动提示。</div>
            <div class="drawer-foot" style="margin-top:16px">
              <button class="btn xs" type="button" id="qrRestart">重新生成</button>
            </div>
          </div>
        </div>`,
      okText: '关闭',
      onOk: () => { stopQrPoll(true); return true; },
      onClose: () => stopQrPoll(true),
    });
    const restart = $('#qrRestart');
    if (restart) restart.onclick = () => {
      stopQrPoll(true); dlg().close(); setTimeout(() => startQrLogin(provider), 250);
    };
    $$('#dlgBody .tab[data-provider]').forEach((t) => {
      t.onclick = () => {
        if (t.dataset.provider === provider) return;
        stopQrPoll(true); dlg().close();
        setTimeout(() => startQrLogin(t.dataset.provider), 250);
      };
    });
    pollQr();
  }

  async function pollQr() {
    if (!qrPoll || qrPoll.done) return;
    try {
      const st = await api(`/login/qr/${qrPoll.id}?v=${qrPoll.version}`);
      if (st.qr_version !== qrPoll.version && st.qr) {
        qrPoll.version = st.qr_version;
        const box = $('#qrBox');
        if (box) box.innerHTML = `<img src="${st.qr}" alt="抖音扫码登录">`;
      }
      const msg = $('#qrState');
      if (msg) msg.textContent = st.message || '';
      if (st.state === 'success') {
        qrPoll.done = true;
        toast(st.message, 'ok');
        setTimeout(() => { dlg().close(); }, 800);
        S.accounts = await api('/accounts');
        renderView();
        return;
      }
      if (['error', 'expired', 'cancelled'].includes(st.state)) {
        qrPoll.done = true;
        toast(st.message || '扫码登录结束', st.state === 'error' ? 'bad' : 'warn');
        const box = $('#qrBox');
        if (box && st.state !== 'cancelled') {
          box.innerHTML = `<div class="empty" style="padding:12px">${esc(st.message || '')}</div>`;
        }
        return;
      }
    } catch (e) {
      if (e.status === 404) { qrPoll.done = true; return; }
    }
    qrTimer = setTimeout(pollQr, 1500);
  }

  // ----------------------------------------------------------- 远程浏览器
  let rb = null;

  function stopRemote() {
    if (rb && rb.ws) { try { rb.ws.close(); } catch (e) { /* noop */ } }
    rb = null;
    api('/remote/stop', { method: 'POST' }).catch(() => {});
  }

  async function startRemote() {
    if (S.accounts && S.accounts.remote_browser === false) {
      openDialog({
        title: '远程浏览器未安装',
        body: `<div class="hint-card">它复用扫码登录的 Playwright + Chromium，不需要 noVNC / X11。<br><br>
          <pre class="code">uv pip install -r requirements-login.txt
playwright install chromium</pre>
          Docker：<code>WITH_LOGIN=1 docker compose up -d --build</code></div>`,
        okText: '知道了',
      });
      return;
    }
    openDialog({
      title: '远程浏览器',
      xwide: true,
      body: `<div class="remote-wrap">
          <div class="remote-bar">
            <button class="btn xs" type="button" data-rb="home">登录页</button>
            <button class="btn xs" type="button" data-rb="back">后退</button>
            <button class="btn xs" type="button" data-rb="reload">刷新</button>
            <span class="grow"></span>
            <span class="note" id="rbStatus">正在启动浏览器…</span>
            <button class="btn xs primary" type="button" data-rb="grab">抓取登录态</button>
          </div>
          <canvas id="rbCanvas" width="1280" height="800" tabindex="0"></canvas>
          <div class="hint">点击画面即可操作：手机号、验证码、滑块都在这块画布里完成；登录成功后会自动入池（每秒检查一次 HttpOnly 的 sessionid）。</div>
        </div>`,
      okText: '关闭',
      onOk: () => { stopRemote(); return true; },
      onClose: () => stopRemote(),
    });

    let info;
    try {
      info = await api('/remote/start', { method: 'POST' });
    } catch (e) { toast(e.message, 'bad'); return; }

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(proto + '://' + location.host + info.ws);
    ws.binaryType = 'blob';
    rb = { ws, meta: { width: 1280, height: 800 }, down: false, lastMove: 0 };
    const canvas = $('#rbCanvas');
    const status = (t) => { const el = $('#rbStatus'); if (el) el.textContent = t; };

    ws.onmessage = async (ev) => {
      if (typeof ev.data === 'string') {
        let msg; try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === 'state') {
          if (msg.meta) rb.meta = msg.meta;
          status(msg.state === 'live' ? '已连接 · 点击画面操作' : ('状态：' + msg.state));
          if (msg.state === 'error') status('启动失败：' + (msg.message || ''));
        } else if (msg.type === 'login') {
          toast(msg.message || '检测到登录成功，已入池', 'ok');
          status('登录成功，已入池');
          api('/accounts').then((d) => { S.accounts = d; renderView(); }).catch(() => {});
        } else if (msg.type === 'grab') {
          toast(msg.ok ? (msg.message || '已抓取') : (msg.message || '尚未登录'),
            msg.ok ? 'ok' : 'warn');
          if (msg.ok) api('/accounts').then((d) => { S.accounts = d; renderView(); });
        } else if (msg.type === 'error') {
          status(msg.message || '出错了');
          toast(msg.message || '远程浏览器出错', 'bad');
        }
        return;
      }
      try {
        const bmp = await createImageBitmap(ev.data);
        if (!canvas) return;
        if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
          canvas.width = bmp.width; canvas.height = bmp.height;
        }
        canvas.getContext('2d').drawImage(bmp, 0, 0);
        bmp.close && bmp.close();
      } catch (e) { /* 帧解码失败跳过 */ }
    };
    ws.onclose = () => status('连接已断开');
    ws.onerror = () => status('连接出错');

    const pos = (e) => {
      const r = canvas.getBoundingClientRect();
      return {
        x: (e.clientX - r.left) / r.width * rb.meta.width,
        y: (e.clientY - r.top) / r.height * rb.meta.height,
      };
    };

    canvas.addEventListener('mousemove', (e) => {
      if (!rb || ws.readyState !== 1) return;
      const now = Date.now();
      if (now - rb.lastMove < 40) return;
      rb.lastMove = now;
      const p = pos(e);
      ws.send(JSON.stringify({ t: 'mouse', type: 'mouseMoved', x: p.x, y: p.y }));
    });
    canvas.addEventListener('mousedown', (e) => {
      e.preventDefault(); canvas.focus();
      const p = pos(e);
      ws.send(JSON.stringify({ t: 'mouse', type: 'mousePressed', x: p.x, y: p.y,
        button: 'left', buttons: 1, clickCount: 1 }));
      rb.down = true;
    });
    canvas.addEventListener('mouseup', (e) => {
      const p = pos(e);
      ws.send(JSON.stringify({ t: 'mouse', type: 'mouseReleased', x: p.x, y: p.y,
        button: 'left', buttons: 0, clickCount: 1 }));
      rb.down = false;
    });
    canvas.addEventListener('mouseleave', () => {
      if (rb && rb.down) {
        ws.send(JSON.stringify({ t: 'mouse', type: 'mouseReleased', x: 0, y: 0,
          button: 'left', buttons: 0, clickCount: 1 }));
        rb.down = false;
      }
    });
    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      const p = pos(e);
      const dy = Math.max(-300, Math.min(300, e.deltaY));
      ws.send(JSON.stringify({ t: 'wheel', x: p.x, y: p.y, deltaY: dy }));
    }, { passive: false });

    const SPECIAL = {
      Enter: 13, Backspace: 8, Tab: 9, Escape: 27, Delete: 46,
      ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
      Home: 36, End: 35,
    };
    canvas.addEventListener('keydown', (e) => {
      if (!rb) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;   // 组合键交给浏览器/系统
      if (SPECIAL[e.key]) {
        e.preventDefault();
        ws.send(JSON.stringify({ t: 'key', type: 'rawKeyDown', key: e.key,
          code: e.code, keyCode: SPECIAL[e.key] }));
        ws.send(JSON.stringify({ t: 'key', type: 'keyUp', key: e.key,
          code: e.code, keyCode: SPECIAL[e.key] }));
        return;
      }
      if (e.key.length === 1) {
        e.preventDefault();
        ws.send(JSON.stringify({ t: 'text', text: e.key }));
      }
    });

    $$('#dlgBody [data-rb]').forEach((b) => {
      b.onclick = () => {
        const act = b.dataset.rb;
        if (act === 'grab') { ws.send(JSON.stringify({ t: 'grab' })); status('正在抓取登录态…'); }
        else { ws.send(JSON.stringify({ t: act })); }
      };
    });
    setTimeout(() => canvas && canvas.focus(), 100);
  }

  // ------------------------------------------------------------- 事件委托
  const ACTIONS = {
    refresh: () => refresh(true),
    'qr-login': () => startQrLogin(),
    'remote-browser': () => startRemote(),
    'goto-tasks': () => { location.hash = '#tasks'; },
    'goto-models': () => { location.hash = '#models'; },
    'add-account': () => addAccountDialog(),
    'probe-all': async () => {
      toast('开始逐个探活…');
      try {
        const res = await api('/accounts/probe-all', { method: 'POST' });
        const bad = (res.items || []).filter((x) => !x.ok);
        toast(bad.length ? `${res.items.length - bad.length} 个正常，${bad.length} 个异常` : '全部正常',
          bad.length ? 'warn' : 'ok');
        S.accounts = await api('/accounts');
        renderView();
      } catch (e) { toast(e.message, 'bad'); }
    },
    'probe-account': async (id) => {
      toast('探活中…');
      try {
        const r = await api(`/accounts/${id}/probe`, { method: 'POST' });
        toast(r.ok ? `登录有效 · 积分 ${r.probe.credits}` : ('登录失败：' + r.error), r.ok ? 'ok' : 'bad');
        S.accounts = await api('/accounts'); renderView();
      } catch (e) { toast(e.message, 'bad'); }
    },
    'toggle-account': async (id, el) => {
      const on = el.dataset.on === '1';
      try {
        await api(`/accounts/${id}`, { method: 'PATCH', body: { enabled: !on } });
        S.accounts = await api('/accounts'); renderView();
        toast(on ? '已禁用' : '已启用', 'ok');
      } catch (e) { toast(e.message, 'bad'); }
    },
    'clear-cooldown': async (id) => {
      try { await api(`/accounts/${id}/cooldown`, { method: 'POST', body: { seconds: 0 } });
        S.accounts = await api('/accounts'); renderView(); toast('已解除冷却', 'ok');
      } catch (e) { toast(e.message, 'bad'); }
    },
    'del-account': (id) => confirmDialog(
      '删除账号只移除本地凭据，不会影响上游账号本身。该账号的历史任务会保留，但不再参与调度。',
      async () => {
        try { await api(`/accounts/${id}`, { method: 'DELETE' });
          S.accounts = await api('/accounts'); renderView(); toast('已删除', 'ok');
        } catch (e) { toast(e.message, 'bad'); }
      }, '删除'),
    'task-open': (id) => openTask(id),
    'task-search': () => {
      S.tasksQ = ($('#taskSearch').value || '').trim(); S.tasksPage = 0; refresh(true);
    },
    'task-clear': () => { S.tasksQ = ''; S.tasksStatus = ''; S.tasksPage = 0; refresh(true); },
    'task-prev': () => { S.tasksPage = Math.max(0, S.tasksPage - 1); refresh(true); },
    'task-next': () => { S.tasksPage += 1; refresh(true); },
    'task-refresh': async (id) => {
      try { const r = await api(`/tasks/${id}/refresh`, { method: 'POST' });
        toast(r.ok ? '图片链接已刷新' : ('失败：' + r.error), r.ok ? 'ok' : 'bad');
        openTask(id);
      } catch (e) { toast(e.message, 'bad'); }
    },
    'task-reconcile': async (id) => {
      try {
        const r = await api(`/tasks/${id}/reconcile`, { method: 'POST' });
        toast(r.found ? `实扣 ${r.amount} 积分` : ('未找到消耗记录：' + r.reason),
          r.found ? 'ok' : 'warn');
        if (r.found) openTask(id);
      } catch (e) { toast(e.message, 'bad'); }
    },
    'task-delete': (id) => confirmDialog('只删除本地任务记录；上游没有取消接口，产物仍会留在即梦账号里。', async () => {
      try { await api(`/tasks/${id}`, { method: 'DELETE' }); closeDrawer(); refresh(true); toast('已删除', 'ok'); }
      catch (e) { toast(e.message, 'bad'); }
    }, '删除'),
    'usage-days': (id) => { S.usageDays = Number(id); refresh(true); },
    'log-tab': (id) => { S.logsTab = id; refresh(true); },
    'new-key': () => {
      openDialog({
        title: '创建访问密钥',
        body: `<div class="field"><label>名称</label><input class="input" id="keyName" placeholder="例如：生产环境 / 同事 A"></div>
          <div class="field"><label>积分额度（0 = 不限）</label><input class="input" id="keyQuota" type="number" min="0" value="0">
          <span class="hint">额度按任务实际（或预估）积分累加，用尽后该 Key 返回 402。</span></div>
          <label class="check"><input type="checkbox" id="keyAdmin"> 设为管理员（可登录面板）</label>`,
        okText: '创建',
        onOk: async () => {
          try {
            const r = await api('/keys', {
              method: 'POST',
              body: { name: $('#keyName').value.trim() || '未命名', quota_credits: Number($('#keyQuota').value) || 0, is_admin: $('#keyAdmin').checked },
            });
            S.keys = { items: r.items };
            renderView();
            dlg().close();
            openDialog({
              title: '密钥已创建（只显示这一次）',
              body: `<div class="hint-card">复制并保存到密码管理器。关闭后无法再次查看明文。</div>
                <div class="copyrow" style="margin-top:10px">
                  <input class="input" id="newKeyVal" value="${esc(r.key)}" readonly>
                  <button class="btn" type="button" id="copyNewKey">复制</button></div>`,
              okText: '我已保存',
              onOk: () => true,
              after: () => {
                const b = $('#copyNewKey');
                if (b) b.onclick = () => copyText($('#newKeyVal').value);
              },
            });
          } catch (e) { toast(e.message, 'bad'); return false; }
          return true;
        },
      });
    },
    'toggle-key': async (id, el) => {
      const on = el.dataset.on === '1';
      try { await api(`/keys/${id}`, { method: 'PATCH', body: { enabled: !on } });
        S.keys = await api('/keys'); renderView(); toast(on ? '已禁用' : '已启用', 'ok');
      } catch (e) { toast(e.message, 'bad'); }
    },
    'del-key': (id) => confirmDialog('删除后使用该 Key 的调用会立刻 401。', async () => {
      try { await api(`/keys/${id}`, { method: 'DELETE' }); S.keys = await api('/keys'); renderView(); toast('已删除', 'ok'); }
      catch (e) { toast(e.message, 'bad'); }
    }, '删除'),
    'redirect-add': async () => {
      const aliasEl = $('#redirAlias');
      const alias = (aliasEl ? aliasEl.value : '').trim();
      if (!alias) { toast('请填写对外模型名', 'bad'); return; }
      const targetEl = $('#redirTarget');
      const noteEl = $('#redirNote');
      try {
        await api('/model-redirects', {
          method: 'POST',
          body: {
            alias,
            target: targetEl ? targetEl.value : '',
            note: noteEl ? noteEl.value.trim() : '',
          },
        });
        S.models = await api('/models'); renderView(); toast('已添加', 'ok');
      } catch (e) { toast(e.message, 'bad'); }
    },
    'redirect-fill': (alias) => {
      const el = $('#redirAlias');
      if (el) { el.value = alias || ''; el.focus(); }
    },
    'redirect-toggle': async (id, el) => {
      const on = el.dataset.on === '1';
      try {
        await api(`/model-redirects/${id}`, { method: 'PATCH', body: { enabled: !on } });
        S.models = await api('/models'); renderView();
        toast(on ? '已停用' : '已启用', 'ok');
      } catch (e) { toast(e.message, 'bad'); }
    },
    'redirect-del': (id, el) => confirmDialog(
      `删除后调用方再传 <code>${esc(el.dataset.alias || '')}</code> 会得到「未知模型」。`,
      async () => {
        try {
          await api(`/model-redirects/${id}`, { method: 'DELETE' });
          S.models = await api('/models'); renderView(); toast('已删除', 'ok');
        } catch (e) { toast(e.message, 'bad'); }
      }, '删除'),
    'redirect-import': () => {
      openDialog({
        title: '批量导入模型重定向',
        wide: true,
        body: `<div class="field"><label>每行一条：<code>对外名 =&gt; 目标</code></label>
          <textarea class="textarea" id="redirBulk" style="min-height:160px" placeholder="# 也支持 JSON：{&quot;gpt-image-1&quot;:&quot;seedream-5.5&quot;}&#10;gpt-image-1 => seedream-5.5&#10;doubao-seedream-4-0-250828 => seedream-4.0"></textarea>
          <span class="hint">分隔符支持 <code>=&gt;</code> / <code>-&gt;</code> / <code>→</code> / Tab；<code>#</code> 开头为注释。
          目标可以填对外模型名、上游 key 或后编辑工具 id（如 <code>jimeng-hd</code>）。</span></div>
          <label class="check"><input type="checkbox" id="redirOverwrite"> 覆盖已存在的同名配置</label>`,
        okText: '导入',
        onOk: async () => {
          const box = $('#redirBulk');
          const text = box ? box.value.trim() : '';
          if (!text) { toast('请粘贴配置', 'bad'); return false; }
          try {
            const r = await api('/model-redirects/import', {
              method: 'POST',
              body: { text, overwrite: $('#redirOverwrite').checked },
            });
            S.models = await api('/models');
            renderView();          // 结果弹窗盖在上面，底下的表也要同步
            dlg().close();
            const bad = (r.items || []).filter((x) => !x.ok);
            openDialog({
              title: '导入结果',
              body: `<div class="row" style="margin-bottom:12px">
                  <span class="tag ok">新增 ${r.created}</span>
                  <span class="tag accent">更新 ${r.updated}</span>
                  <span class="tag ${bad.length ? 'bad' : 'mute'}">失败 ${bad.length}</span></div>
                ${bad.length
                  ? `<div class="hint-card neg">${bad.map((x) =>
                      `第 ${x.line} 行 <code>${esc(x.alias)}</code>：${esc(x.error)}`).join('<br>')}</div>`
                  : '<div class="hint-card">全部写入成功。</div>'}`,
              okText: '完成', onOk: () => true,
            });
          } catch (e) { toast(e.message, 'bad'); return false; }
          return false;
        },
      });
    },
    'refresh-models': async () => {
      toast('读取上游能力表…');
      try { S.models = await api('/models/refresh', { method: 'POST' }); renderView(); toast('已更新', 'ok'); }
      catch (e) { toast(e.message, 'bad'); }
    },
    'save-settings': async () => {
      const get = (k) => { const el = $('#set_' + k); return el ? (el.type === 'number' ? Number(el.value) : el.value) : null; };
      const body = {};
      ['concurrency', 'per_account', 'poll_interval', 'task_timeout', 'sync_timeout',
        'max_retry', 'cooldown_rate', 'cooldown_risk', 'credit_low_threshold',
        'models_live_refresh', 'default_model'].forEach((k) => {
        const v = get(k);
        if (v !== null && v !== '' && !Number.isNaN(v)) body[k] = v;
      });
      try { const r = await api('/settings', { method: 'PUT', body });
        S.settings = await api('/settings'); renderView(); toast('已保存', 'ok');
      } catch (e) { toast(e.message, 'bad'); }
    },
    'play-dry': async () => {
      const payload = collectPlay(true);
      if (!payload) return;
      try {
        const r = await api('/tasks', { method: 'POST', body: payload });
        S.play.dry = r;
        S.play.resolution = r.resolution;
        renderModels();
        toast('草稿已构造（未发送、未计费）', 'ok');
      } catch (e) { toast(e.message, 'bad'); }
    },
    'play-real': async () => {
      if (!$('#playConfirm').checked) { toast('先勾选"我确认这是计费动作"', 'warn'); return; }
      const payload = collectPlay(false);
      if (!payload) return;
      confirmDialog(
        `即将用池子里的账号提交：<b>${esc(payload.model)}</b>，${payload.count} 张，${esc(payload.size)}。<br>
         这是<b>计费动作</b>，成功后不可撤销（上游没有取消接口）。`,
        async () => {
          try {
            const r = await api('/tasks', { method: 'POST', body: payload });
            S.play.dry = { dry_run: false, task_id: r.task_id, estimate_credits: r.estimate_credits, degradations: r.degradations };
            renderModels();
            toast('已受理：' + r.task_id, 'ok');
          } catch (e) { toast(e.message, 'bad'); }
        }, '确认提交');
    },
  };

  function collectPlay(dryRun) {
    const model = $('#playModel').value;
    const size = $('#playSize').value;
    const count = Number($('#playCount').value) || 1;
    const prompt = $('#playPrompt').value.trim();
    const image = $('#playImage').value.trim();
    if (!prompt) { toast('请填写提示词', 'bad'); return null; }
    S.play.model = model; S.play.size = size; S.play.count = count;
    const opt = $('#playSize').selectedOptions[0];
    if (opt) S.play.resolution = opt.dataset.res || '';
    return {
      prompt, model, size, count, image,
      resolution: S.play.resolution || '',
      dry_run: dryRun, confirm: !dryRun,
    };
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const fn = ACTIONS[el.dataset.act];
    if (fn) { e.preventDefault(); fn(el.dataset.id, el); }
    const zoom = e.target.closest('[data-zoom]');
    if (zoom) {
      $('#lightboxImg').src = zoom.dataset.zoom;
      $('#lightbox').classList.add('on');
    }
  });

  // 选择变化：模型切换后重建尺寸/张数选项
  document.addEventListener('change', (e) => {
    if (e.target.id === 'playModel') {
      S.play.model = e.target.value; S.play.size = ''; S.play.count = 0; S.play.resolution = '';
      ensurePlayDefaults(); renderModels();
    }
    if (e.target.id === 'playConfirm') {
      const b = $('#playRealBtn');
      if (b) b.disabled = !e.target.checked;
    }
    if (e.target.id === 'playSize') {
      const opt = e.target.selectedOptions[0];
      S.play.size = e.target.value;
      if (opt) S.play.resolution = opt.dataset.res || '';
      const est = $('#playEstimate');
      const m = (S.models.models || []).find((x) => x.key === S.play.model);
      if (est && m) est.textContent = priceLine(m, S.play.resolution);
    }
    if (e.target.id === 'taskStatusFilter') {
      S.tasksStatus = e.target.value; S.tasksPage = 0; refresh(true);
    }
    if (e.target.id === 'logErrOnly') {
      S.logsErrors = e.target.checked; refresh(true);
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeDrawer(); $('#lightbox').classList.remove('on'); }
    if (e.key === 'Enter' && e.target.id === 'taskSearch') ACTIONS['task-search']();
  });

  // ------------------------------------------------------------------ 启动
  applyTheme();
  $('#btnTheme').onclick = toggleTheme;
  $('#btnLogout').onclick = logout;
  $('#btnRefresh').onclick = () => refresh(true);
  $('#dlg').addEventListener('close', () => {
    if (dlgCleanup) { const f = dlgCleanup; dlgCleanup = null; f(); }
  });
  $('#drawerClose').onclick = closeDrawer;
  $('#scrim').onclick = closeDrawer;
  $('#lightboxClose').onclick = () => $('#lightbox').classList.remove('on');
  $('#lightbox').onclick = (e) => { if (e.target.id === 'lightbox') e.currentTarget.classList.remove('on'); };
  $('#gateForm').onsubmit = (e) => { e.preventDefault(); login($('#gateKey').value); };

  window.addEventListener('hashchange', async () => {
    if (!S.key) return;
    S.loaded = false;
    setChrome();
    $('#view').innerHTML = empty('加载中…');
    await refresh(true);
  });

  const saved = localStorage.getItem(LS_KEY);
  if (saved) {
    S.key = saved;
    hideGate();
    if (!location.hash) location.hash = '#overview';
    setChrome();
    refresh(true).then(startAuto);
  } else {
    showGate('');
  }
})();
