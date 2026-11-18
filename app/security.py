#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鉴权：一把 Bearer 打通业务与面板（与 wb2api 的形态一致）。

* 业务 Key：`sk-` + 48 hex，库里只存 sha256 + 前缀；
* 面板 Key：同一张表，`is_admin=1`；首次启动自动生成并写文件（0600）；
* 明文 Key 只在创建那一次返回 —— 之后任何接口都拿不回来。

不做的：OAuth、JWT、按 IP 白名单。这是单机自建网关，
引入一套身份系统只会让"谁能用"更难审计。
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from . import repo
from .config import settings

log = logging.getLogger("jimeng2api.auth")


def extract_key(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _reject(detail: str, code: int = 401) -> None:
    raise HTTPException(status_code=code, detail=detail,
                        headers={"WWW-Authenticate": "Bearer"})


def require_key(request: Request) -> dict[str, Any]:
    raw = extract_key(request)
    if not raw:
        _reject("缺少 Bearer Key")
    key = repo.key_by_raw(raw)
    if not key:
        _reject("Key 无效")
    if not key["enabled"]:
        _reject("Key 已禁用", 403)
    quota = int(key.get("quota_credits") or 0)
    used = int(key.get("used_credits") or 0)
    if quota > 0 and used >= quota:
        _reject(f"Key 额度已用尽（{used}/{quota} 积分）", 402)
    # 面板每 5 秒轮询一次，若也算进 request_count，"请求数"会变成心跳计数
    if not request.url.path.startswith("/panel/"):
        repo.touch_key(key["id"])
    key["raw"] = raw
    return key


def require_admin(request: Request) -> dict[str, Any]:
    key = require_key(request)
    if not key.get("is_admin"):
        _reject("该 Key 没有管理权限", 403)
    return key


def bootstrap() -> str:
    """保证库里至少有一把管理员 Key。返回新生成的明文（已存在则空串）。"""
    if repo.admin_key_exists():
        return ""
    raw = settings.bootstrap_admin_key or ("sk-" + secrets.token_hex(24))
    repo.create_key("管理员", is_admin=True, key=raw)
    path = Path(settings.data_dir) / "first_boot_key.txt"
    try:
        path.write_text(
            "jimeng2api 管理员 Key（仅此一次明文展示）\n"
            f"{raw}\n\n"
            "面板登录、业务调用都用它；请立刻改存到你自己的密码管理器。\n",
            encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        log.warning("无法写 first_boot_key.txt")
    log.warning("=" * 68)
    log.warning("首次启动：管理员 Key = %s", raw)
    log.warning("已写入 %s（权限 600）", path)
    log.warning("=" * 68)
    return raw
