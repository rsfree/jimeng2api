#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填 `assets` 表：把历史成功任务的产物归属补进来。

    uv run python scripts/backfill_assets.py            # 只统计
    uv run python scripts/backfill_assets.py --apply    # 真写

为什么需要它：`assets` 是随「成功出图」增量写的，加表之前的任务没有归属记录。
那些 item_id 仍然有效（作品还在账号上），缺了归属就会在多号池里被随机打错号。
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")

from app import db, repo  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写入（默认只统计）")
    args = ap.parse_args()

    db.init()
    rows = db.query(
        "SELECT id, account_id, images FROM tasks "
        "WHERE status = 'succeeded' AND account_id IS NOT NULL "
        "ORDER BY finished_at DESC")
    total = with_item = written = 0
    for r in rows:
        total += 1
        try:
            images = json.loads(r["images"] or "[]")
        except ValueError:
            continue
        items = [i for i in images if isinstance(i, dict) and i.get("item_id")]
        if not items:
            continue
        with_item += 1
        if args.apply:
            written += repo.remember_assets(int(r["account_id"]), r["id"], items)

    print(f"成功任务 {total} 条，其中带 item_id 的 {with_item} 条")
    if args.apply:
        print(f"已写入 {written} 条归属记录")
    else:
        print("（dry-run：加 --apply 才会写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
