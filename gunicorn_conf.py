#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gunicorn 配置。

🔴 `workers = 1` 是**硬约束**：协调器是"唯一执行者"，多 worker 会让同一批
queued 任务被两个进程各提交一次 = 双倍计费。并发靠线程（I/O 密集），
不靠多进程；要扩就分片账号池，不要加 worker。
"""
import os

bind = f"{os.environ.get('JIMENG2API_HOST', '0.0.0.0')}:{os.environ.get('JIMENG2API_PORT', '8300')}"
workers = 1
worker_class = "gthread"
threads = max(8, int(os.environ.get("JIMENG2API_THREADS", "16")))
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("JIMENG2API_LOG_LEVEL", "info").lower()
capture_output = True
