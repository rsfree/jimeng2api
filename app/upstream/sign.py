#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""即梦请求签名 —— 纯算法，零浏览器。

    sign = md5("9e2c|" + pathname[-7:] + "|" + pf + "|" + appvr
               + "|" + unix秒 + "|" + tdid + "|11ac")

三个必须同时成立的点，任错一个都是 100% 不通：

1. `pathname[-7:]` 取的是**路径最后 7 个字符**（不是能力名、不是路径段）；
   `/mweb/v1/aigc_draft/generate` → `enerate`，`/mweb/v1/get_common_config`
   → `_config`。
2. `device-time` 必须等于参与签名的那一秒 —— 签名和这个头是同一个原子的两半。
3. `pf` / `appvr` 与请求头一致（当前站点仍是 7 / 8.4.0）。

sign 本身**不参与鉴权**（2026-09-18 负对照：不带也能 ret=0），但 `/commerce/v1/*`
实测**校验它**（不带 → ret=1014 system busy）。纯 MD5 零成本，所以所有请求一律带。

`SELF_TEST_VECTORS` 是真实抓包向量：最后一条是 2026-11-18 本机浏览器实测，
证明站点改版到 da_version 3.3.40 之后算法未变。
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlsplit

APPID = "513695"
APPVR = "8.4.0"
PF = "7"
SIGN_VER = "1"
DA_VERSION = "3.3.40"
WEB_VERSION = "7.5.0"
APP_SDK_VERSION = "48.0.0"

_SIGN_PREFIX = "9e2c"
_SIGN_SUFFIX = "11ac"
_TAIL_LEN = 7


def sign_payload(pathname: str, *, ts: int, pf: str = PF, appvr: str = APPVR,
                 tdid: str = "") -> str:
    return (f"{_SIGN_PREFIX}|{pathname[-_TAIL_LEN:]}|{pf}|{appvr}|{ts}"
            f"|{tdid}|{_SIGN_SUFFIX}")


def sign_headers(url_or_path: str, *, ts: int | None = None,
                 pf: str = PF, appvr: str = APPVR, tdid: str = "") -> dict[str, str]:
    pathname = urlsplit(url_or_path).path or "/"
    ts = int(time.time()) if ts is None else int(ts)
    sig = hashlib.md5(
        sign_payload(pathname, ts=ts, pf=pf, appvr=appvr, tdid=tdid).encode()
    ).hexdigest().lower()
    return {
        "sign": sig,
        "sign-ver": SIGN_VER,
        "device-time": str(ts),
        "pf": pf,
        "appvr": appvr,
        "tdid": tdid,
    }


#: (pathname, device-time, sign)。最后一条为 2026-11-18 实测（站点 3.3.40）。
SELF_TEST_VECTORS: tuple[tuple[str, int, str], ...] = (
    ("/mweb/v1/aigc_draft/generate", 1789742348, "103979f37bb61af3a5a6d0a53f9e5992"),
    ("/mweb/v1/get_history_by_ids", 1789742352, "a6def80120c292743fa98f189898e112"),
    ("/mweb/v1/aigc_draft/generate", 1789744838, "c735ac6365447728451e5ff3cb15244d"),
    ("/mweb/v1/get_history_by_ids", 1789744874, "571a5ba7496c5303a26bcc0d950ad017"),
    ("/mweb/v1/get_upload_token", 1789746355, "166eea62cb5dfc9c50fbcd5382d194fe"),
    ("/mweb/v1/get_common_config", 1795014070, "192451e631689e8e07955c9525b86467"),
)


def verify_vectors() -> list[str]:
    bad: list[str] = []
    for path, ts, expect in SELF_TEST_VECTORS:
        got = sign_headers(path, ts=ts)["sign"]
        if got != expect:
            bad.append(f"{path}@{ts}: 期望 {expect}，实得 {got}")
    return bad


if __name__ == "__main__":  # pragma: no cover
    failed = verify_vectors()
    if failed:
        raise SystemExit("签名自检失败：\n  " + "\n  ".join(failed))
    print(f"签名自检通过（{len(SELF_TEST_VECTORS)} 条真实抓包向量）")
