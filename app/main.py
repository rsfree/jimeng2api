#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用装配：路由、静态资源、启动/关闭、请求日志。

启动形态（**工厂**，不是模块级 app）：

    uvicorn "app.main:create_app" --factory
    gunicorn -c gunicorn_conf.py "app.main:create_app()"

写成 `app.main:app` 会得到 `Failed to find attribute 'app'` ——
单测全绿也照样炸，因为它们直接调 `create_app()`。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import api, audio_api, db, panel_api, prompt_api, remote_browser, security
from .config import settings
from .coordinator import coordinator
from .public_errors import HTTP_CODES, error_body, public_error
from .upstream.errors import JimengError

WEB = Path(__file__).resolve().parent.parent / "web"
log = logging.getLogger("jimeng2api")

FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="8" fill="#4a6ae8"/>
<path d="M9 22V10h5l2.5 6 2.5-6h4v12h-3v-7l-2.2 5h-2.6L13 15v7z" fill="#fff"/>
<circle cx="24.5" cy="10.5" r="2.2" fill="#7de2b8"/></svg>"""


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def create_app() -> FastAPI:
    # 库里存着 sessionid（必须可逆才能换上游请求），所以进程写出的文件一律
    # 只给属主读：DB / WAL / 日志都按这个 umask 落盘，不依赖部署者记得 chmod。
    os.umask(0o077)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init()
        security.bootstrap()
        if settings.coordinator_enabled:
            coordinator.start()
        log.info("jimeng2api %s 就绪：%s:%s（DB %s）", settings.version,
                 settings.host, settings.port, settings.db_path)
        try:
            yield
        finally:
            await coordinator.stop()
            from .pool import pool
            pool.close_all()

    app = FastAPI(title="jimeng2api", version=settings.version,
                  docs_url="/docs", redoc_url=None, lifespan=lifespan)

    app.include_router(api.router)
    app.include_router(audio_api.router)
    app.include_router(prompt_api.router)
    app.include_router(panel_api.router)
    app.include_router(panel_api.login_router)

    # ----------------------------------------------------- 远程浏览器 WebSocket
    @app.websocket("/panel/ws/remote")
    async def remote_ws(ws: WebSocket) -> None:
        """CDP screencast → 浏览器；鼠标键盘 → CDP。

        鉴权走一次性 ticket（WebSocket 不能可靠地设 Authorization 头），
        ticket 由 `POST /panel/api/remote/start` 在管理员鉴权后签发。
        """
        if not remote_browser.verify_ticket(ws.query_params.get("ticket", "")):
            await ws.close(code=4401)
            return
        await ws.accept()
        session = remote_browser.attach()
        await ws.send_json({"type": "state", "state": session.state,
                            "meta": session.meta})

        async def reader() -> None:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict):
                    session.send(msg)

        task = asyncio.create_task(reader())
        closed_sent = False
        try:
            while True:
                frame = session.pop_frame()
                if frame:
                    await ws.send_bytes(frame)
                while True:
                    ev = session.pop_event()
                    if not ev:
                        break
                    await ws.send_json(ev)
                if session.state in ("closed", "error") and not closed_sent:
                    closed_sent = True
                    await ws.send_json({"type": "state", "state": session.state,
                                        "message": session.error})
                    break
                await asyncio.sleep(0.03)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - 连接断了不需要堆栈吓人
            pass
        finally:
            task.cancel()
            remote_browser.detach(session)

    # ----------------------------------------------------------- 静态资源
    if WEB.exists():
        app.mount("/panel/assets", StaticFiles(directory=str(WEB)),
                  name="panel-assets")

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        return Response(status_code=307, headers={"Location": "/panel/"})

    @app.get("/panel", include_in_schema=False)
    def panel_noredirect() -> Response:
        return Response(status_code=307, headers={"Location": "/panel/"})

    @app.get("/panel/", include_in_schema=False)
    def panel() -> FileResponse:
        return FileResponse(WEB / "index.html")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        return Response(FAVICON, media_type="image/svg+xml")

    # ------------------------------------------------------------- 探活
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok", "version": settings.version}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        ok = db.scalar("SELECT 1", (), 0) == 1
        if settings.coordinator_enabled:
            ok = ok and coordinator.running
        return JSONResponse(status_code=200 if ok else 503,
                            content={"ready": ok})

    # --------------------------------------------------------- 请求日志
    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        path = request.url.path
        interesting = (
            path.startswith(("/v1/", "/async/"))
            or (path.startswith("/panel/api/") and request.method != "GET")
        )
        if interesting:
            try:
                db.log_request(
                    api_key_id=None, task_id=None, method=request.method,
                    path=path, status=response.status_code,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    ip=_client_ip(request),
                    ua=request.headers.get("user-agent") or "")
            except Exception:  # noqa: BLE001 - 日志失败不能影响响应
                log.debug("请求日志写入失败", exc_info=True)
        if path.startswith("/panel/assets/"):
            # 面板是自建后台、更新频率高：让浏览器每次校验，避免"改了代码看不到"
            response.headers["cache-control"] = "no-cache, must-revalidate"
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("referrer-policy", "same-origin")
        response.headers.setdefault("x-frame-options", "DENY")
        return response

    # ----------------------------------------------- 错误：客户端面只出标准码
    @app.exception_handler(JimengError)
    async def jimeng_error_handler(_: Request, exc: JimengError) -> JSONResponse:
        status, code, message = public_error(exc)
        # 原生错误只进服务端日志（含 ret / errmsg）；客户端拿不到其中任何一个字。
        # 归因按「谁的错」翻：上游凭证失效是 502，不是 401 —— 客户端没写错。
        log.warning("业务异常：kind=%s ret=%s %s",
                    exc.kind, getattr(exc, "ret", None), exc)
        headers = {"Retry-After": "30"} if status == 429 else None
        return JSONResponse(status_code=status,
                            content=error_body(code, message), headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        """鉴权 / 路由类错误的统一信封。

        `detail` 是我们自己写的标准文案（"缺少 Bearer Key"），可以给客户端；
        内部枚举（old：kind/ret）一律不进 body。
        """
        code = HTTP_CODES.get(exc.status_code, "internal_error")
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(code, str(exc.detail)),
                            headers=dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request,
                               exc: RequestValidationError) -> JSONResponse:
        """422 只回字段路径，**绝不回显 input**。

        pydantic 的默认错误体带 `input`：客户端传一个 data URI 垫图时，
        几 MB 的 base64 会被原样抄回响应；既是带宽浪费，也是把客户数据
        又复制了一份到日志和浏览器里。
        """
        fields = []
        for err in exc.errors():
            loc = [str(x) for x in (err.get("loc") or []) if x != "body"]
            if loc:
                fields.append(".".join(loc))
        names = ", ".join(sorted(set(fields))[:6]) or "body"
        return JSONResponse(status_code=422, content=error_body(
            "invalid_request", f"请求参数格式错误：{names}"))

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理异常：%s", exc)
        return JSONResponse(status_code=500,
                            content=error_body("internal_error", "网关内部错误"))

    return app
