#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务编排：建任务、选号、提交、轮询、对账。

主线（与参考实现一致）：

    POST 只落库 → 协调器提交 → 协调器轮询 → 终态落库
    ↑ 请求内零上游往返     ↑ 唯一执行者，受并发闸门约束

为什么不在请求里直接提交：建任务是**计费动作**，需要重试、需要限并发、
需要在账号之间调度 —— 塞进请求线程会让这三件事都做不干净。
"""
from __future__ import annotations

import base64
import binascii
import re
import time
import uuid
from typing import Sequence

import httpx

from . import db, pool as pool_mod, repo
from .models import ModelSpec, Registry
from .upstream import protocol
from .upstream.errors import JimengError, JimengParamError

MAX_IMAGE_BYTES = 12 * 1024 * 1024


# ------------------------------------------------------------------ 模型别名
def model_aliases(reg: Registry | None = None) -> dict[str, str]:
    """模型 key / 面板名 / 常用别名 → 上游 key。

    面板与 OpenAI 风格接口共用同一份解析 —— 两套 alias 表迟早会分叉，
    然后同一句 `model: "seedream-5.5"` 在两边得到不同结果。
    """
    reg = reg or pool_mod.registry()
    out: dict[str, str] = {}
    for m in reg.models:
        out[m.key] = m.key
        slug = _slug(m.name)
        out[slug] = m.key
        out[slug.replace(".", "-")] = m.key
    default = repo.runtime_config()["default_model"]
    out.update({
        "default": default, "jimeng": default,
        "seedream-5.5": "high_aes_general_v55",
        "seedream-5.0-pro": "high_aes_general_v50p_large",
        "seedream-5.0-lite": "high_aes_general_v50",
        "seedream-5.0-flash": "high_aes_general_v50_flash",
        "seedream-4.7": "high_aes_general_v43",
        "seedream-4.6": "high_aes_general_v42",
        "seedream-4.5": "high_aes_general_v40l",
    })
    return {k: v for k, v in out.items() if v}


def _slug(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[\s_/]+", "-", s)
    return re.sub(r"[^a-z0-9.\-]", "", s)


def resolve_model_name(name: str, reg: Registry | None = None) -> str:
    key = model_aliases(reg).get((name or "").strip().lower())
    if not key:
        raise JimengParamError(f"未知模型 {name!r}", kind="param")
    return key


# ------------------------------------------------------------------ 入参解析
def resolve_request(*, registry: Registry, model: str, size: str,
                    count: int, resolution: str = "") -> dict:
    """把调用方参数吸附到上游能力表：模型、张数、分辨率桶、比例槽。

    任何"请求了 A、实际做 B"都进返回值 `degradations` —— 静默降级会让人
    按 A 的预期为 B 付钱。
    """
    if model:
        model = resolve_model_name(model, registry)
    spec = registry.get(model) if model else registry.default()
    if spec is None:
        raise JimengParamError(f"未知模型 {model!r}；可用：{list(registry.by_key)}",
                               kind="param")
    degradations: list[str] = []
    opts = spec.count_options()
    want = int(count or 1)
    final_count = want
    if opts and want not in opts:
        final_count = min(opts, key=lambda x: (abs(x - want), x))
        degradations.append(f"张数 {want} 不在该模型选项 {opts}，吸附为 {final_count}")
    try:
        width, height = protocol.parse_size(size)
    except ValueError as e:
        raise JimengParamError(str(e), kind="param") from e
    bucket = resolution or protocol.resolution_type_for_size(width, height)
    if spec.resolutions and bucket not in spec.resolutions:
        fallback = ("2k" if "2k" in spec.resolutions
                    else next(iter(spec.resolutions)))
        degradations.append(f"该模型无 {bucket} 档，改用 {fallback}")
        bucket = fallback
    # 比例槽吸附（比例不合法时上游会按最长边裁切，这里先算好再告知）
    ratio = protocol.ratio_for_size(width, height)
    exact = protocol.RATIOS[ratio]
    if abs(width / height - exact[0] / exact[1]) > 0.01:
        degradations.append(
            f"尺寸 {width}×{height} 比例不在 8 个槽内，按 {exact[0]}:{exact[1]} 提交")
    return {
        "spec": spec, "model": spec.key, "count": final_count,
        "width": width, "height": height, "resolution": bucket,
        "size": f"{width}x{height}", "ratio": ratio,
        "degradations": degradations,
    }


def estimate_credits(spec: ModelSpec, resolution: str, count: int,
                     mode: str) -> int | None:
    unit = spec.price(resolution, "i2i" if mode == "i2i" else "t2i")
    if unit is None:
        return None
    return int(unit) * max(1, count)


# ---------------------------------------------------------------- 输入图处理
def _decode_image(ref: str) -> tuple[bytes, str]:
    """把 `data:` / `http(s):` 引用变成字节。`tos-` 开头属于 URI 直传，不走这里。"""
    if ref.startswith("data:"):
        try:
            head, b64 = ref.split(",", 1)
            return base64.b64decode(b64, validate=True), head
        except (ValueError, binascii.Error) as e:
            raise JimengParamError("data URI 解码失败", kind="param") from e
    if ref.startswith(("http://", "https://")):
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as c:
                r = c.get(ref)
                r.raise_for_status()
                data = r.content
        except httpx.HTTPError as e:
            raise JimengParamError(f"拉取输入图失败：{e}", kind="param") from e
        if len(data) > MAX_IMAGE_BYTES:
            raise JimengParamError("输入图超过 12MB", kind="param")
        return data, r.headers.get("content-type", "")
    raise JimengParamError(
        "输入图需为 data URI、http(s) 链接或 tos image_uri", kind="param")


def prepare_image_uris(account_id: int, refs: Sequence[str]) -> list[str]:
    """本地图/外链 → `image_uri`。`tos-` 形态直接透传（零请求）。"""
    rt = pool_mod.pool.runtime(account_id)
    uris: list[str] = []
    for ref in refs:
        ref = (ref or "").strip()
        if not ref:
            continue
        if ref.startswith("tos-"):
            uris.append(ref)
            continue
        data, ctype = _decode_image(ref)
        if ctype and "image" not in ctype and not ref.startswith("data:image"):
            raise JimengParamError(f"输入不是图片（{ctype}）", kind="param")
        if not data:
            raise JimengParamError("输入图为空", kind="param")
        uris.append(rt.uploader.upload(data))
    if not uris:
        raise JimengParamError("没有可用的输入图", kind="param")
    return uris


# ---------------------------------------------------------------- 建任务
def create_task(*, api_key_id: int | None, model: str, prompt: str,
                size: str, count: int, resolution: str = "",
                negative_prompt: str = "", seed: int | None = None,
                images: Sequence[str] | None = None) -> dict:
    reg = pool_mod.registry()
    resolved = resolve_request(registry=reg, model=model, size=size,
                               count=count, resolution=resolution)
    mode = "i2i" if images else "t2i"
    if mode == "i2i" and not str(prompt or "").strip():
        raise JimengParamError("图生图必须给 prompt（描述要怎么改）", kind="param")
    if not str(prompt or "").strip():
        raise JimengParamError("prompt 不能为空", kind="param")
    task_id = str(uuid.uuid4())
    task = repo.create_task(
        task_id=task_id, api_key_id=api_key_id, mode=mode,
        model=resolved["model"], prompt=prompt,
        negative_prompt=negative_prompt, size=resolved["size"],
        width=resolved["width"], height=resolved["height"],
        resolution=resolved["resolution"], count=resolved["count"],
        seed=seed, image_uris=list(images or []),
    )
    task["degradations"] = resolved["degradations"]
    return task


def wait_task(task_id: str, timeout: float) -> dict:
    """同步接口用：**只查本地库**，不碰上游。"""
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        task = repo.get_task(task_id)
        if task and task["status"] in ("succeeded", "failed"):
            return task
        time.sleep(0.4)
    return repo.get_task(task_id) or {}


# ---------------------------------------------------------------- 提交执行
def _mark_account_fault(account_id: int, err: JimengError) -> None:
    cfg = repo.runtime_config()
    if err.kind in ("auth", "quota"):
        repo.record_account_result(account_id, ok=False, error=str(err),
                                   kind=err.kind)
    elif err.kind == "rate":
        repo.set_cooldown(account_id, int(cfg["cooldown_rate"]), str(err))
    elif err.kind == "risk":
        repo.set_cooldown(account_id, int(cfg["cooldown_risk"]), str(err))


def submit_task(task: dict) -> None:
    """提交一条 queued/submitting 任务。**这里是唯一真正花钱的地方。**

    重试边界（重要）：
    * 上游**明确拒绝**（1015 / 额度 / 限流 / 风控）⇒ 任务肯定没建，换号重试安全；
    * 网络错误 / 超时 ⇒ **不知道建没建**，绝不重发 —— 宁可报"结果未知"，
      也不让同一份 prompt 被扣两次积分。
    """
    cfg = repo.runtime_config()
    task_id = task["id"]
    tried: set[int] = set()
    last_err: JimengError | None = None

    if not any(a["enabled"] for a in repo.list_accounts()):
        repo.mark_task_failed(task_id, "账号池为空或全部被禁用", "pool")
        return

    for _ in range(4):
        cands = [c for c in repo.dispatch_candidates(
            int(cfg["per_account"]), int(cfg["cooldown_risk"]))
            if c["id"] not in tried]
        if not cands:
            break
        account = cands[0]
        tried.add(account["id"])
        db.execute("UPDATE tasks SET attempts = attempts + 1 WHERE id = ?",
                   (task_id,))
        try:
            uris = task["image_uris"]
            if task["mode"] == "i2i" and uris:
                uris = prepare_image_uris(account["id"], uris)
            _submit(account["id"], task, uris)
            repo.mark_task_running(
                task_id, account["id"],
                forecast=estimate_credits(
                    pool_mod.registry().get(task["model"]) or ModelSpec(
                        key=task["model"], name=task["model"]),
                    task["resolution"], task["count"], task["mode"]),
            )
            if uris != task["image_uris"]:
                db.execute("UPDATE tasks SET image_uris = ? WHERE id = ?",
                           (db.dumps(uris), task_id))
            return
        except JimengError as e:
            last_err = e
            _mark_account_fault(account["id"], e)
            if e.kind in ("auth", "quota", "rate", "risk"):
                continue  # 上游明确拒绝：任务未创建，换号安全
            repo.mark_task_failed(task_id, str(e), e.kind)
            return

    # 走到这里说明本轮没有提交成功：要么没号可用，要么全被明确拒绝
    msg = str(last_err) if last_err else "没有可用账号（全部冷却 / 禁用 / 失效）"
    attempts = int(db.scalar("SELECT attempts FROM tasks WHERE id = ?",
                             (task_id,), 0) or 0)
    if last_err is None:
        # 没有可用账号（冷却/在途满）：等，但只等到 10 分钟，避免任务永远排队
        age = db.now() - int(task["created_at"])
        if age > 600:
            repo.mark_task_failed(task_id, f"等待可用账号超时（{msg}）", "pool")
            return
        delay = 15
    else:
        if attempts >= 4 + int(cfg["max_retry"]):
            repo.mark_task_failed(task_id, msg, last_err.kind)
            return
        delay = min(60, 10 * max(1, attempts))
    repo.mark_task_queued_retry(task_id, f"{msg}；稍后重试")
    db.execute("UPDATE tasks SET next_poll_at = ? WHERE id = ?",
               (db.now() + delay, task_id))


def _submit(account_id: int, task: dict, uris: list[str]) -> str:
    """按任务形态选 t2i / i2i 提交。**这是唯一真正花钱的调用。**"""
    rt = pool_mod.pool.runtime(account_id)
    if task["mode"] == "i2i":
        return rt.client.submit_i2i(
            prompt=task["prompt"], image_uris=uris, model=task["model"],
            count=task["count"], width=task["width"], height=task["height"],
            submit_id=task["id"], resolution_type=task["resolution"])
    return rt.client.submit_t2i(
        prompt=task["prompt"], model=task["model"], count=task["count"],
        width=task["width"], height=task["height"], submit_id=task["id"],
        seed=task["seed"], negative_prompt=task["negative_prompt"],
        resolution_type=task["resolution"])


# ---------------------------------------------------------------- 轮询执行
def poll_once() -> None:
    cfg = repo.runtime_config()
    interval = float(cfg["poll_interval"])
    tasks = repo.due_tasks()
    if not tasks:
        return
    # 兜底：卡在 submitting 超过 2 分钟的任务（进程重启/提交线程崩溃）
    for row in repo.list_tasks(status="submitting", limit=20)["items"]:
        if db.now() - (row["updated_at"] or 0) > 120:
            repo.mark_task_failed(
                row["id"],
                "提交状态未知（进程中断）。上游**可能已创建并计费**，"
                "请先到即梦作品页确认，再决定是否重提", "unknown")

    by_account: dict[int, list[dict]] = {}
    for t in tasks:
        if not t["account_id"]:
            continue
        if db.now() - (t["started_at"] or 0) > int(cfg["task_timeout"]):
            repo.mark_task_failed(t["id"], "任务超时（上游未在预算内完成）",
                                  "timeout")
            continue
        by_account.setdefault(t["account_id"], []).append(t)

    for account_id, group in by_account.items():
        try:
            rt = pool_mod.pool.runtime(account_id)
            result = rt.client.fetch_many([t["id"] for t in group])
        except JimengError as e:
            if e.kind in ("auth", "quota"):
                repo.record_account_result(account_id, ok=False, error=str(e),
                                           kind=e.kind)
            elif e.kind in ("rate", "risk"):
                _mark_account_fault(account_id, e)
            for t in group:
                repo.update_task_poll(
                    t["id"], upstream_status=t["upstream_status"],
                    progress=t["progress"], next_poll_at=db.now() + 30)
            continue
        for t in group:
            st = result.get(t["id"]) or {}
            if st.get("ok"):
                images = st.get("images") or []
                repo.finish_task(t["id"], images=images,
                                 forecast=st.get("cost"),
                                 upstream_status=st.get("status"))
                repo.record_account_result(account_id, ok=True)
                est = st.get("cost") or t.get("forecast_credits")
                if est:
                    repo.charge_key(t.get("api_key_id"), int(est))
                db.execute("UPDATE tasks SET charged_credits = COALESCE(charged_credits, ?) "
                           "WHERE id = ?", (None, t["id"]))
            elif st.get("failed"):
                msg = st.get("fail_message") or "上游生成失败"
                repo.mark_task_failed(t["id"], msg, "upstream")
                repo.record_account_result(account_id, ok=False, error=msg)
            else:
                repo.update_task_poll(
                    t["id"], upstream_status=st.get("status"),
                    progress=st.get("progress") or "",
                    next_poll_at=db.now() + max(1, int(interval)))


# ---------------------------------------------------------------- 对账/刷新
def reconcile(task_id: str) -> dict:
    """按 submit_id 在消耗记录里找真实扣费。

    上游消耗记录**延迟结算**：跑完立刻查可能还没有那条记录 ⇒
    `found=false` 不等于免费，面板必须这样写。
    """
    task = repo.get_task(task_id)
    if not task or not task.get("account_id"):
        return {"found": False, "reason": "任务没有绑定账号"}
    try:
        rt = pool_mod.pool.runtime(task["account_id"])
        # 每页最多 20 条（再大就 1000），最多翻 3 页 = 60 条
        hit = None
        cursor = "0"
        for _ in range(3):
            data = rt.client.credit_history(count=20, history_type=2,
                                            cursor=cursor)
            records = data.get("records") or []
            hit = next((r for r in records
                        if str(r.get("submit_id")) == task_id), None)
            if hit or not data.get("has_more"):
                break
            cursor = str(data.get("new_cursor") or "")
            if not cursor:
                break
    except JimengError as e:
        return {"found": False, "reason": str(e)}
    if not hit:
        # 免费档（会员 Lite 实测 0 积分）本来就不会产生消耗记录 —— 别把
        # "没有记录"说成"没结算"，两者要分开讲。
        return {"found": False,
                "reason": "最近 60 条消耗记录里没有这个 submit_id：可能尚未结算，"
                          "也可能本次为 0 积分（免费档）"}
    amount = hit.get("amount")
    try:
        amount_i = int(amount)
    except (TypeError, ValueError):
        amount_i = None
    old = task.get("charged_credits")
    repo.set_charged(task_id, amount_i)
    if amount_i is not None:
        base = old if old is not None else (task.get("forecast_credits") or 0)
        repo.charge_key(task.get("api_key_id"), amount_i - int(base))
    return {"found": True, "amount": amount, "record": hit}


def refresh_task_images(task_id: str) -> dict:
    task = repo.get_task(task_id)
    if not task:
        return {"ok": False, "error": "任务不存在"}
    uris = [img.get("uri") for img in task["images"] if img.get("uri")]
    if not uris or not task.get("account_id"):
        return {"ok": False, "error": "没有可刷新的 image_uri"}
    try:
        rt = pool_mod.pool.runtime(task["account_id"])
        mapping = rt.client.image_by_uri(uris)
    except JimengError as e:
        return {"ok": False, "error": str(e)}
    for img in task["images"]:
        item = mapping.get(img.get("uri") or "")
        if item and item.get("image_url"):
            img["url"] = item["image_url"]
    db.execute("UPDATE tasks SET images = ? WHERE id = ?",
               (db.dumps(task["images"]), task_id))
    return {"ok": True, "images": task["images"]}


