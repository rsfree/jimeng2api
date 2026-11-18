#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理面板 API。

信息架构对齐 workbuddy2api 的面板：概览 / 账号池 / 任务中心 / 用量 / 日志 /
模型与档位 / 访问密钥 / 设置。所有写操作都要求管理员 Key。

一条纪律：**面板永远不返回完整 sessionid**。账号列表只给尾 6 位，
"换号"只能删了重加 —— 这比"提供一个查看明文的接口"更安全，也更少借口。
"""
from __future__ import annotations

import base64
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from . import db, login_assistant, models, pool as pool_mod, remote_browser, repo, service
from .config import settings
from .coordinator import coordinator
from .security import require_admin
from .upstream import protocol
from .upstream.errors import JimengError

router = APIRouter(prefix="/panel/api", dependencies=[Depends(require_admin)])
#: 登录必须**不经过** require_admin（它要在请求体里收 Key），单独挂一个路由
#: —— 否则面板第一次打开就永远进不去（先 401，再也没机会提交 Key）。
login_router = APIRouter(prefix="/panel/api")


class LoginReq(BaseModel):
    key: str = ""


class AccountReq(BaseModel):
    sessionid: str
    remark: str = ""
    probe: bool = True


class AccountPatch(BaseModel):
    enabled: bool | None = None
    remark: str | None = None


class CooldownReq(BaseModel):
    seconds: int = 0


class KeyReq(BaseModel):
    name: str = ""
    quota_credits: int = 0
    is_admin: bool = False


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    quota_credits: int | None = None


class PlayReq(BaseModel):
    prompt: str = ""
    model: str = ""
    size: str = "2048x2048"
    resolution: str = ""
    count: int = 1
    image: str = ""
    dry_run: bool = True
    confirm: bool = False


class LoginStartReq(BaseModel):
    remark: str = ""
    provider: str = "douyin"      # douyin | feishu


class BatchAccountReq(BaseModel):
    text: str
    remark: str = ""
    probe: bool = True


class SettingsPatch(BaseModel):
    concurrency: int | None = Field(default=None, ge=1, le=32)
    per_account: int | None = Field(default=None, ge=1, le=8)
    poll_interval: float | None = Field(default=None, ge=0.5, le=60)
    task_timeout: int | None = Field(default=None, ge=60, le=86400)
    sync_timeout: int | None = Field(default=None, ge=10, le=900)
    default_model: str | None = None
    cooldown_rate: int | None = Field(default=None, ge=10, le=3600)
    cooldown_risk: int | None = Field(default=None, ge=60, le=86400)
    max_retry: int | None = Field(default=None, ge=0, le=5)
    credit_low_threshold: int | None = Field(default=None, ge=0, le=100000)
    models_live_refresh: int | None = Field(default=None, ge=0, le=86400)


# ------------------------------------------------------------------ 登录/概览
@login_router.post("/login")
def login(req: LoginReq) -> dict:
    key = repo.key_by_raw(req.key.strip())
    if not key or not key["enabled"] or not key["is_admin"]:
        raise HTTPException(401, "Key 无效或没有管理权限")
    return {"ok": True, "name": key["name"], "key_prefix": key["key_prefix"]}


def _alerts(acc: dict, reg: models.Registry) -> list[dict]:
    out: list[dict] = []
    cfg = repo.runtime_config()
    if acc["total"] == 0:
        out.append({"level": "warn", "text": "账号池为空：先添加一个 sessionid 才能生成"})
    if acc["invalid"]:
        out.append({"level": "bad",
                    "text": f"{acc['invalid']} 个账号登录态失效，需要重新登录后替换"})
    if acc["cooling"]:
        out.append({"level": "warn", "text": f"{acc['cooling']} 个账号处于冷却中"})
    if acc["quota"]:
        out.append({"level": "warn", "text": f"{acc['quota']} 个账号积分不足"})
    low = int(cfg["credit_low_threshold"])
    if low and acc["total"] and acc["credits"] < low * max(1, acc["ok"]):
        out.append({"level": "warn",
                    "text": f"可用积分 {acc['credits']}，低于阈值 {low}×可用账号数"})
    if not reg.live:
        out.append({"level": "warn",
                    "text": f"模型能力表来自{reg.source}，可能已过期"})
    elif reg.captured_at and db.now() - reg.captured_at > 6 * 3600:
        hours = (db.now() - reg.captured_at) // 3600
        out.append({"level": "warn",
                    "text": f"能力表已 {hours} 小时未刷新（上游改动可能导致选错模型）"})
    if settings.coordinator_enabled and not coordinator.running:
        out.append({"level": "bad", "text": "协调器未运行：任务不会被执行"})
    return out


@router.get("/overview")
def overview() -> dict:
    acc = repo.account_stats()
    today = repo.today_stats()
    reg = pool_mod.registry()
    recent = repo.list_tasks(limit=8)["items"]
    return {
        "accounts": acc,
        "today": today,
        "tasks": repo.task_counts(),
        "trend": repo.trend(7),
        "recent": recent,
        "alerts": _alerts(acc, reg),
        "models": {"count": len(reg.models), "live": reg.live,
                   "source": reg.source, "captured_at": reg.captured_at},
        "coordinator": {"running": coordinator.running,
                        "ticks": coordinator.ticks,
                        "last_error": coordinator.last_error},
        "version": settings.version,
        "upstream": {"base": settings.base_url,
                     "da_version": settings.da_version,
                     "appvr": settings.appvr},
    }


# --------------------------------------------------------------------- 账号
def parse_session_input(raw: str) -> tuple[str, int | None]:
    """从「裸 sessionid」或「整行 cookie」里取出 sessionid 与到期时间。

    运营复制的往往是 DevTools 里整行 cookie（`sessionid=…; sid_guard=…; ttwid=…`），
    要求他们手工挑出 sessionid 是没必要的摩擦 —— 但解析只发生在**入池这一刻**，
    库里仍然只存一个 sessionid，不保存整串 jar（其余 20 多个键实测都不需要）。

    `sid_guard` 的格式是 `<sid>|<签发秒>|<有效秒>|<GMT 到期>`，
    到期 = 签发 + 有效，纯字符串解析、零成本。
    """
    raw = (raw or "").strip()
    if not raw:
        return "", None
    if "sessionid=" in raw:
        kv: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                kv[k.strip()] = v.strip()
        sid = kv.get("sessionid", "").strip('"')
        expires: int | None = None
        guard = kv.get("sid_guard", "")
        bits = guard.split("|")
        if len(bits) >= 3 and bits[1].isdigit() and bits[2].isdigit():
            expires = int(bits[1]) + int(bits[2])
        return sid, expires
    return raw.strip('"'), None


@router.get("/accounts")
def accounts() -> dict:
    items = repo.list_accounts()
    return {"items": items, "stats": repo.account_stats(),
            "config": repo.runtime_config(),
            "login_assistant": login_assistant.available(),
            "remote_browser": remote_browser.available()}


@router.post("/accounts")
def add_account(req: AccountReq) -> dict:
    sid, expires = parse_session_input(req.sessionid)
    if len(sid) < 20:
        raise HTTPException(400, "sessionid 看起来不合法（长度 < 20）")
    if db.one("SELECT id FROM accounts WHERE sessionid = ?", (sid,)):
        raise HTTPException(409, "这个 sessionid 已经在池子里")
    account_id = repo.add_account(sid, req.remark)
    if expires:
        repo.update_account(account_id, expires_at=expires)
    result: dict[str, Any] = {"ok": True, "id": account_id}
    if req.probe:
        try:
            result["probe"] = pool_mod.pool.probe(account_id)
        except JimengError as e:
            result["ok"] = False
            result["error"] = str(e)
            result["kind"] = e.kind
    result["account"] = repo.get_account(account_id)
    return result


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, req: AccountPatch) -> dict:
    if not repo.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    fields: dict[str, Any] = {}
    if req.enabled is not None:
        fields["enabled"] = 1 if req.enabled else 0
        if req.enabled:
            fields["cooldown_until"] = 0
    if req.remark is not None:
        fields["remark"] = req.remark
    repo.update_account(account_id, **fields)
    return {"ok": True, "account": repo.get_account(account_id)}


@router.delete("/accounts/{account_id}")
def remove_account(account_id: int) -> dict:
    running = db.scalar(
        "SELECT COUNT(*) FROM tasks WHERE account_id = ? AND status IN "
        "('submitting','running')", (account_id,), 0)
    if running:
        raise HTTPException(409, f"该账号还有 {running} 个在途任务，先等它们结束")
    pool_mod.pool.drop(account_id)
    repo.delete_account(account_id)
    return {"ok": True}


@router.post("/accounts/{account_id}/probe")
def probe_account(account_id: int) -> dict:
    try:
        return {"ok": True, "probe": pool_mod.pool.probe(account_id),
                "account": repo.get_account(account_id)}
    except JimengError as e:
        return {"ok": False, "error": str(e), "kind": e.kind,
                "account": repo.get_account(account_id)}


@router.post("/accounts/batch")
def add_accounts_batch(req: BatchAccountReq) -> dict:
    """批量导入：一行一个 sessionid 或整行 cookie。

    号池动辄几十个号，逐个点「添加」不现实；这里做三件事：
    * 逐行解析（含 `sessionid=...; sid_guard=...` 整行 cookie）、批内去重、库内去重；
    * 用 4 个线程并发探活（每个号一个独立客户端，互不共享 cookie）；
    * 任何一行失败都不影响其余行 —— 逐行结果返回，不做"全有或全无"。
    """
    lines = [ln.strip() for ln in (req.text or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise HTTPException(400, "没有可导入的行")
    if len(lines) > 200:
        raise HTTPException(400, "单次最多 200 行")

    created: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for idx, line in enumerate(lines, 1):
        sid, expires = parse_session_input(line)
        if len(sid) < 20:
            skipped.append({"line": idx, "reason": "sessionid 长度不合法"})
            continue
        if sid in seen:
            skipped.append({"line": idx, "reason": "批内重复"})
            continue
        seen.add(sid)
        if db.one("SELECT id FROM accounts WHERE sessionid = ?", (sid,)):
            skipped.append({"line": idx, "reason": "已在池中"})
            continue
        account_id = repo.add_account(sid, req.remark or "批量导入")
        if expires:
            repo.update_account(account_id, expires_at=expires)
        created.append({"id": account_id, "line": idx})

    probes: dict[int, dict] = {}
    if req.probe and created:
        from concurrent.futures import ThreadPoolExecutor
        def _probe(item):
            try:
                return item["id"], {"ok": True,
                                    **pool_mod.pool.probe(item["id"])}
            except JimengError as e:
                return item["id"], {"ok": False, "error": str(e), "kind": e.kind}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for aid, res in ex.map(_probe, created):
                probes[aid] = res

    items = []
    for item in created:
        row = repo.get_account(item["id"]) or {}
        p = probes.get(item["id"]) or {}
        items.append({
            "id": item["id"], "line": item["line"],
            "name": row.get("name") or "", "credits": row.get("credits"),
            "state": row.get("state"),
            "probed": bool(p), "ok": p.get("ok") if p else None,
            "error": p.get("error", "") if p else "",
        })
    return {"created": len(created), "items": items, "skipped": skipped,
            "stats": repo.account_stats()}


@router.post("/accounts/probe-all")
def probe_all() -> dict:
    results = []
    for acc in repo.list_accounts():
        if not acc["enabled"]:
            continue
        try:
            results.append({"id": acc["id"], "ok": True,
                            "probe": pool_mod.pool.probe(acc["id"])})
        except JimengError as e:
            results.append({"id": acc["id"], "ok": False, "error": str(e),
                            "kind": e.kind})
    return {"items": results, "stats": repo.account_stats()}


@router.post("/login/qr/start")
def login_qr_start(req: LoginStartReq) -> dict:
    """发起扫码登录。返回会话状态；二维码通过 /image 单独取（避免轮询传大图）。"""
    if not login_assistant.available():
        raise HTTPException(
            501, "登录助手未安装：uv pip install -r requirements-login.txt "
                 "&& playwright install chromium")
    return login_assistant.start(req.remark, provider=req.provider).snapshot()


@router.get("/login/qr/{session_id}")
def login_qr_status(session_id: str, v: int = -1) -> dict:
    """轮询状态。二维码只在版本变化时随包返回（data URL），避免每 1.5 秒传一张图。

    ⚠️ 不用 `<img src>` 直接指向 /image：那个端点要 Bearer，而 `<img>` 带不了
    请求头 —— 要么放宽鉴权（不可接受），要么把图内联。这里选内联。
    """
    s = login_assistant.get(session_id)
    if not s:
        raise HTTPException(404, "扫码会话不存在或已结束")
    data = s.snapshot()
    if s.qr and s.qr_version != v:
        data["qr"] = "data:image/png;base64," + base64.b64encode(s.qr).decode()
    return data


@router.get("/login/qr/{session_id}/image")
def login_qr_image(session_id: str) -> Response:
    s = login_assistant.get(session_id)
    if not s or not s.qr:
        raise HTTPException(404, "二维码尚未生成")
    return Response(content=s.qr, media_type="image/png",
                    headers={"cache-control": "no-store"})


@router.post("/login/qr/{session_id}/cancel")
def login_qr_cancel(session_id: str) -> dict:
    return {"ok": login_assistant.cancel(session_id)}


@router.post("/remote/start")
def remote_start() -> dict:
    """签发一次性 WebSocket ticket（60 秒有效，单次使用）。"""
    if not remote_browser.available():
        raise HTTPException(
            501, "远程浏览器未安装：uv pip install -r requirements-login.txt "
                 "&& playwright install chromium")
    return {"ws": "/panel/ws/remote?ticket=" + remote_browser.issue_ticket(),
            "ttl": 60}


@router.post("/remote/stop")
def remote_stop() -> dict:
    remote_browser.stop()
    return {"ok": True}


@router.post("/accounts/{account_id}/cooldown")
def set_cooldown(account_id: int, req: CooldownReq) -> dict:
    repo.update_account(account_id, cooldown_until=db.now() + req.seconds)
    return {"ok": True, "account": repo.get_account(account_id)}


# --------------------------------------------------------------------- 密钥
@router.get("/keys")
def keys() -> dict:
    return {"items": repo.list_keys()}


@router.post("/keys")
def create_key(req: KeyReq) -> dict:
    raw = repo.create_key(req.name, is_admin=req.is_admin,
                          quota=req.quota_credits)
    return {"ok": True, "key": raw,
            "items": repo.list_keys(),
            "note": "明文只显示这一次，请立即保存"}


@router.patch("/keys/{key_id}")
def patch_key(key_id: int, req: KeyPatch) -> dict:
    fields: dict[str, Any] = {}
    if req.name is not None:
        fields["name"] = req.name
    if req.enabled is not None:
        fields["enabled"] = int(req.enabled)
    if req.quota_credits is not None:
        fields["quota_credits"] = req.quota_credits
    repo.update_key(key_id, **fields)
    return {"ok": True, "items": repo.list_keys()}


@router.delete("/keys/{key_id}")
def remove_key(key_id: int) -> dict:
    admins = db.scalar("SELECT COUNT(*) FROM api_keys WHERE is_admin = 1", (), 0)
    row = db.one("SELECT is_admin FROM api_keys WHERE id = ?", (key_id,))
    if row and row["is_admin"] and admins <= 1:
        raise HTTPException(409, "这是最后一把管理员 Key，删除后无法进入面板")
    repo.delete_key(key_id)
    return {"ok": True, "items": repo.list_keys()}


# --------------------------------------------------------------------- 任务
@router.get("/tasks")
def tasks(status: str = "", q: str = "", limit: int = Query(50, le=200),
          offset: int = 0) -> dict:
    return repo.list_tasks(status=status, q=q, limit=limit, offset=offset)


@router.get("/tasks/{task_id}")
def task_detail(task_id: str) -> dict:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.post("/tasks/{task_id}/reconcile")
def reconcile(task_id: str) -> dict:
    return service.reconcile(task_id)


@router.post("/tasks/{task_id}/refresh")
def refresh_images(task_id: str) -> dict:
    return service.refresh_task_images(task_id)


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str) -> dict:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] in ("queued", "submitting", "running"):
        raise HTTPException(409, "任务仍在执行，不能删除（上游也没有取消接口）")
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"ok": True}


@router.post("/tasks")
def playground(req: PlayReq) -> dict:
    """面板试玩。默认 **dry-run**：只构造草稿，不发请求、不计费。"""
    reg = pool_mod.registry()
    try:
        resolved = service.resolve_request(
            registry=reg, model=req.model, size=req.size,
            count=req.count, resolution=req.resolution)
    except JimengError as e:
        raise HTTPException(400, str(e)) from e
    spec = resolved["spec"]
    mode = "i2i" if req.image else "t2i"
    if req.dry_run or not req.confirm:
        if mode == "i2i":
            draft = protocol.build_blend_draft(
                prompt=req.prompt, image_uris=["tos-cn-i-placeholder/dry-run"],
                model=resolved["model"], count=resolved["count"],
                width=resolved["width"], height=resolved["height"],
                da_version=settings.da_version,
                resolution_type=resolved["resolution"])
        else:
            draft = protocol.build_t2i_draft(
                prompt=req.prompt, model=resolved["model"],
                count=resolved["count"], width=resolved["width"],
                height=resolved["height"], da_version=settings.da_version,
                resolution_type=resolved["resolution"])
        return {
            "dry_run": True,
            "note": "未发送任何请求、未扣积分；勾选确认后才会真实提交",
            "model": resolved["model"], "mode": mode,
            "count": resolved["count"], "size": resolved["size"],
            "resolution": resolved["resolution"],
            "estimate_credits": service.estimate_credits(
                spec, resolved["resolution"], resolved["count"], mode),
            "degradations": resolved["degradations"],
            "draft": draft,
        }
    try:
        task = service.create_task(
            api_key_id=None, model=resolved["model"], prompt=req.prompt,
            size=req.size, count=req.count, resolution=req.resolution,
            images=[req.image] if req.image else [])
    except JimengError as e:
        raise HTTPException(400, str(e)) from e
    return {"dry_run": False, "task_id": task["id"], "status": task["status"],
            "estimate_credits": service.estimate_credits(
                spec, resolved["resolution"], resolved["count"], mode),
            "degradations": resolved["degradations"]}


# --------------------------------------------------------------- 日志/用量
@router.get("/logs/requests")
def request_logs(limit: int = Query(100, le=500), offset: int = 0,
                 path: str = "") -> dict:
    return repo.list_request_logs(limit=limit, offset=offset, path=path)


@router.get("/logs/upstream")
def upstream_logs(limit: int = Query(100, le=500), offset: int = 0,
                  only_error: bool = False) -> dict:
    return repo.list_upstream_logs(limit=limit, offset=offset,
                                   only_error=only_error)


@router.get("/usage")
def usage(days: int = Query(7, ge=1, le=30)) -> dict:
    data = repo.usage(days)
    data["accounts"] = repo.account_stats()
    return data


# --------------------------------------------------------------- 模型/配置
@router.get("/models")
def model_list() -> dict:
    reg = pool_mod.registry()
    return reg.to_dict() | {"default_model": repo.runtime_config()["default_model"]}


@router.post("/models/refresh")
def refresh_models() -> dict:
    try:
        reg = pool_mod.pool.refresh_models(force=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"读取上游能力表失败：{e}") from e
    return reg.to_dict() | {"default_model": repo.runtime_config()["default_model"]}


@router.get("/settings")
def get_settings() -> dict:
    return {
        "runtime": repo.runtime_config(),
        "upstream": {
            "base": settings.base_url, "aid": settings.aid,
            "da_version": settings.da_version,
            "web_version": settings.web_version,
            "appvr": settings.appvr, "pf": settings.pf,
            "proxy": bool(settings.http_proxy),
        },
        "version": settings.version,
        "db": {"path": settings.db_path, "accounts": repo.account_stats()["total"]},
    }


@router.put("/settings")
def put_settings(req: SettingsPatch) -> dict:
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if "default_model" in patch:
        reg = pool_mod.registry()
        if not reg.get(patch["default_model"]):
            raise HTTPException(400, "默认模型不在能力表里")
    return {"ok": True, "runtime": repo.save_runtime_config(patch)}


@router.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "version": settings.version,
        "coordinator": {"running": coordinator.running,
                        "ticks": coordinator.ticks,
                        "last_error": coordinator.last_error,
                        "last_tick_at": int(coordinator.last_tick_at)},
        "accounts": repo.account_stats(),
        "upstream": settings.base_url,
        "now": int(time.time()),
    }
