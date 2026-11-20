#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端面错误：标准码 + 固定文案。原生错误只留在服务端。

一条线的分层，不是两套逻辑：

    客户端 ← {"error": {"code": <稳定码>, "message": <固定文案>}}
    服务端 ← tasks.error（上游原文）、upstream_logs（ret / errmsg / excerpt）、
             tasks.degradations（"请求了 A、实际做 B"）、面板 /panel/api/*

为什么必须分开：

* 原生报文里带 `ret=1015`、上游 errmsg、甚至带签名的对象存储 URL ——
  贴给一个没见过这个渠道的人，他就能认出上游是哪一家（skill §6.4）。
* `kind` / `ret` 是我们的调度依据，不是客户端的行动依据。把它们露出去，
  客户端会写出依赖内部枚举的分支，之后我们连分类都不能改。
* `degradations` 描述的是吸附过程（模型档位、比例槽），属于运营细节；
  客户端看到只会当成错误反复反馈。

**归因按「谁的错」翻，不按上游的码照抄**（skill §6.3）：
服务方的凭证/额度问题对客户端是 502，不是 401/400 —— 客户端没写错，
不该被告知去改请求。
"""
from __future__ import annotations

from .upstream.errors import JimengError, JimengParamError

#: 客户端可见的稳定错误码。**只加不改**：改名等于破坏已接入的客户端。
PUBLIC_ERROR_CODES = (
    "invalid_request",        # 客户端参数错（本地校验 / 上游拒收 / 审核）
    "invalid_api_key",        # 缺失或无效的 Bearer
    "insufficient_quota",     # 该 Key 的额度用尽
    "permission_denied",      # Key 无权执行该操作
    "not_found",              # 资源不存在（含"不属于你"的任务）
    "rate_limited",           # 上游限流，可退避重试
    "executor_unavailable",   # 执行器不健康
    "upstream_error",         # 服务方上游故障
    "internal_error",         # 网关自身异常
)

#: 上游 kind → (HTTP, code, 固定文案)。
#: 文案里**不出现** ret、上游名、账号、内部路径。
_UPSTREAM: dict[str, tuple[int, str, str]] = {
    "auth": (502, "upstream_error", "上游服务凭证失效，请联系网关管理员"),
    "sign": (502, "upstream_error", "上游协议校验失败，请联系网关管理员"),
    "quota": (502, "upstream_error", "上游额度不足，请联系网关管理员"),
    "rate": (429, "rate_limited", "上游限流，请稍后重试"),
    "risk": (502, "upstream_error", "上游风控拦截，请稍后重试"),
    "content": (400, "invalid_request", "内容未通过审核，请调整提示词或输入图"),
    "param": (400, "invalid_request", "请求参数不被上游接受"),
    "network": (502, "upstream_error", "上游连接失败，结果未知"),
    "timeout": (504, "upstream_error", "上游超时，结果未知"),
    "unknown": (502, "upstream_error", "上游返回未知错误"),
}

#: HTTP 状态 → 客户端码（本地鉴权/路由错误走这张表）。
HTTP_CODES: dict[int, str] = {
    400: "invalid_request", 401: "invalid_api_key", 402: "insufficient_quota",
    403: "permission_denied", 404: "not_found", 405: "invalid_request",
    409: "invalid_request", 422: "invalid_request", 429: "rate_limited",
    503: "executor_unavailable",
}


def public_error(exc: JimengError) -> tuple[int, str, str]:
    """上游/业务异常 → (HTTP, code, message)。

    唯一的例外：**本地校验**抛出的 `JimengParamError`（`ret is None`）
    可以原样带出 `message` —— 它描述的是客户端自己的输入，例如
    "size 需形如 2048x2048"，不含任何上游字段。带 `ret` 的同名异常来自
    上游，一律走固定文案。
    """
    if isinstance(exc, JimengParamError) and exc.ret is None:
        return 400, "invalid_request", str(exc)
    return _UPSTREAM.get(exc.kind or "unknown", _UPSTREAM["unknown"])


def error_body(code: str, message: str) -> dict:
    """客户端面唯一的错误信封。只有两个键 —— 不放 kind/ret/param。"""
    return {"error": {"code": code, "message": message}}
