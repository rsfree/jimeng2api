#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协调器：**唯一**把任务推到上游、唯一轮询状态的执行者。

它与 API 进程同生共死，但职责完全分开：

    API 线程：只写库（queued）→ 立刻返回
    协调器：  读库（queued）→ 提交 → 轮询 → 终态

单实例是硬要求：两个协调器会各自提交同一批 queued 任务 = 双倍计费。
扩展方向是"分片账号池"而不是"多副本跑同一个库"。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import db, pool as pool_mod, repo, service

log = logging.getLogger("jimeng2api.coord")


class Coordinator:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_models = 0.0
        self._last_prune = 0.0
        self.ticks = 0
        self.last_tick_at = 0.0
        self.last_error = ""
        #: 连续失败的 tick 数。tick 失败**也会**刷新 last_tick_at（否则一次
        #: 偶发异常就会被当成假死），所以心跳抓不到"每轮都失败"——只能单独计数。
        self.fail_streak = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="jimeng-coordinator")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        log.info("协调器启动")
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await asyncio.to_thread(self._tick)
                self.last_error = ""
                self.fail_streak = 0
            except Exception as e:  # noqa: BLE001 - 单次 tick 失败不能杀死循环
                self.last_error = f"{type(e).__name__}: {e}"
                self.fail_streak += 1
                log.exception("协调器 tick 失败")
            self.ticks += 1
            self.last_tick_at = time.time()
            interval = float(repo.runtime_config()["poll_interval"])
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.2, interval - elapsed))

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        cfg = repo.runtime_config()
        now = db.now()

        # 1) 提交新任务（全局并发上限）
        inflight = db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE status IN "
            "('submitting','running')", (), 0)
        slots = max(0, int(cfg["concurrency"]) - int(inflight))
        if slots:
            for task in repo.claim_queued(slots):
                try:
                    service.submit_task(task)
                except Exception as e:  # noqa: BLE001
                    # 只把 str(e) 写进任务行会让"division by zero"这类错误
                    # 完全失去定位信息 —— 堆栈必须进日志。
                    log.exception("任务提交失败 task=%s", task["id"])
                    repo.mark_task_failed(task["id"], f"提交异常：{e}", "unknown")

        # 2) 轮询在途任务（合并查询，一次拿一批）
        service.poll_once()

        # 3) 能力表定期刷新（免费读）
        step = int(cfg["models_live_refresh"])
        if step > 0 and now - self._last_models > step and repo.list_accounts():
            self._last_models = now
            try:
                pool_mod.pool.refresh_models(force=True)
            except Exception:  # noqa: BLE001
                log.warning("能力表刷新失败", exc_info=True)

        # 4) 日志清理（14 天）
        if now - self._last_prune > 6 * 3600:
            self._last_prune = now
            try:
                repo.prune_logs(14)
            except Exception:  # noqa: BLE001
                log.warning("日志清理失败", exc_info=True)


coordinator = Coordinator()
