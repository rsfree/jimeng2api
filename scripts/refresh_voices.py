#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色表刷新：从 `/mweb/v1/feed` 提取全部音色 ID，写入 `data/voices.json`。

    uv run python scripts/refresh_voices.py            # 刷新并落盘
    uv run python scripts/refresh_voices.py --dry-run  # 只看数量

**原理（2026-11-25 实测确认）**：音色没有独立列表接口，它们混在首页推荐流的
`/mweb/v1/feed` 响应里，每条的特征是 `common_attr.effect_type == 209`，
`common_attr.id` 就是音色 ID，`common_attr.title` 是音色名。

实测一次能拿到 **208 条**（去重后 193 个中文名）。`活泼女声` 的 id
`7366933865698431515` 与 `tts_generate` 实际可用的 ID 完全一致，可交叉验证。

为什么不用 UI 点试听：虚拟列表要滚动 + 逐个点，一个音色 1 秒起步，而且上游会把
列表接口限流成 2 项。直接读 feed 一次 HTTP 就够。
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
HOME = "https://jimeng.jianying.com/ai-tool/home"
OUT = Path(__file__).resolve().parents[1] / "data" / "voices.json"

#: 音色在 feed 里的类型标记（实测值，勿改）
EFFECT_TYPE_VOICE = 209


def session_id() -> str:
    con = sqlite3.connect(settings.db_path)
    try:
        row = con.execute(
            "SELECT sessionid FROM accounts WHERE enabled=1 ORDER BY id LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit("账号池为空：先加一个账号")
    return row[0]


def collect() -> list[dict]:
    """打开首页，截获 feed 响应并抽取音色。"""
    feeds: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True,
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(viewport={"width": 1600, "height": 1000},
                            user_agent=UA, locale="zh-CN")
        ctx.add_cookies([{"name": "sessionid", "value": session_id(),
                          "domain": ".jianying.com", "path": "/",
                          "httpOnly": True, "secure": True}])
        page = ctx.new_page()

        def on_resp(r):
            if "/mweb/v1/feed" not in r.url:
                return
            try:
                feeds.append(r.text())
            except Exception:
                pass

        page.on("response", on_resp)
        page.goto(HOME, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8000)
        # 多滚两屏，让更多 feed 分页加载
        for _ in range(2):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(2500)
        b.close()

    found: list[dict] = []
    seen: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            ca = o.get("common_attr")
            if isinstance(ca, dict) and ca.get("effect_type") == EFFECT_TYPE_VOICE:
                vid = ca.get("id")
                title = (ca.get("title") or "").strip()
                if vid and title and str(vid) not in seen:
                    seen.add(str(vid))
                    found.append({"name": title, "id": str(vid)})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for raw in feeds:
        try:
            walk(json.loads(raw))
        except ValueError:
            continue
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    voices = collect()
    print(f"feed 中提取到 {len(voices)} 条（按 id 去重）")
    if not voices:
        print("没取到音色。检查：sessionid 是否有效、首页 feed 是否正常返回。",
              file=sys.stderr)
        return 2

    by_name: dict[str, str] = {}
    for v in voices:
        by_name.setdefault(v["name"], v["id"])
    print(f"按名字去重后 {len(by_name)} 个")

    if args.dry_run:
        for n, i in list(by_name.items())[:15]:
            print(f"  {n}  {i}")
        return 0

    existing = {}
    if OUT.exists():
        try:
            existing = json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            existing = {}
    existing.update(by_name)
    OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {OUT}（合计 {len(existing)} 条）。重启服务或调 "
          f"tts.reload_voices() 生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
