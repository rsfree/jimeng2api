#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词增强北向入口：`POST /v1/prompts/enhance`。

免费、同步（内部轮询约 5–15 秒），把一句短提示词扩写成结构化脚本。
错误信封与 `/v1/audio/*` 一致（完整四键），与图片族的两键信封刻意隔离。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import repo, service
from .security import require_key as _require_key
from .upstream import prompt_enhance, subject as subject_mod, video

log = logging.getLogger("prompt")

router = APIRouter()

MAX_BODY_BYTES = 256 * 1024
MAX_PROMPT_CHARS = 2000

_TYPES = {400: "invalid_request_error", 401: "authentication_error",
          404: "not_found_error", 413: "invalid_request_error",
          429: "rate_limit_error", 500: "api_error", 502: "api_error",
          504: "api_error"}
_CODES = {400: None, 401: "invalid_api_key", 404: None,
          413: "request_too_large", 429: "rate_limit_exceeded",
          500: None, 502: None, 504: None}


def _error(status: int, message: str, *, param: str | None = None):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status, content={"error": {
        "message": message, "type": _TYPES.get(status, "api_error"),
        "param": param, "code": _CODES.get(status)}})


def _pick_runtime():
    cfg = repo.runtime_config()
    cands = repo.dispatch_candidates(int(cfg["per_account"]),
                                     int(cfg["cooldown_risk"]))
    if not cands:
        cands = [a for a in repo.list_accounts() if a["enabled"]]
    return cands[0] if cands else None


@router.post("/v1/prompts/enhance")
async def enhance(request: Request):
    """短提示词 → 结构化导演脚本。

    请求体：

        {"prompt": "一只猫在草地上奔跑",
         "model": "jimeng-video",        // 可选，决定用哪个视频模型做增强上下文
         "ratio": "16:9",                // 可选
         "resolution": "720p",           // 可选
         "duration": 5,                  // 可选，秒
         "context": "T2V"}               // 可选，目前只支持 T2V

    响应：`{"original", "enhanced", "pe_task_id", "credits": 0}`
    """
    try:
        _key = _require_key(request)
    except HTTPException as exc:
        return _error(exc.status_code, str(exc.detail),
                      code=None) if exc.status_code != 401 else _error(
            401, str(exc.detail))

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(413, f"请求体超过 {MAX_BODY_BYTES} 字节上限")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _error(400, "请求体必须是 JSON 对象")

    allowed = {"prompt", "model", "ratio", "resolution", "duration",
               "context", "target"}
    for k in body:
        if k not in allowed:
            return _error(400, f"不支持的字段 {k!r}", param=k)

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return _error(400, "prompt 不能为空", param="prompt")
    if len(prompt) > MAX_PROMPT_CHARS:
        return _error(400, f"prompt 超过 {MAX_PROMPT_CHARS} 字符", param="prompt")

    # 模型只用来取上游 model_req_key，不参与计费
    model_name = (body.get("model") or "jimeng-video").strip()
    vm = video.get(model_name)
    if vm is None:
        return _error(400, f"不支持的 model {model_name!r}，可用："
                      f"{[m.key for m in video.MODELS.values() if not m.experimental]}",
                      param="model")

    target = (body.get("target") or "video").strip().lower()
    if target not in ("video", "image"):
        # 上游只服务视频；image 是本网关的裁剪封装，不是上游能力
        return _error(400, "target 只支持 video / image", param="target")

    context = (body.get("context") or "T2V").strip().upper()
    if context not in prompt_enhance.CONTEXTS:
        return _error(400, f"不支持的 context {context!r}，可用："
                      f"{list(prompt_enhance.CONTEXTS)}", param="context")

    ratio = (body.get("ratio") or "16:9").strip()
    if ratio not in video.ASPECT_RATIOS:
        return _error(400, f"不支持的 ratio {ratio!r}", param="ratio")
    resolution = (body.get("resolution") or "720p").strip()
    if resolution not in video.RESOLUTIONS:
        return _error(400, f"不支持的 resolution {resolution!r}", param="resolution")
    try:
        duration = float(body.get("duration", 5))
    except (TypeError, ValueError):
        return _error(400, "duration 必须是数字", param="duration")
    if not (4 <= duration <= 15):
        return _error(400, "duration 需在 4–15 秒之间", param="duration")

    account = await run_in_threadpool(_pick_runtime)
    if account is None:
        return _error(502, "账号池无可用账号，请联系网关管理员")

    try:
        out = await run_in_threadpool(
            service.enhance_prompt,
            account_id=int(account["id"]), prompt=prompt,
            model_req_key=vm.model_req_key, context=context, ratio=ratio,
            resolution=resolution, duration_ms=int(duration * 1000))
    except prompt_enhance.EnhanceError as exc:
        status = 504 if getattr(exc, "timeout", False) else 502
        log.warning("提示词增强失败：%s", exc)
        return _error(status, "提示词增强失败，请稍后重试")
    except Exception:                                   # noqa: BLE001
        log.exception("提示词增强未预期异常")
        return _error(500, "网关内部错误")

    # target=image：用同一次上游增强的结果，裁掉音乐/情节/声音等视频专属段落。
    # 注意这**不是**上游的图片增强能力 —— 图片侧没有 `support_prompt_enhancement`，
    # 这里只是把视频脚本的视觉部分抽出来，供图片 prompt 复用。
    if target == "image":
        out = {**out, "enhanced_video": out["enhanced"],
               "enhanced": prompt_enhance.to_image_prompt(out["enhanced"])}
    return {**out, "target": target, "credits": 0}


@router.get("/v1/subjects")
def list_subjects(cursor: int = 0, limit: int = 20, source: str = "mine"):
    """列出主体（角色/商品），用于在生成时复用同一形象。

    只读、免费。上游的 `create` 需要参考图并走独立的生成流程，
    未经验证不开放 —— 这里只提供查询。
    """
    account = _pick_runtime()
    if account is None:
        return _error(502, "账号池无可用账号，请联系网关管理员")
    try:
        out = service.list_subjects(account_id=int(account["id"]),
                                    cursor=cursor, limit=limit, source=source)
    except subject_mod.SubjectError as exc:
        return _error(400, str(exc), param=getattr(exc, "param", None))
    except Exception:                                   # noqa: BLE001
        log.exception("主体列表未预期异常")
        return _error(500, "网关内部错误")
    return {"object": "list", **out}
