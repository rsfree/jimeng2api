#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地图片 → 即梦 `image_uri`（火山 ImageX 四段式，纯 stdlib 签名）。

    ① POST /mweb/v1/get_upload_token   {"scene":2}      → STS 临时凭据
    ② GET  https://<upload_domain>/?Action=ApplyImageUpload...   → UploadAddress
    ③ POST https://<UploadHost>/upload/v1/<StoreUri>    → 裸字节
    ④ POST https://<upload_domain>/?Action=CommitImageUpload...  → 可引用

**这一步不产生生成、不扣积分。**

两个照抄抓包的细节（不是笔误）：
* `ApplyImageUpload` 的 SignedHeaders 是 `x-amz-date;x-amz-security-token`（不含 host）；
* `CommitImageUpload` 是 `content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token`。

另有两个实测事实决定了实现形态：
* STS 下发的 `region` 可能是站点别名（`cn`），必须归一成 `cn-north-1`，
  否则 ApplyImageUpload 恒 `SignatureDoesNotMatch`；
* `image_uri` **不是内容寻址**的 —— 同一份字节传两次得到两个不同 uri，
  所以这里按 sha256 做缓存 + in-flight 去重，避免重复素材。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import zlib
from typing import Any

import httpx

from .client import JimengClient
from .errors import JimengError

API_VERSION = "2018-08-01"
REGION = "cn-north-1"
SERVICE = "imagex"
REGION_ALIAS = {"cn": "cn-north-1"}
STS_REUSE_SECONDS = 1500.0
URI_CACHE_TTL = 6 * 3600.0
URI_CACHE_MAX = 128


class ImageXError(JimengError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, kind="upload", retryable=retryable)


def _aws_region(token: dict, host: str) -> str:
    r = token.get("region")
    if r:
        return REGION_ALIAS.get(str(r), str(r))
    first = (host or "").split(".")[0]
    if first.startswith("imagex-") and first != "imagex-":
        return first[len("imagex-"):]
    return REGION


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _enc(s: str, *, encode_slash: bool = True) -> str:
    return urllib.parse.quote(s, safe="-_.~" if encode_slash else "-_.~/")


def aws4_sign(*, method: str, host: str, path: str, query: dict[str, str],
              access_key: str, secret_key: str, session_token: str,
              payload: bytes, signed_headers: tuple[str, ...],
              extra_headers: dict[str, str] | None = None,
              region: str = REGION, service: str = SERVICE) -> dict[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    hdrs = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-security-token": session_token,
        "x-amz-content-sha256": _sha256_hex(payload),
    }
    hdrs.update(extra_headers or {})
    canonical_query = "&".join(
        f"{_enc(k)}={_enc(v)}" for k, v in sorted(query.items()))
    canonical_headers = "".join(
        f"{h}:{hdrs[h].strip()}\n" for h in signed_headers)
    signed = ";".join(signed_headers)
    canonical_request = "\n".join([
        method, path, canonical_query, canonical_headers, signed,
        hdrs["x-amz-content-sha256"],
    ])
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        _sha256_hex(canonical_request.encode()),
    ])
    # 密钥推导的 region/service 必须与 scope 逐字一致，否则签名恒不匹配
    k = _hmac(("AWS4" + secret_key).encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "aws4_request")
    sig = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    out = {
        "x-amz-date": amz_date,
        "x-amz-security-token": session_token,
        "authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}"),
    }
    if "x-amz-content-sha256" in signed_headers:
        out["x-amz-content-sha256"] = hdrs["x-amz-content-sha256"]
    if "content-type" in signed_headers and extra_headers:
        out["content-type"] = extra_headers["content-type"]
    return out


def find_uri(obj: Any) -> str | None:
    """在 commit 响应里找 Uri（字段名不同版本有 Uri / uri）。"""
    if isinstance(obj, dict):
        for k in ("Uri", "uri", "StoreUri"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("tos-"):
                return v
        for v in obj.values():
            got = find_uri(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = find_uri(v)
            if got:
                return got
    return None


class ImageXUploader:
    """按账号复用一个实例（STS 与内容缓存都属于账号）。"""

    def __init__(self, client: JimengClient) -> None:
        self.c = client
        self._token: dict | None = None
        self._token_at = 0.0
        self._sts_lock = threading.Lock()
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, str, float]] = {}
        self._inflight: dict[str, threading.Event] = {}
        self.last_cached = False

    # ------------------------------------------------------------------ STS
    def token(self, *, force: bool = False) -> dict:
        if (not force and self._token
                and time.time() - self._token_at < STS_REUSE_SECONDS):
            return self._token
        with self._sts_lock:
            if (not force and self._token
                    and time.time() - self._token_at < STS_REUSE_SECONDS):
                return self._token
            t = self.c.upload_token()
            self._token, self._token_at = t, time.time()
            return t

    # ------------------------------------------------------- ② Apply（签名）
    def apply(self, file_size: int) -> dict:
        t = self.token()
        host = t.get("upload_domain") or "imagex.bytedanceapi.com"
        query = {"Action": "ApplyImageUpload", "Version": API_VERSION,
                 "ServiceId": t["space_name"], "FileSize": str(file_size)}
        headers = aws4_sign(
            method="GET", host=host, path="/", query=query,
            access_key=t["access_key_id"],
            secret_key=t["secret_access_key"],
            session_token=t["session_token"], payload=b"",
            signed_headers=("x-amz-date", "x-amz-security-token"),
            region=_aws_region(t, host))
        url = f"https://{host}/?" + "&".join(f"{k}={v}" for k, v in query.items())
        try:
            r = self.c.http_get(url, headers)
        except httpx.HTTPError as e:
            raise ImageXError(f"ApplyImageUpload 失败：{e}", retryable=True) from e
        res = self._unwrap(r, "ApplyImageUpload")
        addr = res.get("UploadAddress") or res.get("uploadAddress")
        if not isinstance(addr, dict):
            raise ImageXError(f"ApplyImageUpload 无 UploadAddress：{sorted(res)}")
        return addr

    # ---------------------------------------------------------- ③ 传裸字节
    def put(self, addr: dict, data: bytes) -> None:
        si = (addr.get("StoreInfos") or addr.get("storeInfos") or [{}])[0]
        uri = si.get("StoreUri") or si.get("storeUri")
        auth = si.get("Auth") or si.get("auth")
        hosts = addr.get("UploadHosts") or addr.get("uploadHosts") or []
        if not hosts and addr.get("UploadHost"):
            hosts = [addr["UploadHost"]]
        if not (uri and auth and hosts):
            raise ImageXError(
                f"UploadAddress 缺字段（uri={bool(uri)} auth={bool(auth)} "
                f"hosts={hosts}）")
        headers = {
            "authorization": auth,
            "content-type": "application/octet-stream",
            "content-crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
        }
        try:
            r = self.c.http_post(f"https://{hosts[0]}/upload/v1/{uri}",
                                 headers, data)
        except httpx.HTTPError as e:
            raise ImageXError(f"上传字节失败：{e}", retryable=True) from e
        if r.status_code >= 400:
            raise ImageXError(
                f"上传字节被拒：HTTP {r.status_code} {r.text[:160]!r}",
                retryable=r.status_code >= 500)

    # ------------------------------------------------------- ④ Commit
    def commit(self, addr: dict) -> dict:
        t = self.token()
        host = t.get("upload_domain") or "imagex.bytedanceapi.com"
        session_key = addr.get("SessionKey") or base64.b64encode(
            json.dumps(addr, ensure_ascii=False,
                       separators=(",", ":")).encode()).decode()
        body = json.dumps({"SessionKey": session_key},
                          separators=(",", ":")).encode()
        query = {"Action": "CommitImageUpload", "Version": API_VERSION,
                 "ServiceId": t["space_name"]}
        headers = aws4_sign(
            method="POST", host=host, path="/", query=query,
            access_key=t["access_key_id"],
            secret_key=t["secret_access_key"],
            session_token=t["session_token"], payload=body,
            signed_headers=("content-type", "host", "x-amz-content-sha256",
                            "x-amz-date", "x-amz-security-token"),
            extra_headers={"content-type": "application/json"},
            region=_aws_region(t, host))
        url = f"https://{host}/?" + "&".join(f"{k}={v}" for k, v in query.items())
        try:
            r = self.c.http_post(url, headers, body)
        except httpx.HTTPError as e:
            raise ImageXError(f"CommitImageUpload 失败：{e}", retryable=True) from e
        return self._unwrap(r, "CommitImageUpload")

    # ------------------------------------------------------------- 一步到位
    def upload(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        hit = self._cache.get(digest)
        if hit and time.time() - hit[2] < URI_CACHE_TTL:
            self.last_cached = True
            return hit[0]
        claim = threading.Event()
        with self._lock:
            hit = self._cache.get(digest)
            if hit and time.time() - hit[2] < URI_CACHE_TTL:
                self.last_cached = True
                return hit[0]
            waiter = self._inflight.get(digest)
            leader = waiter is None
            if leader:
                self._inflight[digest] = claim
        if not leader:
            if not waiter.wait(timeout=120):
                raise ImageXError("等待同一份内容上传超时")
            hit = self._cache.get(digest)
            if hit:
                self.last_cached = True
                return hit[0]
            raise ImageXError("同一份内容的上传刚失败")
        try:
            self.last_cached = False
            t = self.token()
            addr = self.apply(len(data))
            si = (addr.get("StoreInfos") or addr.get("storeInfos") or [{}])[0]
            store_uri = si.get("StoreUri") or si.get("storeUri")
            self.put(addr, data)
            out = self.commit(addr)
            uri = find_uri(out) or store_uri
            if not uri:
                raise ImageXError(f"commit 成功但拿不到 Uri：{json.dumps(out)[:200]}")
            self._cache[digest] = (uri, t["space_name"], time.time())
            if len(self._cache) > URI_CACHE_MAX:
                for k in sorted(self._cache, key=lambda x: self._cache[x][2])[
                        :len(self._cache) - URI_CACHE_MAX]:
                    self._cache.pop(k, None)
            return uri
        finally:
            with self._lock:
                self._inflight.pop(digest, None)
            claim.set()

    @staticmethod
    def _unwrap(r: httpx.Response, what: str) -> dict:
        try:
            d = r.json()
        except ValueError as e:
            raise ImageXError(
                f"{what} 返回非 JSON（HTTP {r.status_code}）：{r.text[:160]!r}",
                retryable=r.status_code >= 500) from e
        if r.status_code >= 400 or (d.get("ResponseMetadata") or {}).get("Error"):
            err = (d.get("ResponseMetadata") or {}).get("Error") or {}
            raise ImageXError(
                f"{what} 失败：HTTP {r.status_code} "
                f"code={err.get('Code')} msg={err.get('Message')}")
        return d.get("Result", d)
