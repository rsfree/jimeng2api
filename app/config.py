#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程级配置：只从环境变量读，启动后不变。

面板可改的那部分（并发、轮询间隔、默认模型…）住在数据库 `settings` 表，
见 `app/repo.py::runtime_config` —— 两者刻意分开：环境变量属于部署形态，
运行期参数属于运营调参，混在一起会让"为什么重启后变了"变成谜。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # ---- 服务 ----
    host: str
    port: int
    db_path: str
    data_dir: str
    log_level: str
    # ---- 引导 ----
    #: 首次启动时自动建的管理员 Key；留空则随机生成并写到 data/first_boot_key.txt
    bootstrap_admin_key: str
    #: 面板 / 业务接口是否共用一个 Bearer 空间（默认是）
    require_auth: bool
    # ---- 调度 ----
    coordinator_enabled: bool
    # ---- 上游站点 ----
    base_url: str
    site_origin: str
    aid: str
    da_version: str
    web_version: str
    app_sdk_version: str
    appvr: str
    pf: str
    #: 可选的 secsdk 运行时头（浏览器抓包才有，正常不需要）；为空则不带
    secsdk_signature: str
    secsdk_expire: str
    user_agent: str
    # ---- 网络 ----
    http_timeout: float
    #: 出站代理，如 http://127.0.0.1:7890；空 = 直连
    http_proxy: str

    @property
    def version(self) -> str:
        return "0.1.0"


def load() -> Settings:
    data_dir = _env("JIMENG2API_DATA", str(ROOT / "data"))
    db_path = _env("JIMENG2API_DB", str(Path(data_dir) / "jimeng2api.db"))
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    return Settings(
        host=_env("JIMENG2API_HOST", "127.0.0.1"),
        port=_int("JIMENG2API_PORT", 8300),
        db_path=db_path,
        data_dir=data_dir,
        log_level=_env("JIMENG2API_LOG_LEVEL", "INFO").upper(),
        bootstrap_admin_key=_env("JIMENG2API_ADMIN_KEY"),
        require_auth=_bool("JIMENG2API_REQUIRE_AUTH", True),
        coordinator_enabled=_bool("JIMENG2API_COORDINATOR", True),
        base_url=_env("JIMENG_BASE", "https://jimeng.jianying.com"),
        site_origin=_env("JIMENG_ORIGIN", "https://jimeng.jianying.com"),
        aid=_env("JIMENG_AID", "513695"),
        da_version=_env("JIMENG_DA_VERSION", "3.3.40"),
        web_version=_env("JIMENG_WEB_VERSION", "7.5.0"),
        app_sdk_version=_env("JIMENG_APP_SDK_VERSION", "48.0.0"),
        appvr=_env("JIMENG_APPVR", "8.4.0"),
        pf=_env("JIMENG_PF", "7"),
        secsdk_signature=_env("JIMENG_SECSDK_SIGNATURE"),
        secsdk_expire=_env("JIMENG_SECSDK_EXPIRE"),
        user_agent=_env(
            "JIMENG_UA",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/157.0.0.0 Safari/537.36",
        ),
        http_timeout=_float("JIMENG2API_HTTP_TIMEOUT", 45.0),
        http_proxy=_env("JIMENG2API_HTTP_PROXY"),
    )


settings = load()
