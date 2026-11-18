#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外业务接口（OpenAI 形态的图片生成 + 异步任务查询）。

    POST /v1/images/generations          同步：受理 + 等终态（≤ sync_timeout）
    POST /async/v1/images/generations    异步：立即返回 task_id
    GET  /async/v1/images/generations/{id}
    GET  /v1/tasks/{id}
    GET  /v1/models                      免鉴权的能力清单
    GET  /v1/credits                     池子余额（鉴权后可见）

鉴权：`Authorization: Bearer <sk-...>`；`/v1/models` 与探活端点例外。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db, pool as pool_mod, repo, service
from .config import settings
from .coordinator import coordinator
from .security import require_key
from .upstream.errors import JimengParamError

router = APIRouter()


class GenerateRequest(BaseModel):
    model: str = ""
    prompt: str = ""
    n: int = Field(default=1, ge=1, le=8)
    size: str = "2048x2048"
    resolution: str = ""
    negative_prompt: str = ""
    seed: int | None = None
    image: str | list[str] | None = None
    async_: bool = Field(default=False, alias="async")
    wait: float | None = None

    model_config = {"populate_by_name": True}


def task_view(task: dict) -> dict:
    return {
        "task_id": task["id"],
        "status": task["status"],
        "progress": task.get("progress") or "",
        "model": task.get("model"),
        "mode": task.get("mode"),
        "count": task.get("count"),
        "size": task.get("size"),
        "resolution": task.get("resolution"),
        "created": task.get("created_at"),
        "started": task.get("started_at"),
        "finished": task.get("finished_at"),
        "error": task.get("error") or "",
        "error_kind": task.get("error_kind") or "",
        "credits": {
            "forecast": task.get("forecast_credits"),
            "charged": task.get("charged_credits"),
        },
        "data": [{"url": i.get("url"), "width": i.get("width"),
                  "height": i.get("height"), "format": i.get("format")}
                 for i in (task.get("images") or [])],
    }


#: 内部任务状态 → 客户端词（image-adapter skill §3.2 的四词表）。
#: `submitting` / `queued` 都是"还没有像素产出"，客户端区分它们只会多写分支；
#: 上游词用 `failure` 而不是 `failed`，`succeeded` 而不是 `success` —— 这两个
#: 不对称的拼法是上游原样，写死是为了让客户端只认一套词汇。
ASYNC_STATUS = {
    "queued": "queued",
    "submitting": "queued",
    "running": "in_progress",
    "succeeded": "succeeded",
    "failed": "failure",
}


def async_items(task: dict) -> list[dict]:
    """可交付产物。判据与 skill §3.3 一致：**有 url 或 b64_json 才算**。

    `data: [{}]` 也是非空数组，但它什么也没产出 —— 只数长度会把一次空跑
    当成成功，客户端随后拿到一个点不开的链接。
    """
    out = []
    for item in (task.get("images") or []):
        if not isinstance(item, dict):
            continue
        if item.get("url") or item.get("b64_json"):
            out.append(item)
    return out


def async_status(task: dict) -> str:
    """终态成功但无产物 ⇒ 失败。规范不允许用空 data[] 冒充成功。"""
    status = ASYNC_STATUS.get(str(task.get("status") or ""), "queued")
    if status == "succeeded" and not async_items(task):
        return "failure"
    return status


def async_error(task: dict) -> dict:
    code = "task_failed"
    if str(task.get("status") or "") == "succeeded":
        code = "empty_result"          # 上游说成功却没有产物
    elif str(task.get("error_kind") or "") in ("param", "content"):
        code = "invalid_request"
    return {"code": code,
            "message": str(task.get("error") or "") or "任务失败"}


def async_view(task: dict) -> dict:
    """异步轮询的对外形状（image-adapter skill §3.2）。

    与面板的 `task_view` **刻意分开**：面板要 attempts / credits / error_kind
    这些运维字段，客户端只该看到协议面。合成一个函数的话，下一次改面板就会
    悄悄改掉对外契约 —— 而对外契约一旦漂移，只有调用方才发现。
    """
    status = async_status(task)
    body: dict[str, Any] = {
        "task_id": task["id"],
        # id 只在轮询响应里出现（创建响应只有 task_id），为按 id 解析的老客户端保留
        "id": task["id"],
        "object": "image.generation",
        "created": task.get("created_at"),
        "status": status,
    }
    if status in ("queued", "in_progress"):
        progress = str(task.get("progress") or "")
        if progress:
            body["progress"] = progress
        return body
    if status == "failure":
        body["error"] = async_error(task)
        return body
    items = []
    for i in async_items(task):
        # 只放有值的键：`{"url": null}` 在客户端看来是"给了一个空链接"，
        # 而协议的判据是"每项至少有一个 url 或 b64_json"—— null 不算有。
        item: dict[str, Any] = {}
        for k in ("url", "b64_json", "width", "height", "format"):
            if i.get(k) is not None:
                item[k] = i[k]
        items.append(item)
    body["data"] = items
    return body


def ok_response(task: dict, degradations: list[str] | None = None) -> dict:
    body = {
        "created": task.get("finished_at") or task.get("created_at"),
        "task_id": task["id"],
        "model": task.get("model"),
        "data": [{"url": i.get("url"), "width": i.get("width"),
                  "height": i.get("height"), "format": i.get("format")}
                 for i in (task.get("images") or [])],
        "credits": {"forecast": task.get("forecast_credits"),
                    "charged": task.get("charged_credits")},
    }
    if degradations:
        body["degradations"] = degradations
    if task.get("status") == "failed":
        body["error"] = task.get("error")
    return body


def _payload(req: GenerateRequest) -> dict:
    images: list[str] = []
    if isinstance(req.image, str):
        images = [req.image] if req.image else []
    elif isinstance(req.image, list):
        images = [x for x in req.image if x]
    model = service.resolve_model_name(req.model) if req.model else ""
    return {
        "model": model, "prompt": req.prompt, "size": req.size,
        "resolution": req.resolution, "count": req.n,
        "negative_prompt": req.negative_prompt, "seed": req.seed,
        "images": images,
    }


def executor_ready() -> bool:
    """没有执行器时**快速失败**，而不是让调用方白等一整个等待预算。

    排队任务只有协调器会推上游；协调器没在跑时受理等于静默丢单。
    """
    return bool(settings.coordinator_enabled and coordinator.running)


@router.post("/v1/images/generations")
def generate(req: GenerateRequest, key: dict = Depends(require_key)) -> Any:
    if not executor_ready():
        raise HTTPException(503, detail="执行器未运行（JIMENG2API_COORDINATOR=1 并重启）")
    try:
        task = service.create_task(api_key_id=key["id"], **_payload(req))
    except JimengParamError as e:
        raise HTTPException(400, detail=str(e)) from e
    cfg = repo.runtime_config()
    budget = min(float(cfg["sync_timeout"]),
                 float(req.wait or cfg["sync_timeout"]))
    done = service.wait_task(task["id"], budget)
    if done.get("status") in ("succeeded", "failed"):
        if done["status"] == "failed" and not (done.get("images") or []):
            code = 400 if done.get("error_kind") in ("param", "content") else 502
            return JSONResponse(status_code=code,
                                content=ok_response(done, task.get("degradations")))
        return ok_response(done, task.get("degradations"))
    return JSONResponse(status_code=202, content={
        "task_id": task["id"], "status": done.get("status", "queued"),
        "poll_url": f"/async/v1/images/generations/{task['id']}",
        "detail": "上游未在等待预算内完成，请按 poll_url 轮询",
        "degradations": task.get("degradations") or [],
    })


@router.post("/async/v1/images/generations", status_code=202)
def generate_async(req: GenerateRequest, key: dict = Depends(require_key)) -> dict:
    if not executor_ready():
        raise HTTPException(503, detail="执行器未运行（JIMENG2API_COORDINATOR=1 并重启）")
    try:
        task = service.create_task(api_key_id=key["id"], **_payload(req))
    except JimengParamError as e:
        raise HTTPException(400, detail=str(e)) from e
    # 只有 task_id：这是这类上游受理时的原生形状。多塞 status / poll_url /
    # degradations 只是把自己的任务形状漏给客户端（skill §3.1），而客户端
    # 一旦依赖它们，网关就再也改不动内部字段。轮询路径是固定的，不需要告知。
    return {"task_id": task["id"]}


@router.get("/async/v1/images/generations/{task_id}")
def poll(task_id: str, key: dict = Depends(require_key)) -> Any:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(404, detail="task 不存在")
    if not key.get("is_admin") and task.get("api_key_id") != key["id"]:
        raise HTTPException(404, detail="task 不存在")
    view = async_view(task)
    # 运行中 202、终态 200：上游就是这么答的，客户端若按"202 就是未完成"判，
    # 一个已结束的请求会被读成还在跑。失败也是 200，错误在 body 里 —— 用
    # 4xx/5xx 会让重试逻辑把"已经失败的任务"当成"查询失败"，永远重试。
    code = 200 if view["status"] in ("succeeded", "failure") else 202
    return JSONResponse(status_code=code, content=view)


@router.get("/v1/tasks/{task_id}")
def task_detail(task_id: str, key: dict = Depends(require_key)) -> dict:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(404, detail="task 不存在")
    if not key.get("is_admin") and task.get("api_key_id") != key["id"]:
        raise HTTPException(404, detail="task 不存在")
    return task_view(task)


@router.get("/v1/models")
def list_models() -> dict:
    reg = pool_mod.registry()
    return {
        "object": "list",
        "captured_at": reg.captured_at,
        "live": reg.live,
        "source": reg.source,
        "data": [{
            "id": m.key, "name": m.name, "new": m.is_new,
            "counts": m.count_options(), "default_count": m.default_count,
            "resolutions": m.buckets(), "prices": m.prices,
            "max_images": m.max_images, "sizes": m.size_options(),
        } for m in reg.models],
    }


@router.get("/v1/credits")
def credits(key: dict = Depends(require_key)) -> dict:
    acc = repo.account_stats()
    accounts = [{
        "id": a["id"], "name": a["name"] or a["uid"] or f"#{a['id']}",
        "credits": a["credits"], "state": a["state"],
        "member_level": a["member_level"],
    } for a in repo.list_accounts()]
    return {"total": acc["credits"], "accounts": accounts,
            "checked_at": db.now()}
