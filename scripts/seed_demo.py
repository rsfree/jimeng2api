#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一份**演示数据**（假账号、假任务、假日志），只用于本地看面板。

用法：
    JIMENG2API_DATA=/tmp/j2a-demo .venv/bin/python scripts/seed_demo.py
    JIMENG2API_PORT=8301 JIMENG2API_DATA=/tmp/j2a-demo .venv/bin/uvicorn ...

⚠️ 不要把它指向生产库：脚本会**清空** accounts / tasks / logs / api_keys。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

DEMO_KEY = "sk-demo-admin-0123456789abcdef"
BIZ_KEY = "sk-demo-biz-0123456789abcdef"


def sha(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def thumb(title: str, c1: str, c2: str) -> str:
    """内联 SVG 占位图，避免演示依赖外网。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/>'
        '</linearGradient></defs><rect width="512" height="512" fill="url(#g)"/>'
        f'<text x="50%" y="52%" text-anchor="middle" fill="rgba(255,255,255,.92)" '
        f'font-family="system-ui" font-size="34">{title}</text></svg>'
    )
    import base64
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def main() -> None:
    db.init()
    now = int(time.time())
    for table in ("accounts", "tasks", "api_keys", "request_logs", "upstream_logs"):
        db.execute(f"DELETE FROM {table}")

    # 演示用的能力表：数据是真的（2026-11-18 抓包），但没在本进程里现场读，
    # 所以 source 写明"演示回放"，不冒充实时。顺手关掉自动刷新，
    # 避免协调器拿假账号去读表、把 22 个模型降级成访客的 13 个。
    from app import models as models_mod
    snap = models_mod.from_snapshot()
    db.put_setting("models_cache", {
        "captured_at": int(time.time()),
        "source": "演示回放 · 2026-11-18 抓包",
        "models": [m.to_api() for m in snap.models],
    })
    db.put_setting("models_live_refresh", 0)

    db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, is_admin, quota_credits,"
        " used_credits, request_count, last_used_at, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("演示管理员", sha(DEMO_KEY), DEMO_KEY[:11], 1, 0, 0, 0, None, now - 86400 * 5))
    biz_key_id = db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, is_admin, quota_credits,"
        " used_credits, request_count, last_used_at, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("业务 Key / 测试", sha(BIZ_KEY), BIZ_KEY[:11], 0, 500, 128, 64,
         now - 900, now - 86400 * 3))

    accounts = [
        ("BettermeTry", "2928751148803262", "a" * 64, "主号 · 会员", 5740, "maestro", "ok", 0),
        ("小号 / 备用", "2928751148803999", "b" * 64, "测试号", 1200, "", "ok", 0),
        ("旧号（限流中）", "2928751148804111", "c" * 64, "上周频繁触发风控", 86, "", "cooling", now + 1800),
    ]
    account_ids: list[int] = []
    for idx, (name, uid, sid, remark, credits, member, status, cd) in enumerate(accounts):
        # 演示 sid_guard 解析出来的到期日：主号一年期，备用 20 天后到期
        expires = now + (300 if idx == 0 else 20) * 86400
        account_ids.append(db.execute(
            "INSERT INTO accounts(uid, name, sessionid, remark, enabled, status,"
            " credits, credit_detail, member_level, expires_at, cooldown_until,"
            " last_error, last_checked, last_used_at, success_count, fail_count,"
            " created_at, updated_at) VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, name, sid, remark, status, credits,
             json.dumps({"buckets": {"vip_credit": credits}}, ensure_ascii=False),
             member, expires, cd, "ret=1010 并发超限" if cd else "",
             now - 120, now - 300, 42, 3, now - 86400 * 6, now - 120)))

    prompts = [
        ("一只橘猫趴在窗台上，午后逆光，胶片质感", "high_aes_general_v55", "2048x2048", 4),
        ("赛博朋克雨夜街头，霓虹倒影，广角", "high_aes_general_v55", "2560x1440", 2),
        ("新中式茶室，晨雾，留白构图", "high_aes_general_v50p_large", "2304x1728", 1),
        ("宇航员在麦田里，超现实，暖色调", "high_aes_general_v55", "2048x2048", 4),
        ("极简产品图：白色耳机悬浮，柔光", "high_aes_general_v50_flash", "1728x2304", 2),
        ("海边灯塔，长曝光，冷色调", "high_aes_general_v43", "2560x1440", 3),
        ("水彩风插画：山谷里的小屋", "high_aes_general_v55", "2048x2048", 1),
        ("城市天际线，蓝调时刻，航拍", "high_aes_general_v50p_large", "3024x1296", 2),
    ]
    palettes = [("#1f2a44", "#5b7cfa"), ("#2b1f2e", "#f0655f"), ("#122a24", "#3ddc97"),
                ("#2a2418", "#f5b544"), ("#1a1f2e", "#8b5cf6")]
    tid = 0
    for day in range(6, -1, -1):
        base = now - day * 86400
        for i in range(random.randint(1, 3) if day else 4):
            tid += 1
            created = base - random.randint(200, 7200)
            failed = random.random() < 0.18
            prompt, model, size = random.choice(prompts)[:3]
            count = random.choice([1, 2, 4])
            status = "failed" if failed else "succeeded"
            started = created + 3 if status != "queued" else None
            finished = started + random.randint(12, 65) if started else None
            imgs = []
            if status == "succeeded":
                c1, c2 = random.choice(palettes)
                for k in range(count):
                    imgs.append({"url": thumb(f"{model.split('_')[-1]} · {k+1}", c1, c2),
                                 "width": 2048, "height": 2048, "format": "png",
                                 "uri": f"tos-cn-i-demo/{tid}-{k}", "item_id": f"it{tid}{k}"})
            db.execute(
                "INSERT INTO tasks(id, api_key_id, account_id, mode, model, prompt,"
                " size, width, height, resolution, count, image_uris, status,"
                " upstream_status, progress, images, error, error_kind,"
                " forecast_credits, charged_credits, attempts, created_at,"
                " started_at, finished_at, updated_at, next_poll_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (f"demo-{tid:04d}", biz_key_id,
                 account_ids[0] if i % 2 == 0 else account_ids[1], "t2i", model, prompt,
                 size, 2048, 2048, "2k", count, "[]", status,
                 50 if status == "succeeded" else 30,
                 f"{count}/{count}" if status == "succeeded" else "",
                 json.dumps(imgs, ensure_ascii=False),
                 "内容审核未通过（ret=2003）" if failed else "",
                 "content" if failed else "",
                 count, None if not failed else 0, 1, created, started, finished,
                 finished or created))

    for i in range(26):
        ts = now - i * 640
        ok = random.random() > 0.15
        db.execute(
            "INSERT INTO request_logs(ts, api_key_id, method, path, status,"
            " latency_ms, ip, ua) VALUES(?,?,?,?,?,?,?,?)",
            (ts, biz_key_id, "POST", "/v1/images/generations", 200 if ok else 502,
             random.randint(80, 900), "127.0.0.1",
             "curl/8.7.1" if i % 3 else "python-httpx/0.27"))
    for i in range(18):
        db.execute(
            "INSERT INTO upstream_logs(ts, account_id, path, http_status, ret, kind,"
            " latency_ms, error) VALUES(?,?,?,?,?,?,?,?)",
            (now - i * 900, account_ids[0] if i % 2 else account_ids[1],
             "/mweb/v1/aigc_draft/generate",
             200, "0" if i % 5 else "1010", "" if i % 5 else "rate",
             random.randint(120, 700),
             "" if i % 5 else "上游限流（并发或频率超限），已进入退避"))

    print("演示数据已写入", os.environ.get("JIMENG2API_DATA", "./data"))
    print("管理员 Key:", DEMO_KEY)
    print("业务 Key  :", BIZ_KEY)


if __name__ == "__main__":
    main()
