#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音合成北向入口：`POST /v1/audio/speech`（OpenAI 形状）。

与 `/v1/images/*` **共用认证与账号池**，但错误信封是完整四键：

    {"error": {"message": "...", "type": "...", "param": null, "code": null}}

为什么不复用全局 `error_body`：图片族已经在生产上跑，它的 `error` 只有
`code`/`message` 两个键，且有测试对形状做严格相等断言。给音频端点一套
完整的 OpenAI 信封，比改既有契约安全。

为什么手动解析而不是用 pydantic 参数：FastAPI 的 `RequestValidationError`
会被全局处理器接住并输出旧形状，且拿不到「是哪个字段错了」。这里自己读
body，才能把 `param` 精确指到 `speed` / `voice` / `response_format`。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import repo, service
from .security import require_key as _require_key
from .upstream import tts

log = logging.getLogger("audio")

router = APIRouter()

#: JSON 体积上限（解析前拦截）。
MAX_BODY_BYTES = 1 * 1024 * 1024

#: 北向 `error.type` 取值。
_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    504: "api_error",
}

#: 本部署错误码 → OpenAI 标准 code（无对应标准码时为 None）。
_CODES = {
    400: None,
    401: "invalid_api_key",
    403: None,
    404: None,
    413: "request_too_large",
    429: "rate_limit_exceeded",
    500: None,
    502: None,
    504: None,
}


def _error(status: int, message: str, *, param: str | None = None,
           code: str | None = "__default__") -> JSONResponse:
    """唯一的错误出口。`param` 缺省写 null，符合 OpenAI 形状。"""
    if code == "__default__":
        code = _CODES.get(status)
    body = {"error": {
        "message": message,
        "type": _TYPES.get(status, "api_error"),
        "param": param,
        "code": code,
    }}
    headers = {"Retry-After": "30"} if status == 429 else None
    return JSONResponse(status_code=status, content=body, headers=headers)


def _pick_runtime():
    """按现有调度策略挑一个账号；都不健康时退化为「第一个启用的」。"""
    cfg = repo.runtime_config()
    cands = repo.dispatch_candidates(int(cfg["per_account"]),
                                     int(cfg["cooldown_risk"]))
    if not cands:
        cands = [a for a in repo.list_accounts() if a["enabled"]]
    return cands[0] if cands else None


@router.post("/v1/audio/speech")
async def speech(request: Request):
    """文本 → 音频字节。同步返回，不排队、不轮询。

    认证**手动调用**而不是 `Depends`：依赖抛出的 401 会被全局 handler 接住，
    输出的是图片族的两键信封（只有 code/message），拿不到 `type`/`param`。
    这里自己接，保证语音端点始终是完整 OpenAI 形状。
    认证必须早于体积预检与解析（skill §8）。
    """
    try:
        _key = _require_key(request)
    except HTTPException as exc:
        return _error(
            exc.status_code, str(exc.detail),
            code="invalid_api_key" if exc.status_code == 401 else None)

    # ---- 体积预检（解析前）
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(413, f"请求体超过 {MAX_BODY_BYTES} 字节上限")

    # ---- 解析
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _error(400, "请求体必须是 JSON 对象")

    # ---- 未知字段：先映射，后拒绝（一个都不静默吞掉）
    allowed = {"model", "input", "voice", "response_format", "speed",
               "instructions", "stream_format", "sample_rate"}
    for key in body:
        if key not in allowed:
            return _error(400, f"不支持的字段 {key!r}", param=key)

    # ---- 声明的「不支持」必须显式拒绝，不能收下不用
    if body.get("instructions") not in (None, ""):
        return _error(400, "本部署不支持 instructions", param="instructions")
    if body.get("stream_format") not in (None, "", "audio"):
        return _error(400, "本部署只支持 stream_format=audio", param="stream_format")

    # ---- 字段校验（失败时 param 精确指向字段）
    try:
        model = tts.resolve_model(body.get("model") or "")
        text = tts.check_text(body.get("input") or "")
        voice = body.get("voice")
        voice_id = tts.resolve_voice(tts.DEFAULT_VOICE if voice in (None, "") else voice)
        fmt, _media = tts.resolve_format(body.get("response_format") or tts.DEFAULT_FORMAT)
        speed = body.get("speed", 1.0)
        tts.speed_to_rate(speed)                       # 只做校验
        sr = body.get("sample_rate", tts.DEFAULT_SAMPLE_RATE)
        if int(sr) not in tts.SAMPLE_RATES:
            return _error(400,
                          f"不支持的 sample_rate {sr!r}，可用：{list(tts.SAMPLE_RATES)}",
                          param="sample_rate")
    except tts.TtsParamError as exc:
        return _error(400, str(exc), param=getattr(exc, "param", None))

    # ---- 选号
    account = await run_in_threadpool(_pick_runtime)
    if account is None:
        return _error(502, "账号池无可用账号，请联系网关管理员")

    # ---- 合成（阻塞调用放线程池，不占事件循环）
    try:
        audio = await run_in_threadpool(
            service.speech,
            account_id=int(account["id"]), model=model, text=text,
            voice_id=voice_id, fmt=fmt, sample_rate=int(sr), speed=float(speed))
    except tts.TtsParamError as exc:
        return _error(400, str(exc), param=getattr(exc, "param", None))
    except tts.TtsUpstreamError as exc:
        # 限流 → 429（可退避），超时 → 504（结果未知），其它 → 502（服务方故障）
        if getattr(exc, "rate", False):
            status, msg = 429, "上游限流，请退避后重试"
        elif getattr(exc, "timeout", False):
            status, msg = 504, "语音合成超时，请稍后重试"
        else:
            status, msg = 502, "语音合成失败，请稍后重试"
        log.warning("TTS 上游失败：%s", exc)
        return _error(status, msg)
    except Exception:                                   # noqa: BLE001
        log.exception("TTS 未预期异常")
        return _error(500, "网关内部错误")

    return Response(
        content=audio.content,
        media_type=audio.media_type,
        headers={
            # 文件名扩展名只来自白名单，不拼上游返回值
            "Content-Disposition": f'inline; filename="speech.{audio.extension}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/v1/audio/capabilities")
def capabilities() -> dict:
    """本部署声明（免鉴权，便于调用方在启动时自检取值面）。"""
    caps = tts.capabilities()
    caps["endpoint"] = "/v1/audio/speech"
    caps["billing"] = "free"
    caps["upstream"] = "sync"
    caps["voice_count"] = len(tts.VOICES)
    return caps


@router.get("/v1/audio/voices")
def voices(gender: str = "", age: str = "", accent: str = "",
           category: str = "", style: str = "", q: str = "") -> dict:
    """音色清单 + 元数据，支持按维度筛选（免鉴权）。

    返回的 `data` 每条都带 `name` 与 `id`，可直接作为 `/v1/audio/speech`
    的 `voice` 传参（中文名或数字 ID 都行）。

    - `gender`   male / female / child / unknown
    - `age`      child / teen / young / adult / senior
    - `accent`   standard / dongbei / tianjin / … （方言音色）
    - `category` general / dialect / screen_role / anime_role / scenario
    - `style`    温柔 / 活泼 / 御姐 / 磁性 / 正统 / …
    - `q`        名字包含的关键字

    `facets` 给出各维度的取值与数量，客户端可直接用于渲染筛选器。
    """
    items = tts.list_voices(gender=gender, age=age, accent=accent,
                            category=category, style=style, q=q)
    return {
        "object": "list",
        "total": len(items),
        "data": items,
        "facets": tts.voice_facets(),
    }
