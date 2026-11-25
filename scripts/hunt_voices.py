#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色守护采集：等到上游音色列表恢复下发，立即抓取全部 ID 并落盘。

    uv run python scripts/hunt_voices.py              # 最多守 2 小时
    uv run python scripts/hunt_voices.py --interval 60 --rounds 60

**为什么需要守护**：上游音色列表接口会对高频访问限流 —— 同一账号连续访问后，
「全部音色」只返回「创建音色 / 我的音色」两个入口，等一段时间才会恢复。
观察到恢复的瞬间，前端会把整份音色数组放进 React hooks state，
此时提取即可拿到全部 name + id，不用逐个点试听。

提取优先级（都实测过，按可靠度排序）：
  1. hooks `memoizedState` 里的长数组（元素含 id/name）—— 最直接
  2. ReactVirtualized 的 `props.rowRenderer(0).props` —— 兜底
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.config import settings  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
HOME = "https://jimeng.jianying.com/ai-tool/home"
OUT = Path(__file__).resolve().parents[1] / "data" / "voices.json"

KILL = """() => {
  document.querySelectorAll('.lv-modal-wrapper, .lv-modal-mask, .arco-modal-mask')
    .forEach(e => e.remove());
  document.body.style.overflow = 'auto';
  return 1;
}"""

#: ⚠️ 切「全部音色」必须用**真实坐标点击**。locator.click(force=True) 会静默失效。
OPEN_AUDIO = """() => {
  const el = [...document.querySelectorAll('[role=combobox], button')]
    .find(e => (e.innerText||'').includes('创作类型')
            || (e.getAttribute('aria-label')||'').includes('创作类型'));
  if (!el) return false;
  el.click(); return true;
}"""

PICK_AUDIO = """() => {
  const it = [...document.querySelectorAll(
      '[role=menuitem], [role=option], li, [class*=menu-item]')]
    .find(e => (e.innerText||'').trim() === '音频生成');
  if (!it) return false;
  it.click(); return true;
}"""

TAB_POS = """() => {
  const el = [...document.querySelectorAll('*')]
    .find(e => (e.innerText||'').trim() === '全部音色' && e.children.length === 0);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return [Math.round(r.x + r.width/2), Math.round(r.y + r.height/2)];
}"""

EXTRACT = """() => {
  const lab = document.querySelector('.voice-grid-cell-label-RnMfVo');
  if (!lab) return null;
  const key = Object.keys(lab).find(k => k.startsWith('__reactFiber$'));
  if (!key) return null;
  let node = lab[key], d = 0;
  while (node && d < 90) {
    let st = node.memoizedState, si = 0;
    while (st && si < 50) {
      const v = st.memoizedState;
      if (Array.isArray(v) && v.length > 20 && v[0] && typeof v[0] === 'object') {
        const ks = Object.keys(v[0]);
        if (ks.some(k => /id|name|voice/i.test(k))) {
          const rows = v.map(o => {
            const r = {};
            for (const k of ks) {
              const x = o[k];
              if (x === null || ['string','number','boolean'].includes(typeof x)) r[k] = x;
            }
            return r;
          });
          return {src: 'hooks', len: v.length, rows: rows.slice(0, 300)};
        }
      }
      st = st.next; si++;
    }
    const mp = node.memoizedProps;
    if (mp && typeof mp.rowRenderer === 'function'
        && typeof mp.rowCount === 'number' && mp.rowCount > 20) {
      try {
        const el = mp.rowRenderer({index: 0, key: 0, style: {}, parent: {}, isScrolling: false});
        const p = el && el.props;
        if (p) {
          const r = {};
          for (const k of Object.keys(p)) {
            const x = p[k];
            if (x === null || ['string','number','boolean'].includes(typeof x)) r[k] = x;
          }
          return {src: 'rowRenderer', len: mp.rowCount, rows: [r]};
        }
      } catch (e) {}
    }
    node = node.return; d++;
  }
  return null;
}"""


def session_id() -> str:
    con = sqlite3.connect(settings.db_path)
    try:
        row = con.execute(
            "SELECT sessionid FROM accounts WHERE enabled=1 ORDER BY id LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit("账号池为空")
    return row[0]


def pick_id(row: dict) -> str:
    """从一条音色记录里找数字 ID。上游字段名可能是 voiceId / id / voice_id。"""
    for k in ("voiceId", "voice_id", "id", "vid", "timbreId", "speakerId"):
        v = row.get(k)
        if isinstance(v, str) and v.isdigit() and len(v) >= 10:
            return v
        if isinstance(v, int) and v > 10**10:
            return str(v)
    # 兜底：扫所有值
    for v in row.values():
        if isinstance(v, str) and v.isdigit() and len(v) >= 12:
            return v
    return ""


def pick_name(row: dict) -> str:
    for k in ("name", "voiceName", "voice_name", "title", "displayName", "label"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=120, help="每轮间隔秒数")
    ap.add_argument("--rounds", type=int, default=60, help="最多尝试轮数")
    args = ap.parse_args()

    sid = session_id()
    for attempt in range(1, args.rounds + 1):
        stamp = time.strftime("%H:%M:%S")
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(
                    headless=True, args=["--disable-blink-features=AutomationControlled"])
                ctx = b.new_context(viewport={"width": 1600, "height": 1000},
                                    user_agent=UA, locale="zh-CN")
                ctx.add_cookies([{"name": "sessionid", "value": sid,
                                  "domain": ".jianying.com", "path": "/",
                                  "httpOnly": True, "secure": True}])
                page = ctx.new_page()
                page.set_default_timeout(15000)
                caught: list[dict] = []

                def on_resp(r):
                    """列表加载瞬间会把音色数组下发给前端 —— 这个响应才是根。

                    每轮都挂监听，成功那轮自然会把接口和 body 一起留下。
                    """
                    if r.request.resource_type not in ("xhr", "fetch"):
                        return
                    try:
                        body = r.text()
                    except Exception:
                        return
                    if len(body) < 60 or len(body) > 3_000_000:
                        return
                    if not any(k in body for k in ("voice", "音色", "timbre", "speaker")):
                        return
                    caught.append({"url": r.url.split("?")[0], "len": len(body),
                                   "body": body[:400_000]})

                page.on("response", on_resp)
                page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5500)
                for _ in range(3):
                    page.evaluate(KILL)
                    page.wait_for_timeout(400)
                if not page.evaluate(OPEN_AUDIO):
                    print(f"[{stamp}] {attempt}: 打不开创作类型", flush=True)
                    b.close(); time.sleep(args.interval); continue
                page.wait_for_timeout(1200)
                page.evaluate(PICK_AUDIO)
                page.wait_for_timeout(4000)
                page.locator('button:has-text("音色库")').first.click(force=True, timeout=8000)
                page.wait_for_timeout(3000)
                pos = page.evaluate(TAB_POS)
                if not pos:
                    print(f"[{stamp}] {attempt}: 无音色库 tab", flush=True)
                    b.close(); time.sleep(args.interval); continue
                page.mouse.click(pos[0], pos[1])      # 必须坐标点击
                page.wait_for_timeout(4000)
                n = page.evaluate(
                    "() => document.querySelectorAll('.voice-grid-cell-label-RnMfVo').length")
                if n < 10:
                    if caught:
                        path = Path("/tmp/voice_api.json")
                        path.write_text(json.dumps(caught, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
                        print(f"[{stamp}] {attempt}: 列表 {n} 项，但捕获 {len(caught)} 个"
                              f"含音色的响应 → {path}", flush=True)
                        for c in caught[:3]:
                            print(f"    {c['len']:>8d}B {c['url'][-60:]}", flush=True)
                        b.close()
                        return 0
                    print(f"[{stamp}] {attempt}: 列表 {n} 项（仍限流）", flush=True)
                    b.close(); time.sleep(args.interval); continue
                got = page.evaluate(EXTRACT)
                b.close()
                if not got:
                    print(f"[{stamp}] {attempt}: {n} 项但提取失败", flush=True)
                    time.sleep(args.interval); continue
                if caught:
                    Path("/tmp/voice_api.json").write_text(
                        json.dumps(caught, ensure_ascii=False, indent=1), encoding="utf-8")
                    print(f"  已存 {len(caught)} 个音色相关接口响应 → /tmp/voice_api.json",
                          flush=True)
                rows = got.get("rows") or []
                mapping = {}
                for r in rows:
                    name, vid = pick_name(r), pick_id(r)
                    if name and vid:
                        mapping[name] = vid
                print(f"[{stamp}] {attempt}: {n} 项，解析出 {len(mapping)} 个 name→id", flush=True)
                if len(mapping) < 5:
                    raw = Path("/tmp/voice_raw_extract.json")
                    raw.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
                    print(f"  字段名不匹配，原始数据已存 {raw}", flush=True)
                    print(f"  首条样例: {json.dumps(rows[0] if rows else {}, ensure_ascii=False)[:300]}",
                          flush=True)
                    return 3
                merged = {}
                if OUT.exists():
                    try:
                        merged = json.loads(OUT.read_text(encoding="utf-8"))
                    except ValueError:
                        merged = {}
                merged.update(mapping)
                OUT.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"  ✅ 已写入 {OUT}，合计 {len(merged)} 个。重启服务生效。", flush=True)
                return 0
        except Exception as exc:                       # noqa: BLE001
            print(f"[{stamp}] {attempt}: ERR {type(exc).__name__} {str(exc)[:70]}", flush=True)
        time.sleep(args.interval)
    print("用完轮数仍未恢复", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
