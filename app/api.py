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

import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db, pool as pool_mod, repo, service
from .config import settings
from .coordinator import coordinator
from .security import require_key
from .upstream import postedit
from .upstream.errors import JimengError, JimengParamError

router = APIRouter()


class GenerateRequest(BaseModel):
    """同步与异步共用**同一份**请求体（image-adapter §3.5）。

    三个字段需要显式对待，都不能靠"pydantic 默认忽略多出来的键"侥幸通过
    —— 那样 OpenAPI 文档会说谎：

    * `n`：收下并参与上游请求，但张数由能力表的 `count_options` 吸附
      （`resolve_request`），这里不设死上限。模型目录一变，硬上限就会把
      合法请求挡在 422。
    * `response_format`：收下但**不参与上游请求** —— 即梦没有 b64 交付能力，
      同步与异步都只出 `url`。传 `b64_json` 照常受理，不报错（§1.4 / §3.6）。
    * `mask`：**收下并落库，但当前没有任何能力消费它**。传了会在响应的
      `degradations` 里明确写「未下发上游」—— 静默丢弃会让调用方以为
      局部重绘生效了（§0.1 第 4 条）。
    """

    model: str = ""
    prompt: str = ""
    n: int = Field(
        default=1, description="期望张数；由能力表吸附到最近合法档位，n<=0 回落 1")
    size: str = "2048x2048"
    resolution: str = ""
    negative_prompt: str = ""
    seed: int | None = None
    image: str | list[str] | None = None
    mask: str | None = Field(
        default=None,
        description="局部重绘 / 消除笔要它（白=改、黑=留，与原图同尺寸）；其它能力忽略并标注")
    ratio: str | None = Field(
        default=None,
        description=("扩图目标比例：original / 3:4 / 16:9 / 4:3 / 9:16。"
                     "与 scale 互斥 —— 选了具体比例，scale 不参与"))
    scale: float | None = Field(
        default=None,
        description="扩图倍率（仅 ratio=original 生效）：1.5 / 2 / 3")
    markers: list[dict] | None = Field(
        default=None,
        description=("智能编辑的画布标记：brush{points} / arrow{start,end}，"
                     "坐标 0-1 归一化。marker（框选识别）与 text 未开放"))
    response_format: str = Field(
        default="",
        description="恒为 url：即梦无 b64 交付能力，传 b64_json 照常受理但只返回链接")
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
        # 客户端实际传的名字（可能命中了模型重定向）。运维排障要能区分
        # "客户传的就是这个模型" 与 "客户传的别名被映射到了这个模型"。
        "requested_model": (task.get("extras") or {}).get("requested_model") or "",
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
                  "height": i.get("height"), "format": i.get("format"),
                  # 作品引用的两半。不给它们，「细节修复」就没法用 ——
                  # 它是唯一只收 item 引用、且用错要计费的能力。
                  "item_id": i.get("item_id"),
                  "history_id": i.get("history_id")}
                 for i in (task.get("images") or [])],
    }


#: 受理与轮询结果都是**瞬时状态**：任何中间层缓存住一次 202，客户端就会
#: 永远拿到 `queued`。禁缓存不是性能偏好，是正确性。
NO_STORE = {"Cache-Control": "no-store"}


def poll_headers(view: dict) -> dict:
    """运行中附带 `Retry-After`：客户端不必自己猜间隔，也不会全速打库。"""
    headers = dict(NO_STORE)
    if view.get("status") in ("queued", "in_progress"):
        interval = float(repo.runtime_config()["poll_interval"])
        headers["Retry-After"] = str(max(1, int(interval)))
    return headers


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


def deliverable_url(item: Any) -> str:
    """异步可交付产物的**唯一**形态：`http(s)` 地址（§3.3 / §3.6）。

    同步的响应体发完即弃，b64 再大也只活在这一次 socket 里；异步的结果要
    写进任务行，然后被任意客户端、任意时刻、任意次数地读。让字节走上这条
    路，等于把几 MB 塞进既是队列又是账本的表，并且每次轮询重传一遍。
    """
    if not isinstance(item, dict):
        return ""
    url = item.get("url")
    if not isinstance(url, str):
        return ""
    url = url.strip()
    # `data:` 是内联字节冒充链接，不是地址；先短路，避免把几 MB 交给 urlsplit
    if not url or url[:5].lower() == "data:":
        return ""
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.netloc else ""


def has_inline_carrier(task: dict) -> bool:
    """上游回了内联字节（b64 / data: URI）—— 只用于失败归因，不用于交付。"""
    for item in (task.get("images") or []):
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            return True
        url = item.get("url")
        if isinstance(url, str) and url.strip()[:5].lower() == "data:":
            return True
    return False


def async_items(task: dict) -> list[dict]:
    """可交付产物。判据比同步**更窄**：只有 `http(s)` URL 才算。

    `data: [{}]` 非空但什么也没产出；`[{b64_json: ...}]` 看着有内容，却是
    异步存储模型明确禁止的形态。两种都不算成功 —— 前者靠判据拦住，后者靠
    `async_error` 里的 `carrier_unavailable` 归因，客户端据此知道重试无用。
    """
    return [item for item in (task.get("images") or [])
            if deliverable_url(item)]


def async_status(task: dict) -> str:
    """终态成功但无产物 ⇒ 失败。规范不允许用空 data[] 冒充成功。"""
    status = ASYNC_STATUS.get(str(task.get("status") or ""), "queued")
    if status == "succeeded" and not async_items(task):
        return "failure"
    return status


#: 失败码。客户端靠它决定「重试 / 改请求 / 改渠道配置」，所以必须分型：
#: `task_failed` 可重试，`invalid_request` 重试无用，`carrier_unavailable`
#: 只能改渠道（重试还是同一份内联字节），`empty_result` 是上游空跑。
ASYNC_ERROR_CODES = ("task_failed", "invalid_request",
                     "empty_result", "carrier_unavailable")

#: 异步终态的失败文案。**全部是固定字符串** —— 原生错误（ret / 上游 errmsg /
#: 失败星灵 key）只写进 tasks.error 与 upstream_logs，面板可见，客户端拿不到。
ASYNC_ERROR_MESSAGES = {
    "task_failed": "生成失败，请稍后重试",
    "invalid_request": "请求未通过校验或内容审核",
    "empty_result": "上游标记成功但未产出可交付图片",
    "carrier_unavailable": "上游只返回内联图片（b64/data URI），异步通道只交付 URL",
}


def async_error(task: dict) -> dict:
    """只按**归因**分型，不搬运上游原文（image-adapter §6.3 / §6.4）。"""
    status = str(task.get("status") or "")
    kind = str(task.get("error_kind") or "")
    if status == "succeeded":
        # 上游说成功却没有可交付 URL：区分「根本没产物」与「产物是内联字节」
        code = "carrier_unavailable" if has_inline_carrier(task) else "empty_result"
    elif kind in ("param", "content"):
        code = "invalid_request"
    else:
        code = "task_failed"
    return {"code": code, "message": ASYNC_ERROR_MESSAGES[code]}


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
        # 只有 url 一个载体，**没有第二个分支** —— 凡是"能从行里读出字节
        # 再拼进响应"的写法，都等于把 b64 通道留在代码里。元数据只放有值的
        # 键：`{"url": null}` 在客户端看来是"给了一个空链接"。
        item: dict[str, Any] = {"url": deliverable_url(i)}
        # `item_id` / `history_id` 拼起来就是 `item:<item_id>:<history_id>`，
        # 是「细节修复」的入参 —— 少了它，客户端拿到图也回喂不进去。
        for k in ("width", "height", "format", "item_id", "history_id"):
            if i.get(k) is not None:
                item[k] = i[k]
        items.append(item)
    body["data"] = items
    return body


def ok_response(task: dict) -> dict:
    """同步成功/失败的对外形状。

    **没有 `degradations`、没有 `credits`、没有原生 `error`**：
    前者是吸附过程的运营细节，中者是上游积分口径（等于把我们的成本结构
    摊给客户端），后者是上游原文。三者都只留在 tasks 行与面板（§6.4）。
    """
    body = {
        "created": task.get("finished_at") or task.get("created_at"),
        "task_id": task["id"],
        # 回显**客户端请求的名字**：传别名就回别名，传上游 key 则折成对外 ID
        "model": service.echo_model_id(
            (task.get("extras") or {}).get("requested_model") or "",
            task.get("model") or ""),
        "data": [{"url": i.get("url"), "width": i.get("width"),
                  "height": i.get("height"), "format": i.get("format")}
                 for i in (task.get("images") or [])],
    }
    if task.get("status") == "failed":
        body["error"] = async_error(task)      # 标准码 + 固定文案
    return body


def _payload(req: GenerateRequest) -> dict:
    images: list[str] = []
    if isinstance(req.image, str):
        images = [req.image] if req.image else []
    elif isinstance(req.image, list):
        images = [x for x in req.image if x]
    # 重定向必须**先**做：别名指向后编辑 / 抠图时，拿原值判形态会判错，
    # 结果不是"未知模型"而是走错分支（抠图被排进队列）。
    requested = (req.model or "").strip()
    model = service.resolve_redirect(requested) if requested else ""
    # 后编辑 / 未开放能力不参与 t2i 的别名解析：解析器只认能力表里的
    # 生成模型，硬套会把 `jimeng-hd` 报成"未知模型"，掩盖真正的原因。
    if model and not postedit.is_postedit(model) and not postedit.unsupported_reason(model):
        model = service.resolve_model_name(model)
    return {
        "model": model, "prompt": req.prompt, "size": req.size,
        "resolution": req.resolution,
        # n 非正（0 / 负数）回落 1；超出能力表的由 resolve_request 吸附并登记
        "count": max(1, int(req.n or 1)),
        "negative_prompt": req.negative_prompt, "seed": req.seed,
        "images": images, "mask": req.mask,
        "ratio": req.ratio or "", "scale": req.scale,
        "markers": req.markers,
        # 只用于响应回显（别名请求回别名）。模型解析结果不靠它 ——
        # 任务行里的 model 永远是上游 key / 能力 id。
        "extras": {"requested_model": requested} if requested else {},
    }


#: 协调器心跳超过这个秒数没更新就视为假死。取 180 s 是因为一次 tick 可能
#: 包含能力表刷新（逐账号 HTTP），几十秒不开火属正常，不能按 poll_interval 判。
EXECUTOR_STALE_SECONDS = 180

#: tick 连续失败到这个轮数就停止受理：任务排进来也推不出去，只会等到 600 s
#: 超时再失败，不如在入口就说"执行器不健康"。
EXECUTOR_MAX_FAIL_STREAK = 3


def executor_ready() -> bool:
    """没有执行器时**快速失败**，而不是让调用方白等一整个等待预算。

    排队任务只有协调器会推上游；协调器没在跑时受理等于静默丢单。
    仅看 `running` 不够，还有两种"活着但不干活"：

    * tick 卡住不返回（to_thread 里的 HTTP 挂住）⇒ 心跳陈旧；
    * tick 每轮都抛异常但循环还在转 ⇒ 心跳照常刷新，只能看连续失败计数。
    """
    if not (settings.coordinator_enabled and coordinator.running):
        return False
    if int(getattr(coordinator, "fail_streak", 0) or 0) >= EXECUTOR_MAX_FAIL_STREAK:
        return False
    last = float(getattr(coordinator, "last_tick_at", 0) or 0)
    # last == 0 表示刚启动、第一轮还没跑完：此时受理是安全的（马上就 tick）
    return last == 0 or (time.time() - last) <= EXECUTOR_STALE_SECONDS


def _segment(req: GenerateRequest, key: dict) -> Any:
    """分割类能力（抠图）：同步执行，不建任务。

    它没有 submit_id 可轮询，塞进任务队列只会制造一个永远查不到状态的壳。
    费用为 0，所以 `credits` 恒为 0（不是"未报价"）。
    """
    ref = req.image if isinstance(req.image, str) else (
        (req.image or [None])[0] if isinstance(req.image, list) else "")
    try:
        res = service.cutout(api_key_id=key["id"], image=ref or "")
    except JimengParamError as e:
        raise HTTPException(400, detail=str(e)) from e
    except JimengError as e:
        raise HTTPException(502, detail=str(e)) from e
    return {
        "created": int(time.time()),
        # 与异步/同步生成同一口径：客户端传别名就回别名
        "model": service.echo_model_id(req.model, res["model"]),
        "data": [{"url": i["url"], "format": i.get("format")} for i in res["images"]],
        "credits": {"forecast": 0, "charged": 0},
    }


@router.post("/v1/images/generations")
def generate(req: GenerateRequest, key: dict = Depends(require_key)) -> Any:
    # 别名先归一：「抠图」是同步能力，别名指向它时必须走同步分支
    ability = postedit.get(service.resolve_redirect(req.model))
    if ability is not None and ability.shape == "segment":
        return _segment(req, key)
    if not executor_ready():
        # 部署细节（环境变量名、重启动作）不给客户端：那是运维手册的内容
        raise HTTPException(503, detail="生成执行器暂不可用，请稍后重试")
    try:
        task = service.create_task(api_key_id=key["id"], **_payload(req))
    except JimengParamError as e:
        raise HTTPException(400, detail=str(e)) from e
    cfg = repo.runtime_config()
    # 后编辑默认**不等待**：2K 超清实测 38.5s，4K/8K 更久。占着连接等到超时
    # 再降级，会让"降级"变成常态而不是异常。要同步就显式传 wait。
    # task 可能来自测试替身或旧行，取不到 model 就回落到请求里的
    if postedit.is_postedit(task.get("model") or req.model) and req.wait is None:
        budget = float(cfg["postedit_sync_wait"])
    else:
        budget = min(float(cfg["sync_timeout"]),
                     float(req.wait if req.wait is not None else cfg["sync_timeout"]))
    done = service.wait_task(task["id"], budget) if budget > 0 else task
    if done.get("status") in ("succeeded", "failed"):
        if done["status"] == "failed" and not (done.get("images") or []):
            code = 400 if done.get("error_kind") in ("param", "content") else 502
            return JSONResponse(status_code=code, content=ok_response(done))
        return ok_response(done)
    # 降级成异步后，客户端接下来会打轮询端点；两处若各用一套词（这里
    # `running`、轮询 `in_progress`），按状态机解析的客户端会在这里断掉。
    return JSONResponse(status_code=202, headers=NO_STORE, content={
        "task_id": task["id"],
        "status": ASYNC_STATUS.get(str(done.get("status") or ""), "queued"),
        "poll_url": f"/async/v1/images/generations/{task['id']}",
        "detail": ("后编辑默认异步：请按 poll_url 轮询" if budget <= 0
                   else "上游未在等待预算内完成，请按 poll_url 轮询"),
    })


@router.post("/async/v1/images/generations", status_code=202)
def generate_async(req: GenerateRequest, key: dict = Depends(require_key)) -> Any:
    ability = postedit.get(service.resolve_redirect(req.model))
    if ability is not None and ability.shape == "segment":
        raise HTTPException(
            400, detail=f"{ability.name} 是同步能力，请用 POST /v1/images/generations")
    if not executor_ready():
        raise HTTPException(503, detail="生成执行器暂不可用，请稍后重试")
    try:
        task = service.create_task(api_key_id=key["id"], **_payload(req))
    except JimengParamError as e:
        raise HTTPException(400, detail=str(e)) from e
    # 只有 task_id：这是这类上游受理时的原生形状。多塞 status / poll_url /
    # degradations 只是把自己的任务形状漏给客户端（skill §3.1），而客户端
    # 一旦依赖它们，网关就再也改不动内部字段。轮询路径是固定的，不需要告知。
    #
    # `response_format` 在这条路径上**不产生任何行为**：即梦没有 b64 交付
    # 能力，异步更是只交付 URL（§3.6）。传 `b64_json` 照常建单、不报错 ——
    # 为一个无法兑现的传输偏好拒掉一次完全可满足的生成，是把"不能兑现"
    # 错算成了"请求非法"。这条口径写在 README 里，不靠响应体暗示。
    return JSONResponse(status_code=202, content={"task_id": task["id"]},
                        headers=NO_STORE)


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
    return JSONResponse(status_code=code, content=view,
                        headers=poll_headers(view))


@router.get("/v1/tasks/{task_id}")
def task_detail(task_id: str, key: dict = Depends(require_key)) -> dict:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(404, detail="task 不存在")
    if not key.get("is_admin") and task.get("api_key_id") != key["id"]:
        raise HTTPException(404, detail="task 不存在")
    # error_kind / credits / attempts 是运维字段：业务 Key 拿到的是与轮询端点
    # **同一个协议面**。内部枚举不是客户端的行动依据，露出去他们就会依赖它。
    return task_view(task) if key.get("is_admin") else async_view(task)


def model_entry(spec: Any, *, model_id: str = "", name: str = "",
                alias_of: str = "", default: bool = False) -> dict:
    entry = {
        "id": model_id or service.public_model_id(spec.key),
        "name": name or spec.name, "new": spec.is_new,
        "default": default,
        "counts": spec.count_options(), "default_count": spec.default_count,
        "resolutions": spec.buckets(),
        "max_images": spec.max_images, "sizes": spec.size_options(),
    }
    if alias_of:
        entry["alias_of"] = alias_of
    return entry


@router.get("/v1/models")
def list_models() -> dict:
    """能力清单：客户端只用它来**选参数**。

    不返回 `prices`（上游积分单价 = 成本结构），不返回上游 `model_req_key`
    （`id` 是对外别名），也不返回 `live` / `source`（能力表是否来自实时读取
    属于运维状态）。排障需要的全量视图在 `/panel/api/models`。

    用户配置的**模型重定向**也在这里公布（`id` = 对外名，`alias_of` = 落点）：
    清单里不出现，客户端就只能猜别名拼写，等于把配置能力藏起来。
    """
    reg = pool_mod.registry()
    default_key = reg.default().key if reg.default() else ""
    data = [model_entry(m, default=(m.key == default_key))
            for m in reg.models]
    # 后编辑不是"模型"，但客户端需要一份可路由的清单：能力与载体约束。
    # 未启用的（experimental）不下发 —— 清单里出现就必须真能提交。
    tools = postedit.public_models(
        include_experimental=settings.postedit_experimental)
    # 别名与内置 id 撞名只可能发生在"别名先建、能力表后增"的时候。
    # 解析时别名优先，清单里也必须让别名占住那个 id —— 否则同一个 id 出现
    # 两次，客户端（尤其是同步渠道模型的网关）会拿到互相矛盾的条目。
    data_idx = {e["id"]: i for i, e in enumerate(data)}
    tool_idx = {e["id"]: i for i, e in enumerate(tools)}
    for r in service.enabled_redirects(reg):
        if r["kind"] == "model":
            spec = reg.get(r["key"])
            if spec is None:
                continue
            entry = model_entry(spec, model_id=r["alias"],
                                name=r["alias"], alias_of=r["public"])
            hit = data_idx.get(r["alias"])
            if hit is None:
                data_idx[r["alias"]] = len(data)
                data.append(entry)
            else:
                data[hit] = entry
        else:
            src = tool_idx.get(r["key"])
            if src is None:
                continue      # 目标能力当前未开放：别名也不公布
            # 工具条目保持极窄的字段集（不加 alias_of）：客户端只需要
            # "这个 id 能发请求"，来源属于面板要解释的事。
            entry = {**tools[src], "id": r["alias"], "name": r["alias"]}
            hit = tool_idx.get(r["alias"])
            if hit is None:
                tool_idx[r["alias"]] = len(tools)
                tools.append(entry)
            else:
                tools[hit] = entry
    return {
        "object": "list",
        "captured_at": reg.captured_at,
        "data": data,
        "tools": tools,
    }


@router.get("/v1/credits")
def credits(key: dict = Depends(require_key)) -> dict:
    """只回**本 Key** 的额度。

    池子里有哪几个账号、各剩多少、会员等级 —— 那是运维数据（面板
    `/panel/api/overview` 的位置）。业务 Key 看到的只是"我还能用多少"。
    """
    quota = int(key.get("quota_credits") or 0)
    used = int(key.get("used_credits") or 0)
    body: dict[str, Any] = {
        "quota": quota, "used": used,
        "remaining": max(0, quota - used) if quota else None,
        "unlimited": quota <= 0,
    }
    if key.get("is_admin"):
        acc = repo.account_stats()
        body["pool"] = {
            "credits": acc["credits"],
            "accounts": [{
                "id": a["id"], "name": a["name"] or a["uid"] or f"#{a['id']}",
                "credits": a["credits"], "state": a["state"],
                "member_level": a["member_level"],
            } for a in repo.list_accounts()],
            "checked_at": db.now(),
        }
    return body
