#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号池：客户端生命周期、健康探测、冷却、能力表刷新。

为什么客户端要按账号缓存：连接池、cookie、STS 上传凭据都跟着账号走。
新建一个客户端 = 重新握手 TLS，面板每 5 秒轮询一次就会变成对上游的小型压测。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from . import db, models, repo
from .config import settings
from .upstream.client import JimengClient
from .upstream.errors import JimengError
from .upstream.upload import ImageXUploader


def _log_event(account_id: int | None, event: dict) -> None:
    db.log_upstream(
        account_id=account_id,
        task_id=event.get("task_id"),
        path=event.get("path") or "",
        http_status=event.get("status"),
        ret=str(event.get("ret") or ""),
        kind=event.get("kind") or "",
        latency_ms=int(event.get("latency_ms") or 0),
        error=event.get("error") or "",
    )


@dataclass
class Runtime:
    client: JimengClient
    uploader: ImageXUploader


class Pool:
    def __init__(self) -> None:
        self._runtimes: dict[int, Runtime] = {}
        self._lock = threading.Lock()

    def _build(self, account_id: int) -> Runtime:
        sid = repo.account_secret(account_id)
        if not sid:
            raise JimengError("账号不存在或已被删除", kind="auth")
        client = JimengClient(
            sid,
            base=settings.base_url, origin=settings.site_origin,
            aid=settings.aid, da_version=settings.da_version,
            web_version=settings.web_version, appvr=settings.appvr,
            pf=settings.pf, app_sdk_version=settings.app_sdk_version,
            ua=settings.user_agent, timeout=settings.http_timeout,
            proxy=settings.http_proxy,
            secsdk_signature=settings.secsdk_signature,
            secsdk_expire=settings.secsdk_expire,
            account_id=account_id,
            on_event=lambda e: _log_event(account_id, e),
        )
        return Runtime(client=client, uploader=ImageXUploader(client))

    def runtime(self, account_id: int) -> Runtime:
        with self._lock:
            rt = self._runtimes.get(account_id)
            if rt is None:
                rt = self._build(account_id)
                self._runtimes[account_id] = rt
            return rt

    def drop(self, account_id: int) -> None:
        with self._lock:
            rt = self._runtimes.pop(account_id, None)
        if rt:
            rt.client.close()

    def close_all(self) -> None:
        with self._lock:
            items = list(self._runtimes.values())
            self._runtimes.clear()
        for rt in items:
            rt.client.close()

    # ---------------------------------------------------------------- 探活
    def probe(self, account_id: int) -> dict:
        """零成本探活：用户信息 + 积分。**不发生成请求、不扣积分。**

        这两步同时验证三件事：sessionid 有效（否则 1015）、
        账号没被封（user_info 返回 uid）、签名头正确（commerce 会校验 sign）。
        """
        rt = self.runtime(account_id)
        try:
            # **判活只认 credits**（/commerce/v1 校验签名与登录态，失效必 1015）。
            # 昵称是"锦上添花"：user_info 需要 sec_uid，而 sec_uid 只能从
            # 能力表拿；拿不到或不被接受时不能把账号判死 —— 否则好号会被
            # 误标成 invalid，运营还得手工救回来。
            credit = rt.client.credits()
            info: dict = {}
            try:
                cfg = rt.client.common_config()
                sec_uid = str(cfg.get("sec_uid") or "")
                if sec_uid:
                    info = rt.client.user_info(sec_uid)
            except JimengError:
                info = {}
        except JimengError as e:
            repo.record_account_result(account_id, ok=False, error=str(e),
                                       kind=e.kind)
            if e.kind in ("rate", "risk"):
                cfg = repo.runtime_config()
                repo.set_cooldown(
                    account_id,
                    int(cfg["cooldown_risk"] if e.kind == "risk"
                        else cfg["cooldown_rate"]), str(e))
            elif e.kind in ("network", "sign", "upload"):
                # 非账号问题：不回写 invalid，避免把好号误杀
                repo.update_account(account_id, last_error=str(e)[:300],
                                    last_checked=db.now())
            raise
        detail = credit.get("detail") or {}
        vip = [x for x in (credit.get("expiring") or [])
               if isinstance(x, dict)]
        member = ""
        for bucket in (credit.get("raw") or {}).get("credits_detail", {}) \
                .get("vip_credits", []) or []:
            if isinstance(bucket, dict):
                member = bucket.get("vip_level") or ""
        repo.update_account(
            account_id, uid=str(info.get("uid") or ""),
            name=info.get("name") or "", credits=credit.get("total") or 0,
            credit_detail={
                "buckets": detail,
                "expiring": vip,
                "checked_at": db.now(),
            },
            member_level=member, status="ok", last_error="",
            last_checked=db.now(),
        )
        return {"account_id": account_id, "uid": info.get("uid"),
                "name": info.get("name"), "credits": credit.get("total"),
                "member_level": member}

    # ------------------------------------------------------------ 能力表
    def refresh_models(self, *, account_id: int | None = None,
                       force: bool = False) -> models.Registry:
        cached = db.get_setting("models_cache")
        if (not force and isinstance(cached, dict)
                and cached.get("captured_at", 0) + 600 > db.now()):
            return models.Registry(
                models=[models.ModelSpec.from_dict(m) for m in cached["models"]],
                captured_at=cached["captured_at"], live=True,
                source=cached.get("source", "上游实读（缓存）"))
        candidates = ([repo.get_account(account_id)] if account_id
                      else repo.list_accounts())
        last_err: Exception | None = None
        for acc in candidates:
            # 只用**探活通过**的号读能力表：未检查/失效的号拿到的是访客表
            # （实测访客 13 个模型、真实号 22 个），会把缓存悄悄降级。
            if (not acc or not acc.get("enabled") or acc["state"] != "ok"
                    or not acc.get("last_checked")):
                continue
            try:
                cfg = self.runtime(acc["id"]).client.common_config()
                reg = models.build(cfg)
            except Exception as e:  # noqa: BLE001 - 逐个账号试，最后一个错误上报
                last_err = e
                continue
            if reg.models:
                db.put_setting("models_cache", {
                    "captured_at": reg.captured_at,
                    "source": "上游实读",
                    "models": [m.to_dict() for m in reg.models],
                })
                return reg
        if isinstance(cached, dict):
            return models.Registry(
                models=[models.ModelSpec.from_dict(m) for m in cached["models"]],
                captured_at=cached["captured_at"], live=False,
                source="缓存（上游读取失败）")
        reg = models.from_snapshot()
        if last_err:
            reg.source = f"静态快照（上游读取失败：{type(last_err).__name__}）"
        return reg


pool = Pool()


def registry() -> models.Registry:
    """优先用缓存（含快照语义），面板要新鲜数据时显式 refresh。"""
    cached = db.get_setting("models_cache")
    if isinstance(cached, dict):
        return models.Registry(
            models=[models.ModelSpec.from_dict(m) for m in cached["models"]],
            captured_at=cached["captured_at"], live=True,
            source=cached.get("source", "上游实读（缓存）"))
    return models.from_snapshot()
