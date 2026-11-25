#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""即梦 HTTP 客户端 —— 一个 sessionid 打通全链路。

唯一硬前提是 cookie 里的 `sessionid`；`sign` 头照带（纯 MD5），
`sign-ver` / `device-time` / `pf` / `appvr` 与它同源。

设计要点：
* **每个账号一个客户端**（连接池按账号隔离），账号之间不共享 cookie；
* 写接口（`aigc_draft/generate`）**不做自动重试** —— 建任务是计费动作，
  超时/网络错误后重发可能导致"两条都成功、扣两次"。重试决策在协调器，
  且只对上游**明确拒绝**（限流/风控）的情况重试；
* 所有请求都过 `_post` 一个收缝，埋点只在这里做一次。
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Callable, Sequence

import httpx

from . import postedit, video, protocol
from .errors import (JimengError, JimengNetworkError, raise_for_ret)
from .sign import sign_headers

PATH_SUBMIT = "/mweb/v1/aigc_draft/generate"
PATH_HISTORY = "/mweb/v1/get_history_by_ids"
PATH_COMMON_CONFIG = "/mweb/v1/get_common_config"
PATH_IMAGE_BY_URI = "/mweb/v1/get_image_by_uri"
PATH_UPLOAD_TOKEN = "/mweb/v1/get_upload_token"
PATH_USER_INFO = "/mweb/v1/get_user_info"
PATH_WORKSPACE_LIST = "/mweb/v1/workspace/list"
PATH_SALIENCY_SEG = "/mweb/v1/saliency_seg"
PATH_CREDIT = "/commerce/v1/benefits/user_credit"
PATH_CREDIT_HISTORY = "/commerce/v1/benefits/user_credit_history"

EventHook = Callable[[dict], None]


class JimengClient:
    def __init__(self, sessionid: str, *, base: str, origin: str, aid: str,
                 da_version: str, web_version: str, appvr: str, pf: str,
                 app_sdk_version: str, ua: str, timeout: float = 45.0,
                 proxy: str = "", secsdk_signature: str = "",
                 secsdk_expire: str = "", account_id: int | None = None,
                 on_event: EventHook | None = None) -> None:
        sid = (sessionid or "").strip()
        if not sid:
            raise JimengError("缺少 sessionid —— 即梦唯一的硬前提凭据", kind="auth")
        self.sessionid = sid
        self.base = base.rstrip("/")
        self.origin = origin.rstrip("/")
        self.aid = aid
        self.da_version = da_version
        self.web_version = web_version
        self.appvr = appvr
        self.pf = pf
        self.app_sdk_version = app_sdk_version
        self.ua = ua
        self.account_id = account_id
        self.on_event = on_event
        self.web_id = "%032x" % random.getrandbits(128)
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout, connect=min(15.0, timeout)),
            "follow_redirects": True,
            "headers": {"user-agent": ua},
        }
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.Client(**kwargs)
        self._secsdk = (secsdk_signature, secsdk_expire)

    # ------------------------------------------------------------------ 基础
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JimengClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _url(self, path: str, extra: dict[str, str] | None = None) -> str:
        q = {
            "aid": self.aid, "device_platform": "web", "region": "cn",
            "webId": self.web_id, "da_version": self.da_version,
            "web_version": self.web_version, "aigc_features": "app_lip_sync",
        }
        q.update(extra or {})
        return f"{self.base}{path}?" + "&".join(f"{k}={v}" for k, v in q.items())

    def _headers(self, path: str) -> dict[str, str]:
        h = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "app-sdk-version": self.app_sdk_version,
            "appid": self.aid,
            "appvr": self.appvr,
            "content-type": "application/json",
            "lan": "zh-Hans",
            "loc": "cn",
            "origin": self.origin,
            "pf": self.pf,
            "priority": "u=1, i",
            "referer": self.origin + "/ai-tool/generate",
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", '
                         '"Google Chrome";v="152"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "tdid": "",
            "cookie": f"sessionid={self.sessionid}",
            "user-agent": self.ua,
        }
        if self._secsdk[0]:
            h["x-secsdk-web-signature"] = self._secsdk[0]
            h["x-secsdk-web-expire"] = self._secsdk[1]
            h["x-secsdk-sign-config"] = "1_5000"
        h.update(sign_headers(path, pf=self.pf, appvr=self.appvr))
        return h

    def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def _post(self, path: str, body: dict, *,
              extra_q: dict[str, str] | None = None,
              task_id: str | None = None) -> dict:
        started = time.monotonic()
        status: int | None = None
        payload: dict = {}
        try:
            resp = self._client.post(
                self._url(path, extra_q), headers=self._headers(path),
                content=json.dumps(body, ensure_ascii=False).encode(),
            )
            status = resp.status_code
            if status >= 500:
                raise JimengNetworkError(
                    f"上游 {status}：{resp.text[:160]}", retryable=True)
            try:
                payload = resp.json()
            except ValueError as e:
                raise JimengNetworkError(
                    f"上游返回非 JSON（HTTP {status}）：{resp.text[:160]!r}",
                    retryable=status >= 500) from e
            ret = payload.get("ret")
            if str(ret) != "0":
                raise_for_ret(ret, payload)
            if path == PATH_SUBMIT:
                # 成功提交也要留痕：它是**唯一计费动作**，日志里没有它就只剩失败样本
                self._emit({
                    "path": path, "task_id": task_id, "status": status,
                    "ret": "0", "kind": "",
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "error": "",
                })
            return payload
        except httpx.TimeoutException as e:
            raise JimengNetworkError(f"上游超时：{e}", retryable=False) from e
        except httpx.HTTPError as e:
            raise JimengNetworkError(f"上游连接失败：{e}", retryable=False) from e
        except JimengError as e:
            self._emit({
                "path": path, "task_id": task_id, "status": status,
                "ret": str(payload.get("ret", "")), "kind": e.kind,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": str(e),
            })
            raise

    # ------------------------------------------------------------- 零成本读
    def user_info(self, sec_uid: str = "") -> dict:
        """账号资料。**body 需要的不是空对象，而是 `{"sec_uid": ...}`**。

        实测（2026-11-18 抓包）：空 body 会得到 `ret=1000 invalid parameter`，
        即使 sessionid 完全有效。`sec_uid` 从 `get_common_config` 的
        `data.sec_uid` 拿（同一账号、免费接口），所以探活顺序必须是
        common_config → user_info → credits。
        """
        body = {"sec_uid": sec_uid} if sec_uid else {}
        return self._post(PATH_USER_INFO, body).get("data") or {}

    def credits(self) -> dict:
        """账户积分（只读）。返回 `{total, detail, expiring}`。

        `data.credit` 三个桶：vip / gift / purchase；到账延期结算的消耗
        不会出现在这里，所以面板把它标成"快照"而不是"实时余额"。
        """
        data = self._post(PATH_CREDIT, {}).get("data") or {}
        credit = data.get("credit") or {}
        total = sum(int(credit.get(k) or 0)
                    for k in ("vip_credit", "gift_credit", "purchase_credit"))
        return {
            "total": total,
            "detail": credit,
            "expiring": data.get("expiring_credits") or [],
            "raw": data,
        }

    def credit_history(self, *, count: int = 20, history_type: int = 2,
                       cursor: str = "0") -> dict:
        """消耗记录（`history_type=2`）。用于按 submit_id 对账真实扣费。

        🔴 `count` **上限是 20**：实测 30/50 都返回 `ret=1000 invalid param`
        （2026-11-19，真实账号）。所以这里强制夹紧，翻页靠 `new_cursor`。
        """
        count = max(1, min(int(count), 20))
        return self._post(PATH_CREDIT_HISTORY, {
            "count": count, "cursor": str(cursor), "history_type": history_type,
        }).get("data") or {}

    def common_config(self) -> dict:
        return self._post(PATH_COMMON_CONFIG, {}, extra_q={
            "needCache": "true", "needRefresh": "false",
        }).get("data") or {}

    def saliency_seg(self, uris: Sequence[str], mode: str = "canvas") -> list[dict]:
        """主体分割（抠图）。**免费**，返回 `[{mask: {uri, url}}]`。

        实测（2026-11-19）：`mask.url` 是 RGBA PNG，**alpha 通道即蒙版** ——
        主体保留原色、背景 alpha=0，直接就是透明底结果，不需要二次合成。
        """
        lst = [u for u in ([uris] if isinstance(uris, str) else list(uris)) if u]
        if not lst:
            return []
        data = self._post(PATH_SALIENCY_SEG,
                          {"image_uri_list": lst, "mode": mode}).get("data")
        return list(data or [])

    def workspaces(self) -> list[dict]:
        """账号工作区列表（免费只读）。`pro_hd` 的 `extend.workspace_id` 取这里。"""
        data = self._post(PATH_WORKSPACE_LIST, {}).get("data") or {}
        return list(data.get("workspaces") or [])

    def image_by_uri(self, uris: str | Sequence[str]) -> dict[str, dict]:
        lst = [uris] if isinstance(uris, str) else list(uris)
        lst = [u for u in lst if u]
        if not lst:
            return {}
        d = self._post(PATH_IMAGE_BY_URI, {"uris": lst})
        return d.get("uri2image") or {}

    # --------------------------------------------------------------- 计费写
    def submit_postedit(self, *, ability: postedit.Ability, source: dict,
                        submit_id: str, resolution: str = "",
                        count: int = 1, extra: dict | None = None,
                        workspace_id: str = "", prompt: str = "") -> str:
        """后编辑提交（智能超清 / 超清 / 细节修复 / 扩图）。

        与 t2i/i2i 共用 `/mweb/v1/aigc_draft/generate`，差别全在草稿与 `extend`。
        `pro_hd` 的草稿形态来自 2026-11-19 的真实抓包（docs/evidence/）。
        """
        # 档位留空时取能力表第一项，与 service.create_task 同源。
        # 写死 "2k" 会让 smart-edit 默认走 2K（6 积分）而不是标称的 1.5K（4 积分）。
        if not resolution:
            resolution = ability.resolutions[0] if ability.resolutions else "2k"
        body = postedit.build_submit_body(
            ability=ability, source=source, submit_id=submit_id, aid=int(self.aid),
            resolution=resolution, count=count, extra=extra,
            workspace_id=workspace_id, prompt=prompt)
        self._post(PATH_SUBMIT, body, task_id=submit_id)
        return submit_id

    def post_json(self, path: str, body: dict) -> dict:
        """通用 JSON POST —— 给非图片族能力（如 TTS）复用鉴权、sign 与错误分类。

        只做透传，不改 `ret` 语义：`ret != 0` 仍按 `raise_for_ret` 抛 JimengError，
        由调用方的适配器决定怎么归一。
        """
        return self._post(path, body)

    def submit_video(self, *, model, submit_id: str, prompt: str,
                     duration_ms: int, resolution: str,
                     aspect_ratio: str,
                     first_frame: dict | None = None,
                     end_frame: dict | None = None) -> str:
        """视频提交（`video_base_component` + `gen_video`）。

        与后编辑同一个端点，但草稿是另一棵树（见 `app/upstream/video.py`）。
        实测：720p/4s 提交 ret=0，48 次轮询后 success，产物 mp4 可直接下载。
        """
        body = video.build_submit_body(
            model=model, submit_id=submit_id, prompt=prompt,
            duration_ms=duration_ms, resolution=resolution,
            aspect_ratio=aspect_ratio, aid=str(self.aid),
            first_frame=first_frame, end_frame=end_frame)
        self._post(PATH_SUBMIT, body, task_id=submit_id)
        return submit_id

    def submit_t2i(self, *, prompt: str, model: str, count: int, width: int,
                   height: int, submit_id: str, seed: int | None = None,
                   negative_prompt: str = "",
                   resolution_type: str | None = None) -> str:
        draft = protocol.build_t2i_draft(
            prompt=prompt, model=model, count=count, width=width, height=height,
            da_version=self.da_version, seed=seed,
            negative_prompt=negative_prompt, resolution_type=resolution_type,
        )
        body = protocol.submit_body(
            draft=draft, submit_id=submit_id, model=model, count=count,
            aid=self.aid, mode="t2i")
        self._post(PATH_SUBMIT, body, task_id=submit_id)
        return submit_id

    def submit_i2i(self, *, prompt: str, image_uris: Sequence[str], model: str,
                   count: int, width: int, height: int, submit_id: str,
                   strength: float = 0.5,
                   resolution_type: str | None = None) -> str:
        draft = protocol.build_blend_draft(
            prompt=prompt, image_uris=image_uris, model=model, count=count,
            width=width, height=height, da_version=self.da_version,
            strength=strength, resolution_type=resolution_type,
        )
        body = protocol.submit_body(
            draft=draft, submit_id=submit_id, model=model, count=count,
            aid=self.aid, mode="i2i")
        self._post(PATH_SUBMIT, body, task_id=submit_id)
        return submit_id

    # ------------------------------------------------------------- 任务查询
    def fetch_many(self, submit_ids: list[str]) -> dict[str, dict]:
        ids = [s for s in submit_ids if s]
        if not ids:
            return {}
        data = self._post(PATH_HISTORY, {"submit_ids": ids}).get("data") or {}
        out: dict[str, dict] = {}
        for sid in ids:
            node = data.get(sid)
            if isinstance(node, dict):
                out[sid] = protocol.parse_task(sid, node)
            else:
                out[sid] = {
                    "submit_id": sid, "status": 0, "status_name": "init",
                    "finished": False, "ok": False, "failed": False,
                    "progress": "", "model": None, "cost": None,
                    "fail_message": "", "images": [],
                }
        return out

    # --------------------------------------------------------------- 上传用
    def upload_token(self, scene: int = 2) -> dict:
        data = self._post(PATH_UPLOAD_TOKEN, {"scene": scene}).get("data") or {}
        need = ("access_key_id", "secret_access_key", "session_token",
                "space_name")
        missing = [k for k in need if not data.get(k)]
        if missing:
            raise JimengError(
                f"get_upload_token 缺字段 {missing}；实得 {sorted(data)}",
                kind="param")
        return data

    def http_get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        return self._client.get(url, headers=headers)

    def http_post(self, url: str, headers: dict[str, str],
                  content: bytes) -> httpx.Response:
        return self._client.post(url, headers=headers, content=content)
