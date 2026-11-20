#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gunicorn 配置。

🔴 `workers = 1` 是**硬约束**：协调器是"唯一执行者"，多 worker 会让同一批
queued 任务被两个进程各提交一次 = 双倍计费。并发靠线程（I/O 密集），
不靠多进程；要扩就分片账号池，不要加 worker。

🔴 `worker_class` 必须是 ASGI worker：FastAPI 是 ASGI 应用，用 gthread/sync
这类 WSGI worker 会直接抛 `TypeError: FastAPI.__call__() missing 1 required
positional argument: 'send'`（每个请求 500）。同步 def 端点的并发由 Starlette
的 anyio 线程池提供（默认 40 线程），不再依赖 gunicorn 的 threads 参数。
"""
import os

bind = f"{os.environ.get('JIMENG2API_HOST', '0.0.0.0')}:{os.environ.get('JIMENG2API_PORT', '8300')}"
workers = 1
worker_class = "uvicorn.workers.UvicornWorker"
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("JIMENG2API_LOG_LEVEL", "info").lower()
capture_output = True
