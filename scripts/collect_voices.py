#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色 ID 采集器：把「音色名 → 上游数字 ID」写进 `data/voices.json`。

    uv run python scripts/collect_voices.py            # 采集并合并
    uv run python scripts/collect_voices.py --dry-run  # 只看能采到哪些，不落盘

**为什么需要它**：上游没有音色列表接口，ID 只能从「点一次试听 → 抓
`POST /mweb/v1/tts_generate` 的 `id_info.id`」里拿。前端音色库是虚拟列表，
所以要滚动 + 逐个点击。

已知限制（2026-11-25 05:00 实测）：音色库当前只渲染「我的音色」的两个入口，
`全部音色` 为空 —— 此时脚本会明确报错退出，而不是写一个空表覆盖已有数据。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.config import settings  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
OUT = Path(__file__).resolve().parents[1] / "data" / "voices.json"

#: 清营销弹窗。⚠️ 进音色库之后**不能再执行** —— 弹窗本身也是 .lv-modal-wrapper。
KILL = """() => {
  document.querySelectorAll('.lv-modal-wrapper, .lv-modal-mask, .arco-modal-mask')
    .forEach(e => e.remove());
  document.body.style.overflow = 'auto';
  return 1;
}"""


def session_id() -> str:
    con = sqlite3.connect(settings.db_path)
    try:
        row = con.execute(
            "SELECT sessionid FROM accounts WHERE enabled=1 ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit("账号池为空：先加一个账号再采音色")
    return row[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--max", type=int, default=300, help="最多采集多少个（防失控）")
    args = ap.parse_args()

    found: dict[str, str] = {}
    pending: list[str] = []

    def on_request(r):
        if "tts_generate" in r.url and r.method == "POST" and r.post_data:
            try:
                body = json.loads(r.post_data)
            except ValueError:
                return
            vid = (body.get("id_info") or {}).get("id") or ""
            if vid:
                pending.append(vid)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  user_agent=UA, locale="zh-CN")
        ctx.add_cookies([{"name": "sessionid", "value": session_id(),
                          "domain": ".jianying.com", "path": "/",
                          "httpOnly": True, "secure": True}])
        page = ctx.new_page()
        page.on("request", on_request)

        page.goto("https://jimeng.jianying.com/ai-tool/home",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6000)
        for _ in range(3):                       # 只在这里清，之后绝不再清
            page.evaluate(KILL)
            page.wait_for_timeout(400)

        page.evaluate(
            """() => { const el=[...document.querySelectorAll('[role=combobox], button')]
                 .find(e => (e.innerText||'').includes('创作类型')
                         || (e.getAttribute('aria-label')||'').includes('创作类型'));
                 el && el.click(); }""")
        page.wait_for_timeout(1200)
        page.evaluate(
            """() => { const it=[...document.querySelectorAll(
                 '[role=menuitem], [role=option], li, [class*=menu-item]')]
                 .find(e => (e.innerText||'').trim() === '音频生成');
                 it && it.click(); }""")
        page.wait_for_timeout(4500)
        page.locator('button:has-text("音色库")').first.click(force=True, timeout=8000)
        page.wait_for_timeout(4000)

        for _ in range(60):
            names = page.evaluate(
                """() => [...document.querySelectorAll('.voice-grid-cell-label-RnMfVo')]
                     .map(e => (e.innerText||'').trim()).filter(Boolean)""")
            todo = [n for n in names if n not in found and len(found) < args.max]
            if not todo:
                moved = page.evaluate(
                    """() => { const g = document.querySelector('[class*=voice-grid]');
                         if (!g) return false;
                         const before = g.scrollTop;
                         g.scrollTop = before + g.clientHeight * 0.8;
                         return g.scrollTop !== before; }""")
                page.wait_for_timeout(1200)
                if not moved:
                    break
                continue
            for name in todo:
                pending.clear()
                ok = page.evaluate(
                    """(nm) => {
                         const lab = [...document.querySelectorAll(
                           '.voice-grid-cell-label-RnMfVo')]
                           .find(e => (e.innerText||'').trim() === nm);
                         const cell = lab && lab.closest('[class*=voice-grid-cell-content]');
                         const btn = cell && cell.querySelector('button');
                         if (!btn) return false;
                         btn.click(); return true; }""", name)
                if not ok:
                    continue
                for _ in range(20):              # 等这一条请求出现
                    if pending:
                        break
                    page.wait_for_timeout(150)
                if pending:
                    found[name] = pending[-1]
            print(f"  已采 {len(found)} 个 …", flush=True)

        browser.close()

    if len(found) < 2:
        print("没有采到音色。原因通常是音色库只渲染了「我的音色」入口：\n"
              "  1) 确认 UI 里「全部音色」标签下能看到卡片；\n"
              "  2) 上游对音色库有灰度/开关，非账号问题。\n"
              "未写入文件（避免空表覆盖）。", file=sys.stderr)
        return 2

    print(f"\n采到 {len(found)} 个音色")
    if args.dry_run:
        for name, vid in list(found.items())[:20]:
            print(f"  {name}  {vid}")
        return 0

    merged: dict[str, str] = {}
    if OUT.exists():
        try:
            merged = json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            merged = {}
    merged.update(found)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {OUT}（合计 {len(merged)} 个）。重启服务或调用 tts.reload_voices() 生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
