#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领域查询：账号 / 任务 / 密钥 / 日志 / 用量 / 运行期配置。

所有 SQL 只在这里出现一次 —— 面板和业务接口都走这些函数，
避免"面板看到的口径"和"调度用的口径"两套实现悄悄分叉。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from . import db

#: 面板可改的运行期参数（存 settings 表）。改这里 = 改默认值，
#: 不改已写入库的值。
RUNTIME_DEFAULTS: dict[str, Any] = {
    "concurrency": 4,            # 全局在途任务上限
    "per_account": 2,            # 单账号在途上限（上游容忍 4，保守取 2）
    "poll_interval": 2.0,        # 协调器轮询间隔（秒）
    "task_timeout": 900,         # 单任务总超时（秒）
    "sync_timeout": 300,         # 同步接口最长等待（秒）
    #: 后编辑（超清/细节修复/扩图）在同步端点上的等待秒数。
    #: 0 = 不等待，直接 202 + poll_url（默认）。一次 2K 超清实测 38.5s，
    #: 4K/8K 更久，占着连接等只会把"超时降级"变成常态。
    "postedit_sync_wait": 0,
    "default_model": "high_aes_general_v55",
    "cooldown_rate": 60,         # 限流冷却（秒）
    "cooldown_risk": 1800,       # 风控冷却（秒）
    "max_retry": 1,              # 仅对"上游明确拒绝"的提交失败重试
    "credit_low_threshold": 100, # 低积分告警线
    "models_live_refresh": 1800, # 能力表自动刷新间隔（秒）
}


def runtime_config() -> dict[str, Any]:
    cfg = dict(RUNTIME_DEFAULTS)
    for row in db.query("SELECT key, value FROM settings"):
        if row["key"] in cfg:
            cfg[row["key"]] = db.loads(row["value"], cfg[row["key"]])
    return cfg


def save_runtime_config(patch: dict[str, Any]) -> dict[str, Any]:
    for k, v in patch.items():
        if k not in RUNTIME_DEFAULTS:
            continue
        db.put_setting(k, v)
    return runtime_config()


# --------------------------------------------------------------------- 账号
ACCOUNT_FIELDS = ("uid", "name", "remark", "enabled", "status", "credits",
                  "credit_detail", "member_level", "expires_at",
                  "cooldown_until", "last_error", "last_checked",
                  "last_used_at", "success_count", "fail_count", "inflight")


def account_row(row) -> dict:
    d = {k: row[k] for k in row.keys()}
    # 在途数以**库里的事实**为准（重启安全、跨线程一致），
    # 不用内存计数器：信号量重启归零会让上游被超发。
    if "inflight_live" in row.keys():
        d["inflight"] = row["inflight_live"]
    d["credit_detail"] = db.loads(d.get("credit_detail"), {})
    d["session_hint"] = "•" * 8 + (d.get("sessionid") or "")[-6:]
    d.pop("sessionid", None)
    now = db.now()
    if not d["enabled"]:
        d["state"] = "disabled"
    elif d["status"] == "invalid":
        d["state"] = "invalid"
    elif d["cooldown_until"] and d["cooldown_until"] > now:
        d["state"] = "cooling"
    elif d["status"] == "quota":
        d["state"] = "quota"
    elif d["status"] == "ok":
        d["state"] = "ok"
    else:
        d["state"] = "unknown"
    d["cooldown_left"] = max(0, int((d["cooldown_until"] or 0) - now))
    return d


_ACCOUNT_SELECT = (
    "SELECT a.*, (SELECT COUNT(*) FROM tasks t WHERE t.account_id = a.id "
    "AND t.status IN ('submitting','running')) AS inflight_live FROM accounts a"
)


def list_accounts() -> list[dict]:
    rows = db.query(_ACCOUNT_SELECT + " ORDER BY a.id ASC")
    return [account_row(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    row = db.one(_ACCOUNT_SELECT + " WHERE a.id = ?", (account_id,))
    return account_row(row) if row else None


def account_secret(account_id: int) -> str | None:
    row = db.one("SELECT sessionid FROM accounts WHERE id = ?", (account_id,))
    return row["sessionid"] if row else None


def add_account(sessionid: str, remark: str = "") -> int:
    now = db.now()
    return db.execute(
        "INSERT INTO accounts(sessionid, remark, created_at, updated_at) "
        "VALUES(?,?,?,?)", (sessionid.strip(), remark.strip(), now, now))


def update_account(account_id: int, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in ACCOUNT_FIELDS}
    if not allowed:
        return
    if "credit_detail" in allowed:
        allowed["credit_detail"] = db.dumps(allowed["credit_detail"])
    sets = ", ".join(f"{k} = ?" for k in allowed)
    db.execute(f"UPDATE accounts SET {sets}, updated_at = ? WHERE id = ?",
               (*allowed.values(), db.now(), account_id))


def delete_account(account_id: int) -> None:
    db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


def account_stats() -> dict:
    rows = db.query(_ACCOUNT_SELECT)
    now = db.now()
    out = {"total": len(rows), "ok": 0, "cooling": 0, "disabled": 0,
           "invalid": 0, "quota": 0, "credits": 0, "inflight": 0}
    for r in rows:
        if not r["enabled"]:
            out["disabled"] += 1
            continue
        if r["status"] == "invalid":
            out["invalid"] += 1
        elif r["cooldown_until"] and r["cooldown_until"] > now:
            out["cooling"] += 1
        elif r["status"] == "quota":
            out["quota"] += 1
        elif r["status"] == "ok":
            out["ok"] += 1
        out["credits"] += r["credits"] or 0
        out["inflight"] += r["inflight_live"] or 0
    return out


def dispatch_candidates(per_account: int, cooldown_risk: int) -> list[dict]:
    """可接单的账号：启用、非失效、冷却已过、在途未满。

    排序 = **积分降序 + 最近使用升序**：让高余额账号先用、同时把流量摊开，
    避免"永远打同一个号"导致单号先被风控。
    """
    now = db.now()
    rows = db.query(
        _ACCOUNT_SELECT + " WHERE a.enabled = 1 "
        "AND a.status IN ('ok','unknown') "
        "AND (a.cooldown_until IS NULL OR a.cooldown_until <= ?) "
        "AND (SELECT COUNT(*) FROM tasks t WHERE t.account_id = a.id "
        "     AND t.status IN ('submitting','running')) < ? "
        "ORDER BY CASE WHEN a.credits IS NULL THEN 1 ELSE a.credits END DESC, "
        "COALESCE(a.last_used_at,0) ASC, a.id ASC",
        (now, per_account),
    )
    return [account_row(r) for r in rows]


def record_account_result(account_id: int, *, ok: bool, error: str = "",
                          kind: str = "") -> None:
    if ok:
        db.execute(
            "UPDATE accounts SET success_count = success_count + 1, "
            "status = 'ok', last_error = '', last_used_at = ?, updated_at = ? "
            "WHERE id = ?", (db.now(), db.now(), account_id))
        return
    status = "ok"
    if kind == "auth":
        status = "invalid"
    elif kind == "quota":
        status = "quota"
    db.execute(
        "UPDATE accounts SET fail_count = fail_count + 1, status = ?, "
        "last_error = ?, updated_at = ? WHERE id = ?",
        (status, error[:300], db.now(), account_id))


def set_cooldown(account_id: int, seconds: int, reason: str) -> None:
    db.execute(
        "UPDATE accounts SET cooldown_until = ?, last_error = ?, updated_at = ? "
        "WHERE id = ?", (db.now() + max(1, seconds), reason[:300], db.now(),
                         account_id))


# --------------------------------------------------------------- 模型重定向
def list_redirects() -> list[dict]:
    rows = db.query("SELECT * FROM model_redirects ORDER BY alias ASC")
    return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]


def redirect_map() -> dict[str, str]:
    """运行时实际生效的映射（只含启用项）。alias 已归一化为小写。"""
    rows = db.query(
        "SELECT alias, target FROM model_redirects WHERE enabled = 1")
    return {r["alias"]: r["target"] for r in rows}


def get_redirect(rid: int) -> dict | None:
    row = db.one("SELECT * FROM model_redirects WHERE id = ?", (rid,))
    return (dict(row) | {"enabled": bool(row["enabled"])}) if row else None


def redirect_by_alias(alias: str) -> dict | None:
    row = db.one("SELECT * FROM model_redirects WHERE alias = ?", (alias,))
    return (dict(row) | {"enabled": bool(row["enabled"])}) if row else None


def create_redirect(alias: str, target: str, note: str = "",
                    enabled: bool = True) -> int:
    now = db.now()
    return db.execute(
        "INSERT INTO model_redirects(alias, target, note, enabled, created_at,"
        " updated_at) VALUES(?,?,?,?,?,?)",
        (alias, target, note, int(enabled), now, now))


def update_redirect(rid: int, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items()
               if k in ("target", "note", "enabled")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    db.execute(f"UPDATE model_redirects SET {sets}, updated_at = ? WHERE id = ?",
               (*allowed.values(), db.now(), rid))


def delete_redirect(rid: int) -> None:
    db.execute("DELETE FROM model_redirects WHERE id = ?", (rid,))


# --------------------------------------------------------------------- 密钥
def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return "sk-" + secrets.token_hex(24)


def create_key(name: str, *, is_admin: bool = False, quota: int = 0,
               key: str | None = None) -> str:
    raw = key or new_key()
    db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, is_admin, quota_credits,"
        " created_at) VALUES(?,?,?,?,?,?)",
        (name.strip() or "未命名", hash_key(raw), raw[:11], int(is_admin),
         int(quota), db.now()))
    return raw


def key_by_raw(key: str) -> dict | None:
    row = db.one("SELECT * FROM api_keys WHERE key_hash = ?",
                 (hash_key(key),))
    return dict(row) if row else None


def list_keys() -> list[dict]:
    rows = db.query(
        "SELECT id, name, key_prefix, is_admin, enabled, quota_credits,"
        " used_credits, request_count, last_used_at, created_at FROM api_keys"
        " ORDER BY id ASC")
    return [dict(r) for r in rows]


def update_key(key_id: int, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "enabled", "quota_credits", "is_admin")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    db.execute(f"UPDATE api_keys SET {sets} WHERE id = ?",
               (*allowed.values(), key_id))


def delete_key(key_id: int) -> None:
    db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))


def touch_key(key_id: int) -> None:
    db.execute(
        "UPDATE api_keys SET request_count = request_count + 1, "
        "last_used_at = ? WHERE id = ?", (db.now(), key_id))


def admin_key_exists() -> bool:
    return bool(db.scalar("SELECT COUNT(*) FROM api_keys WHERE is_admin = 1"))


def charge_key(key_id: int | None, credits: int | None) -> None:
    if not key_id or not credits:
        return
    db.execute(
        "UPDATE api_keys SET used_credits = used_credits + ? WHERE id = ?",
        (int(credits), key_id))


# --------------------------------------------------------------------- 任务
TASK_COLUMNS = (
    "id", "api_key_id", "account_id", "mode", "model", "prompt",
    "negative_prompt", "size", "width", "height", "resolution", "count",
    "seed", "image_uris", "status", "upstream_status", "progress", "images",
    "error", "error_kind", "forecast_credits", "charged_credits", "attempts",
    "submit_draft", "created_at", "started_at", "finished_at", "updated_at",
    "next_poll_at",
)


def task_row(row) -> dict:
    d = {k: row[k] for k in row.keys()}
    d["images"] = db.loads(d.get("images"), [])
    d["image_uris"] = db.loads(d.get("image_uris"), [])
    d["extras"] = db.loads(d.get("extras"), {})
    d["degradations"] = db.loads(d.get("degradations"), [])
    d.pop("submit_draft", None)
    return d


def create_task(*, task_id: str, api_key_id: int | None, mode: str,
                model: str, prompt: str, negative_prompt: str, size: str,
                width: int, height: int, resolution: str, count: int,
                seed: int | None, image_uris: list[str],
                mask_uri: str = "", extras: dict | None = None,
                degradations: list[str] | None = None) -> dict:
    now = db.now()
    db.execute(
        "INSERT INTO tasks(id, api_key_id, mode, model, prompt, negative_prompt,"
        " size, width, height, resolution, count, seed, image_uris, mask_uri,"
        " extras, degradations, status, created_at, updated_at, next_poll_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,0)",
        (task_id, api_key_id, mode, model, prompt, negative_prompt, size,
         width, height, resolution, count, seed, db.dumps(image_uris),
         mask_uri, db.dumps(extras or {}), db.dumps(degradations or []),
         now, now),
    )
    return get_task(task_id) or {}


def get_task(task_id: str) -> dict | None:
    row = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return task_row(row) if row else None


def claim_queued(limit: int) -> list[dict]:
    if limit <= 0:
        return []
    now = db.now()
    with db.tx() as c:
        rows = list(c.execute(
            "SELECT * FROM tasks WHERE status = 'queued' "
            "AND next_poll_at <= ? ORDER BY created_at ASC LIMIT ?",
            (now, limit)))
        ids = [r["id"] for r in rows]
        if ids:
            c.executemany(
                "UPDATE tasks SET status = 'submitting', updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                [(now, i) for i in ids])
    return [task_row(r) for r in rows]


def remember_assets(account_id: int, task_id: str, images: list[dict]) -> int:
    """记下产物属于哪个账号。供后续 `item:` 引用锁定账号。

    只记有 `item_id` 的条目；同一 item 重复出现用 upsert（不覆盖首次归属 ——
    一个 item 只可能属于一个账号，重复写入说明是同一条重试）。
    """
    n = 0
    now = db.now()
    for it in images or []:
        if not isinstance(it, dict):
            continue
        item_id = str(it.get("item_id") or "").strip()
        if not item_id:
            continue
        db.execute(
            "INSERT INTO assets (item_id, history_id, account_id, task_id, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET "
            "history_id = excluded.history_id, task_id = excluded.task_id",
            (item_id, str(it.get("history_id") or ""), int(account_id),
             str(task_id), now),
        )
        n += 1
    return n


def account_for_item(item_id: str) -> int | None:
    """这个作品属于哪个账号；未知返回 None（可能是账号原有作品，走正常调度）。"""
    if not item_id:
        return None
    row = db.one("SELECT account_id FROM assets WHERE item_id = ?",
                 (str(item_id),))
    return int(row["account_id"]) if row else None


def mark_task_running(task_id: str, account_id: int, *, forecast: int | None,
                      draft: str = "") -> None:
    now = db.now()
    db.execute(
        # attempts 只在 submit_task 里加（它统计的是"尝试次数"，含被拒的），
        # 这里再加一次会把一次成功记成两次尝试 —— 实跑时确实把 attempts 记成了 2。
        "UPDATE tasks SET status = 'running', account_id = ?, started_at = ?, "
        "updated_at = ?, next_poll_at = ?, "
        "forecast_credits = COALESCE(?, forecast_credits), submit_draft = ? "
        "WHERE id = ?",
        (account_id, now, now, now + 5, forecast, draft[:4000], task_id))


def mark_task_queued_retry(task_id: str, error: str) -> None:
    db.execute(
        "UPDATE tasks SET status = 'queued', error = ?, updated_at = ?, "
        "account_id = NULL WHERE id = ?", (error[:300], db.now(), task_id))


def mark_task_failed(task_id: str, error: str, kind: str = "") -> None:
    now = db.now()
    db.execute(
        "UPDATE tasks SET status = 'failed', error = ?, error_kind = ?, "
        "finished_at = ?, updated_at = ? WHERE id = ?",
        (error[:600], kind, now, now, task_id))


def update_task_poll(task_id: str, *, upstream_status: int | None,
                     progress: str, next_poll_at: int) -> None:
    db.execute(
        "UPDATE tasks SET upstream_status = ?, progress = ?, next_poll_at = ?, "
        "updated_at = ? WHERE id = ?",
        (upstream_status, progress[:32], next_poll_at, db.now(), task_id))


def finish_task(task_id: str, *, images: list[dict], forecast: int | None,
                status: str = "succeeded",
                upstream_status: int | None = None) -> None:
    now = db.now()
    db.execute(
        "UPDATE tasks SET status = ?, images = ?, forecast_credits = "
        "COALESCE(?, forecast_credits), progress = ?, upstream_status = "
        "COALESCE(?, upstream_status), finished_at = ?, updated_at = ? "
        "WHERE id = ?",
        (status, db.dumps(images), forecast, f"{len(images)} 张",
         upstream_status, now, now, task_id))


def set_charged(task_id: str, credits: int | None) -> None:
    db.execute("UPDATE tasks SET charged_credits = ? WHERE id = ?",
               (credits, task_id))


def due_tasks(now: int | None = None) -> list[dict]:
    ts = now or db.now()
    rows = db.query(
        "SELECT * FROM tasks WHERE status = 'running' AND next_poll_at <= ? "
        "ORDER BY started_at ASC LIMIT 100", (ts,))
    return [task_row(r) for r in rows]


def list_tasks(*, status: str = "", q: str = "", limit: int = 50,
               offset: int = 0, api_key_id: int | None = None) -> dict:
    where: list[str] = []
    args: list[Any] = []
    if status:
        where.append("status = ?")
        args.append(status)
    if q:
        where.append("(id LIKE ? OR prompt LIKE ? OR model LIKE ?)")
        args.extend([f"%{q}%"] * 3)
    if api_key_id is not None:
        where.append("api_key_id = ?")
        args.append(api_key_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.scalar(f"SELECT COUNT(*) FROM tasks {clause}", args, 0)
    rows = db.query(
        f"SELECT * FROM tasks {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*args, limit, offset))
    return {"total": total, "items": [task_row(r) for r in rows]}


def task_counts() -> dict:
    rows = db.query("SELECT status, COUNT(*) n FROM tasks GROUP BY status")
    out = {r["status"]: r["n"] for r in rows}
    out["total"] = sum(out.values())
    return out


def _day_start(ts: int | None = None) -> int:
    lt = time.localtime(ts or db.now())
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                            0, 0, 0, 0, 0, -1)))


def today_stats() -> dict:
    start = _day_start()
    row = db.one(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) ok, "
        "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) failed, "
        "COALESCE(SUM(COALESCE(charged_credits, 0)), 0) credits "
        "FROM tasks WHERE created_at >= ?", (start,))
    total = row["total"] or 0
    ok = row["ok"] or 0
    failed = row["failed"] or 0
    return {
        "total": total, "ok": ok, "failed": failed,
        "running": db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','submitting',"
            "'running')", (), 0),
        "credits": row["credits"] or 0,
        "success_rate": round(ok / (ok + failed), 3) if (ok + failed) else None,
    }


def trend(days: int = 7) -> list[dict]:
    start = _day_start() - (days - 1) * 86400
    rows = db.query(
        "SELECT created_at, status FROM tasks WHERE created_at >= ?", (start,))
    buckets: dict[str, dict] = {}
    for i in range(days):
        ts = start + i * 86400
        key = time.strftime("%m-%d", time.localtime(ts))
        buckets[key] = {"date": key, "total": 0, "ok": 0, "failed": 0}
    for r in rows:
        key = time.strftime("%m-%d", time.localtime(r["created_at"]))
        b = buckets.get(key)
        if not b:
            continue
        b["total"] += 1
        if r["status"] == "succeeded":
            b["ok"] += 1
        elif r["status"] == "failed":
            b["failed"] += 1
    return list(buckets.values())


def usage(days: int = 7) -> dict:
    start = _day_start() - (days - 1) * 86400
    by_model = db.query(
        "SELECT model, COUNT(*) n, "
        "SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) ok, "
        "COALESCE(SUM(COALESCE(charged_credits, forecast_credits, 0)),0) credits "
        "FROM tasks WHERE created_at >= ? GROUP BY model "
        "ORDER BY n DESC", (start,))
    by_account = db.query(
        "SELECT t.account_id, a.name, a.uid, COUNT(*) n, "
        "SUM(CASE WHEN t.status='succeeded' THEN 1 ELSE 0 END) ok, "
        "COALESCE(SUM(COALESCE(t.charged_credits, t.forecast_credits, 0)),0) credits "
        "FROM tasks t LEFT JOIN accounts a ON a.id = t.account_id "
        "WHERE t.created_at >= ? GROUP BY t.account_id "
        "ORDER BY n DESC", (start,))
    by_key = db.query(
        "SELECT t.api_key_id, k.name, COUNT(*) n, "
        "COALESCE(SUM(COALESCE(t.charged_credits, t.forecast_credits, 0)),0) credits "
        "FROM tasks t LEFT JOIN api_keys k ON k.id = t.api_key_id "
        "WHERE t.created_at >= ? GROUP BY t.api_key_id ORDER BY n DESC",
        (start,))
    totals = db.one(
        "SELECT COUNT(*) n, "
        "SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) ok, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed, "
        "COALESCE(SUM(COALESCE(charged_credits, forecast_credits, 0)),0) credits "
        "FROM tasks WHERE created_at >= ?", (start,))
    return {
        "days": days,
        "totals": {
            "requests": totals["n"] or 0,
            "ok": totals["ok"] or 0,
            "failed": totals["failed"] or 0,
            "credits": totals["credits"] or 0,
        },
        "trend": trend(days),
        "by_model": [dict(r) for r in by_model],
        "by_account": [dict(r) for r in by_account],
        "by_key": [dict(r) for r in by_key],
    }


# --------------------------------------------------------------------- 日志
def list_request_logs(*, limit: int = 100, offset: int = 0,
                      path: str = "") -> dict:
    where = "WHERE path LIKE ?" if path else ""
    args: list[Any] = [f"%{path}%"] if path else []
    total = db.scalar(f"SELECT COUNT(*) FROM request_logs {where}", args, 0)
    rows = db.query(
        f"SELECT * FROM request_logs {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
        (*args, limit, offset))
    return {"total": total, "items": [dict(r) for r in rows]}


def list_upstream_logs(*, limit: int = 100, offset: int = 0,
                       only_error: bool = False) -> dict:
    where = "WHERE ret != '0' OR error != ''" if only_error else ""
    total = db.scalar(f"SELECT COUNT(*) FROM upstream_logs {where}", (), 0)
    rows = db.query(
        f"SELECT * FROM upstream_logs {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
        (limit, offset))
    return {"total": total, "items": [dict(r) for r in rows]}


def prune_logs(keep_days: int = 14) -> None:
    cutoff = db.now() - keep_days * 86400
    db.execute("DELETE FROM request_logs WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM upstream_logs WHERE ts < ?", (cutoff,))
