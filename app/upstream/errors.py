#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游错误分类 —— 决定"重不重试、要不要换号、要不要冷却"。

码表来自 rsfree/jimeng 的取证（89 条全表），这里只保留**分类**，
不保留逐条文案：文案会变，语义决定调度行为。
"""
from __future__ import annotations

#: ret → (kind, retryable)
#: kind: rate|quota|risk|content|param|auth|unknown
_CODES: dict[int, tuple[str, bool]] = {
    # 限流：退避后可重试
    1: ("rate", True), 1010: ("rate", True), 1057: ("rate", True),
    2014: ("rate", True), 2020: ("rate", True), 10020: ("rate", True),
    # 额度：重试无效，换号才有意义
    1006: ("quota", False), 4001: ("quota", False), 121101: ("quota", False),
    # 风控：重试会加剧，必须冷却
    1018: ("risk", False), 1019: ("risk", False), 1021: ("risk", False),
    2035: ("risk", False), 2038: ("risk", False), 2039: ("risk", False),
    2041: ("risk", False), 2042: ("risk", False), 2043: ("risk", False),
    # 内容审核 / 版权
    1063: ("content", False), 1159: ("content", False), 2003: ("content", False),
    2004: ("content", False), 2005: ("content", False), 2048: ("content", False),
    2050: ("content", False),
    # 参数 / 业务体
    1000: ("param", False), 1001: ("param", False), 1002: ("param", False),
    1161: ("param", False), 1162: ("param", False), 3021: ("param", False),
    4003: ("param", False), 4010: ("param", False), 2008: ("param", False),
    # 凭据
    1015: ("auth", False),
}

#: 签名失败（ErrSign）—— 与"登录失效"不同，别把账号标记成失效
ERR_SIGN = 1014

MESSAGES: dict[str, str] = {
    "rate": "上游限流（并发或频率超限），已进入退避",
    "quota": "账号积分/额度不足",
    "risk": "上游风控拦截，账号需冷却",
    "content": "内容审核未通过（换 prompt 或换图）",
    "param": "请求参数不被上游接受",
    "auth": "登录态失效（sessionid 无效或已过期）",
    "network": "网络错误，结果未知",
    "timeout": "上游超时，结果未知",
    "unknown": "上游返回未知错误",
}


class JimengError(RuntimeError):
    """所有上游异常的基类。`kind` 是调度依据，`ret` 是上游原始码。"""

    kind = "unknown"
    retryable = False
    account_fault = False

    def __init__(self, message: str, *, ret: int | None = None,
                 kind: str | None = None, retryable: bool | None = None,
                 account_fault: bool | None = None) -> None:
        super().__init__(message)
        self.ret = ret
        if kind:
            self.kind = kind
        if retryable is not None:
            self.retryable = retryable
        if account_fault is not None:
            self.account_fault = account_fault


class JimengAuthError(JimengError):
    kind = "auth"
    account_fault = True


class JimengRateLimitError(JimengError):
    kind = "rate"
    retryable = True


class JimengQuotaError(JimengError):
    kind = "quota"
    account_fault = True


class JimengRiskError(JimengError):
    kind = "risk"
    account_fault = True


class JimengContentError(JimengError):
    kind = "content"


class JimengParamError(JimengError):
    kind = "param"


class JimengSignError(JimengError):
    kind = "sign"


class JimengNetworkError(JimengError):
    kind = "network"


_CLS = {
    "auth": JimengAuthError, "rate": JimengRateLimitError,
    "quota": JimengQuotaError, "risk": JimengRiskError,
    "content": JimengContentError, "param": JimengParamError,
    "sign": JimengSignError, "network": JimengNetworkError,
}


def classify(ret: int | None) -> tuple[str, bool]:
    if ret is None:
        return "unknown", False
    if ret == ERR_SIGN:
        return "sign", False
    return _CODES.get(int(ret), ("unknown", False))


def raise_for_ret(ret_raw: object, payload: dict) -> None:
    """`ret != 0` 时抛 typed error。`ret` 上游是字符串，统一转 int。"""
    try:
        ret = int(str(ret_raw))
    except (TypeError, ValueError):
        ret = None
    if ret == 0:
        return
    kind, retryable = classify(ret)
    msg = payload.get("errmsg") or payload.get("message") or ""
    cls = _CLS.get(kind, JimengError)
    detail = f"{MESSAGES.get(kind, '上游错误')}（ret={ret}{', ' + str(msg) if msg else ''}）"
    raise cls(detail, ret=ret, kind=kind, retryable=retryable)
