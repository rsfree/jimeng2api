import json
import uuid

from tests.conftest import ADMIN


def test_health_and_models_are_public(client):
    assert client.get("/healthz").json()["status"] == "ok"
    models = client.get("/v1/models").json()
    assert models["object"] == "list"
    # `/v1/models` 只出对外别名：上游 model_req_key 是成本结构，不能漏
    assert models["data"], "能力清单不应为空"
    assert all(not m["id"].startswith("high_aes_") for m in models["data"])
    assert any(m["default"] for m in models["data"])
    assert client.get("/v1/credits").status_code == 401


def test_panel_requires_admin(client):
    assert client.get("/panel/api/overview").status_code == 401
    assert client.get("/panel/api/login", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 405
    ok = client.post("/panel/api/login", json={"key": ADMIN})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert client.post("/panel/api/login", json={"key": "sk-wrong"}).status_code == 401


def test_overview_shape(client, auth):
    d = client.get("/panel/api/overview", headers=auth).json()
    for key in ("accounts", "today", "tasks", "trend", "alerts", "models"):
        assert key in d
    assert d["coordinator"]["running"] is False  # 测试里 coordinator=0


def test_account_crud_without_probe(client, auth):
    r = client.post("/panel/api/accounts", json={
        "sessionid": "x" * 40, "remark": "单元测试", "probe": False,
    }, headers=auth)
    assert r.status_code == 200
    acc = r.json()["account"]
    assert acc["remark"] == "单元测试"
    assert "sessionid" not in acc and acc["session_hint"].endswith("xxxxxx")

    dup = client.post("/panel/api/accounts", json={
        "sessionid": "x" * 40, "probe": False}, headers=auth)
    assert dup.status_code == 409

    off = client.patch(f"/panel/api/accounts/{acc['id']}",
                       json={"enabled": False}, headers=auth)
    assert off.json()["account"]["state"] == "disabled"

    assert client.delete(f"/panel/api/accounts/{acc['id']}",
                         headers=auth).status_code == 200


def test_playground_dry_run_builds_draft_without_task(client, auth):
    r = client.post("/panel/api/tasks", json={
        "prompt": "一只猫", "model": "seedream-5.5", "size": "2048x2048",
        "count": 2, "dry_run": True,
    }, headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["dry_run"] is True
    assert d["model"] == "high_aes_general_v55"
    assert d["count"] == 2
    draft = json.loads(d["draft"])
    assert draft["component_list"][0]["abilities"]["gen_option"]["gen_count"] == 2
    # dry-run 不落库
    assert client.get("/panel/api/tasks", headers=auth).json()["total"] == 0


def test_sync_endpoint_fails_fast_without_coordinator(client, auth):
    r = client.post("/v1/images/generations", json={
        "prompt": "猫", "model": "seedream-5.5"}, headers=auth)
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["code"] == "executor_unavailable"
    # 部署细节（环境变量名、重启动作）属于运维手册，不进客户端响应
    assert "COORDINATOR" not in body["error"]["message"]


def test_create_task_then_manual_isolation(client, auth):
    r = client.post("/async/v1/images/generations", json={
        "prompt": "猫", "model": "seedream-5.5", "n": 1}, headers=auth)
    assert r.status_code == 503  # 没协调器时异步也拒绝

    task = client.post("/panel/api/tasks", json={
        "prompt": "猫", "model": "seedream-5.5", "dry_run": False, "confirm": True,
    }, headers=auth)
    # dry_run=False + confirm=True 会真的建任务；但面板没有独立开关，
    # 这里用 pool 为空来确认它进了队列而不是被丢弃
    assert task.status_code == 200
    tid = task.json()["task_id"]
    detail = client.get(f"/panel/api/tasks/{tid}", headers=auth).json()
    assert detail["status"] == "queued"


def test_keys_lifecycle(client, auth):
    r = client.post("/panel/api/keys", json={
        "name": "单元测试", "quota_credits": 10}, headers=auth)
    assert r.status_code == 200
    raw = r.json()["key"]
    assert raw.startswith("sk-")
    items = client.get("/panel/api/keys", headers=auth).json()["items"]
    assert any(k["name"] == "单元测试" for k in items)
    kid = next(k["id"] for k in items if k["name"] == "单元测试")
    assert client.patch(f"/panel/api/keys/{kid}", json={"enabled": False},
                        headers=auth).status_code == 200
    assert client.delete(f"/panel/api/keys/{kid}", headers=auth).status_code == 200


def test_settings_roundtrip(client, auth):
    r = client.put("/panel/api/settings", json={"concurrency": 6}, headers=auth)
    assert r.status_code == 200
    assert r.json()["runtime"]["concurrency"] == 6
    assert client.get("/panel/api/settings", headers=auth).json()["runtime"]["concurrency"] == 6
    assert client.put("/panel/api/settings", json={"default_model": "nope"},
                      headers=auth).status_code == 400


def test_models_cache_roundtrip(client, auth):
    """能力表缓存写进去再读出来必须无损 —— 这里踩过 `new` vs `is_new` 的坑。"""
    import json
    from app import db, models as models_mod, pool as pool_mod
    snap = models_mod.from_snapshot()
    db.put_setting("models_cache", {
        "captured_at": 1700000000, "source": "单测",
        "models": [m.to_dict() for m in snap.models],
    })
    reg = pool_mod.registry()
    assert len(reg.models) == len(snap.models)
    first = reg.models[0]
    assert first.key and first.resolutions and isinstance(first.is_new, bool)
    api = client.get("/panel/api/models", headers=auth).json()
    assert len(api["models"]) == len(snap.models)
    assert json.dumps(api)  # 可序列化


def test_parse_session_input_accepts_full_cookie():
    from app.panel_api import parse_session_input
    sid = "a" * 48
    cookie = (f"ttwid=xx; sessionid={sid}; "
              f"sid_guard={sid}|1789021926|31536000|Fri, 10-Sep-2027 06:32:06 GMT; "
              "msToken=zz")
    assert parse_session_input(cookie) == (sid, 1789021926 + 31536000)
    assert parse_session_input("  bare-sid-value  ") == ("bare-sid-value", None)
    assert parse_session_input("") == ("", None)


def test_batch_import_dedupes_and_reports(client, auth):
    sid1, sid2 = "b" * 48, "c" * 48
    text = "\n".join([
        "# 注释行会被忽略",
        sid1,
        f"sessionid={sid2}; sid_guard={sid2}|1789021926|31536000|Fri, 10-Sep-2027 06:32:06 GMT",
        sid1,              # 批内重复
        "short",           # 不合法
    ])
    r = client.post("/panel/api/accounts/batch", json={
        "text": text, "remark": "批量测试", "probe": False,
    }, headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["created"] == 2
    reasons = [x["reason"] for x in d["skipped"]]
    assert any("批内重复" in x for x in reasons)
    assert any("长度不合法" in x for x in reasons)
    # 第二条带 sid_guard，到期日应被解析
    from app import repo
    rows = [a for a in repo.list_accounts() if a["remark"] == "批量测试"]
    assert len(rows) == 2
    assert any(a["expires_at"] == 1789021926 + 31536000 for a in rows)

    # 再导一次：全部命中"已在池中"
    r2 = client.post("/panel/api/accounts/batch", json={
        "text": sid1, "probe": False}, headers=auth)
    assert r2.json()["created"] == 0
    assert "已在池中" in r2.json()["skipped"][0]["reason"]


def test_login_provider_is_recorded(client):
    """provider 只做参数校验；不真的启动浏览器。"""
    from app import login_assistant
    s = login_assistant.LoginSession(id="x", provider="feishu")
    assert s.snapshot()["provider"] == "feishu"


def test_remote_ticket_is_single_use():
    from app import remote_browser as rb
    t = rb.issue_ticket()
    assert rb.verify_ticket(t) is True
    assert rb.verify_ticket(t) is False          # 用过即作废
    assert rb.verify_ticket("") is False
    assert rb.verify_ticket("forged") is False


# ---------------------------------------------------------------- 异步协议形状
# 依据 SkillHub `image-adapter` §3（2026-11-19 核对 apiduo origin/main @ b0694fcf）。
# 这组用例存在的唯一理由：创建侧的形状曾经没有任何断言，于是文档与实现
# 各说各话很久都没人发现。

def _async_task(status: str, **kw) -> str:
    from app import db
    tid = f"task_{status}_{uuid.uuid4().hex[:8]}"
    now = db.now()
    db.execute(
        "INSERT INTO tasks(id, api_key_id, mode, model, prompt, status, images,"
        " error, error_kind, progress, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, None, "t2i", "high_aes_general_v55", "猫", status,
         kw.get("images", "[]"), kw.get("error", ""), kw.get("error_kind", ""),
         kw.get("progress", ""), now, now))
    return tid


def test_async_create_answers_202_and_only_task_id(client, auth, monkeypatch):
    from app import api
    monkeypatch.setattr(api, "executor_ready", lambda: True)
    monkeypatch.setattr(api.service, "create_task", lambda **kw: {
        "id": "task_new", "status": "queued", "created_at": 1700000000,
        "degradations": ["size 被吸附到 2048x2048"],
    })
    r = client.post("/async/v1/images/generations", json={
        "prompt": "猫", "model": "seedream-5.5"}, headers=auth)
    assert r.status_code == 202
    # 只有 task_id：多一个字段，客户端就会依赖它，网关再也改不动内部命名
    assert r.json() == {"task_id": "task_new"}


def test_async_poll_is_202_while_running_and_200_when_terminal(client, auth):
    for status, code, word in [("queued", 202, "queued"),
                               ("submitting", 202, "queued"),
                               ("running", 202, "in_progress")]:
        tid = _async_task(status, progress="1/2")
        r = client.get(f"/async/v1/images/generations/{tid}", headers=auth)
        assert r.status_code == code, (status, r.text)
        body = r.json()
        assert body["status"] == word
        assert body["id"] == tid and body["task_id"] == tid   # id 只在轮询里有


def test_async_poll_success_carries_status_and_data(client, auth):
    import json
    tid = _async_task("succeeded", images=json.dumps(
        [{"url": "https://cdn/x.png", "width": 2048, "height": 2048, "format": "png"}]))
    r = client.get(f"/async/v1/images/generations/{tid}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["data"][0]["url"] == "https://cdn/x.png"


def test_async_poll_failure_is_200_with_failure_status(client, auth):
    tid = _async_task("failed", error="上游 500 ret=1015 session expired",
                      error_kind="upstream")
    r = client.get(f"/async/v1/images/generations/{tid}", headers=auth)
    # 失败也用 200：4xx/5xx 会让客户端把"已失败的任务"当"查询失败"而永久重试
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failure"
    assert body["error"]["code"] == "task_failed"
    # 原生错误（ret / 上游 errmsg）只进 tasks.error 与 upstream_logs
    assert body["error"]["message"] == "生成失败，请稍后重试"


def test_async_success_without_artifact_is_a_failure(client, auth):
    for images in ("[]", '[{}]'):        # 空数组 与 空对象都不算产物
        tid = _async_task("succeeded", images=images)
        body = client.get(f"/async/v1/images/generations/{tid}", headers=auth).json()
        assert body["status"] == "failure", images
        assert body["error"]["code"] == "empty_result", images


def test_async_poll_hides_other_keys_tasks(client, auth):
    r = client.get("/async/v1/images/generations/task_not_mine", headers=auth)
    assert r.status_code == 404


# ------------------------------------------------- 异步载体：只许 http(s) URL
# 依据 image-adapter §3.6 / 不变量 13。这组用例保护的是一条**结构约束**：
# 任务行与终态 data[] 里都不许出现图片字节，不管上游多配合。

def test_deliverable_url_accepts_only_http_schemes():
    from app.api import deliverable_url

    assert deliverable_url({"url": "https://cdn/a.png"}) == "https://cdn/a.png"
    assert deliverable_url({"url": "  HTTP://cdn/a.png "}) == "HTTP://cdn/a.png"
    assert deliverable_url({"url": "data:image/png;base64,iVBORw0KGgo="}) == ""
    assert deliverable_url({"url": "//cdn/a.png"}) == ""     # 协议相对不是绝对地址
    assert deliverable_url({"url": "ftp://cdn/a.png"}) == ""
    assert deliverable_url({"url": "https://"}) == ""        # 无 host
    assert deliverable_url({"url": ""}) == ""
    assert deliverable_url({"b64_json": "iVBORw0KGgo="}) == ""
    assert deliverable_url(None) == ""


def test_async_b64_only_result_is_carrier_unavailable(client, auth):
    import json
    from app.api import ASYNC_ERROR_CODES

    tid = _async_task("succeeded",
                      images=json.dumps([{"b64_json": "iVBORw0KGgo="}]))
    body = client.get(f"/async/v1/images/generations/{tid}", headers=auth).json()
    assert body["status"] == "failure"
    # 与 task_failed 分开：重试还是同一份内联字节，客户端该做的是改渠道
    assert body["error"]["code"] == "carrier_unavailable"
    assert body["error"]["code"] in ASYNC_ERROR_CODES
    assert "data" not in body


def test_async_data_uri_does_not_masquerade_as_url(client, auth):
    import json

    tid = _async_task("succeeded",
                      images=json.dumps([{"url": "data:image/png;base64,iVBOR"}]))
    body = client.get(f"/async/v1/images/generations/{tid}", headers=auth).json()
    assert body["status"] == "failure"
    assert body["error"]["code"] == "carrier_unavailable"


def test_async_success_never_echoes_inline_bytes(client, auth):
    """有可交付 URL 时 b64 被丢弃，而不是顺手透传 —— 不存在第二个分支。"""
    import json

    tid = _async_task("succeeded", images=json.dumps(
        [{"url": "https://cdn/a.png", "b64_json": "AAAA",
          "width": 2048, "height": 2048, "format": "png"}]))
    body = client.get(f"/async/v1/images/generations/{tid}", headers=auth).json()
    assert body["status"] == "succeeded"
    assert body["data"] == [{"url": "https://cdn/a.png", "width": 2048,
                             "height": 2048, "format": "png"}]


# ------------------------------------------------ 请求体：收下但如实登记
# response_format / mask 在即梦没有对应能力。收下（不 400）是为了不因一个
# 无法兑现的偏好否掉可满足的生成，但口径必须写在文档里，不能靠响应体暗示。

def test_async_create_tolerates_unsupported_preferences(client, auth, monkeypatch):
    from app import api

    seen: dict = {}
    monkeypatch.setattr(api, "executor_ready", lambda: True)

    def fake_create(**kw):
        seen.update(kw)
        return {"id": "task_pref", "status": "queued", "created_at": 1}

    monkeypatch.setattr(api.service, "create_task", fake_create)
    r = client.post("/async/v1/images/generations", json={
        "prompt": "猫", "model": "seedream-5.5",
        "response_format": "b64_json", "mask": "https://x/m.png", "n": 0,
    }, headers=auth)
    assert r.status_code == 202
    assert r.json() == {"task_id": "task_pref"}
    assert seen["count"] == 1          # n=0 回落 1，而不是 422


# ---------------------------------------------- 状态词表与瞬态响应头
def test_sync_timeout_branch_speaks_client_vocabulary(client, auth, monkeypatch):
    """同步降级成异步后，客户端改打轮询；两处必须是同一套词。"""
    from app import api

    monkeypatch.setattr(api, "executor_ready", lambda: True)
    monkeypatch.setattr(api.service, "create_task", lambda **kw: {
        "id": "task_slow", "status": "queued", "created_at": 1700000000,
        "degradations": [],
    })
    monkeypatch.setattr(api.service, "wait_task", lambda tid, budget: {
        "id": tid, "status": "running", "images": [], "created_at": 1700000000,
    })
    r = client.post("/v1/images/generations", json={"prompt": "猫"},
                    headers=auth)
    assert r.status_code == 202
    assert r.json()["status"] == "in_progress"      # 不是内部词 running
    assert r.headers["cache-control"] == "no-store"


def test_async_poll_disables_caching_and_hints_retry(client, auth):
    import json

    running = _async_task("running")
    r = client.get(f"/async/v1/images/generations/{running}", headers=auth)
    assert r.status_code == 202
    # 缓存住一次 202，客户端就永远拿到 queued
    assert r.headers["cache-control"] == "no-store"
    assert int(r.headers["retry-after"]) >= 1

    done = _async_task("succeeded",
                       images=json.dumps([{"url": "https://cdn/x.png"}]))
    r2 = client.get(f"/async/v1/images/generations/{done}", headers=auth)
    assert r2.status_code == 200
    assert r2.headers["cache-control"] == "no-store"
    assert "retry-after" not in r2.headers


def test_executor_heartbeat_guards_stale_coordinator(monkeypatch):
    """协调器 task 还活着但 tick 一直失败时，不该继续受理。"""
    import time
    from dataclasses import replace

    from app import api, config
    from app.coordinator import coordinator

    monkeypatch.setattr(api, "settings",
                        replace(config.settings, coordinator_enabled=True))
    monkeypatch.setattr(type(coordinator), "running", property(lambda self: True))

    monkeypatch.setattr(coordinator, "last_tick_at", time.time() - 5)
    assert api.executor_ready() is True

    monkeypatch.setattr(coordinator, "last_tick_at",
                        time.time() - api.EXECUTOR_STALE_SECONDS - 1)
    assert api.executor_ready() is False

    monkeypatch.setattr(coordinator, "last_tick_at", 0)
    assert api.executor_ready() is True      # 刚启动、第一轮还没跑完

    # tick 失败也刷新心跳（否则一次偶发异常就被当假死），所以"每轮都失败"
    # 只能靠连续计数拦住
    monkeypatch.setattr(coordinator, "last_tick_at", time.time())
    monkeypatch.setattr(coordinator, "fail_streak",
                        api.EXECUTOR_MAX_FAIL_STREAK)
    assert api.executor_ready() is False


# ------------------------------------------------- 信息分层：客户端 / 运维面
# 原则：标准错误对客户端可见；原生错误（ret / 上游 errmsg）、degradations、
# 内部积分口径只留给服务端（tasks 行 + upstream_logs + /panel/api）。

def test_upstream_auth_error_is_502_and_hides_native_detail(client, auth, monkeypatch):
    from app import api
    from app.upstream.errors import JimengAuthError

    monkeypatch.setattr(api, "executor_ready", lambda: True)

    def boom(**kw):
        raise JimengAuthError("登录态失效（ret=1015, invalid session）", ret=1015)

    monkeypatch.setattr(api.service, "create_task", boom)
    r = client.post("/async/v1/images/generations", json={"prompt": "猫"},
                    headers=auth)
    # 上游凭证失效是 502，不是 401：客户端 Key 没错，不该被通知去改请求
    assert r.status_code == 502
    err = r.json()["error"]
    assert set(err) == {"code", "message"}        # 没有 kind / ret
    assert err["code"] == "upstream_error"
    assert "1015" not in err["message"] and "ret" not in err["message"]


def test_local_param_error_keeps_actionable_message(client, auth, monkeypatch):
    """本地校验文案可以给客户端 —— 它描述的是客户端自己的输入。"""
    from app import api
    from app.upstream.errors import JimengParamError

    monkeypatch.setattr(api, "executor_ready", lambda: True)

    def boom(**kw):
        raise JimengParamError("size 需形如 2048x2048", kind="param")

    monkeypatch.setattr(api.service, "create_task", boom)
    r = client.post("/async/v1/images/generations", json={"prompt": "猫"},
                    headers=auth)
    assert r.status_code == 400
    assert r.json()["error"] == {"code": "invalid_request",
                                 "message": "size 需形如 2048x2048"}


def test_validation_error_never_echoes_input(client, auth):
    """pydantic 默认会把 input 抄回 422；传垫图时那是几 MB 的 base64。"""
    huge = "A" * 40000
    r = client.post("/async/v1/images/generations",
                    json={"prompt": "猫", "n": huge}, headers=auth)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "invalid_request"
    assert "n" in body["error"]["message"]
    assert huge[:200] not in r.text
    assert len(r.text) < 500


def test_sync_success_has_no_internal_fields(client, auth, monkeypatch):
    from app import api

    monkeypatch.setattr(api, "executor_ready", lambda: True)
    monkeypatch.setattr(api.service, "create_task", lambda **kw: {
        "id": "task_ok", "status": "queued", "created_at": 1700000000,
    })
    monkeypatch.setattr(api.service, "wait_task", lambda tid, budget: {
        "id": tid, "status": "succeeded", "created_at": 1700000000,
        "finished_at": 1700000001, "model": "high_aes_general_v55",
        "images": [{"url": "https://cdn/a.png", "width": 2048,
                    "height": 2048, "format": "png"}],
        "error": "内部：ret=1015", "error_kind": "auth",
    })
    r = client.post("/v1/images/generations", json={"prompt": "猫"},
                    headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "degradations" not in body and "credits" not in body
    assert body["data"][0]["url"] == "https://cdn/a.png"
    assert body["model"] == "seedream-5.5"       # 回显对外 ID，不是上游 key
    assert "high_aes" not in r.text


def test_business_key_gets_protocol_view_only(client, auth):
    """error_kind / credits 是运维字段；业务 Key 只该看到协议面。"""
    from app import db, repo

    raw = repo.create_key("业务视图")
    kid = repo.key_by_raw(raw)["id"]
    tid = _async_task("failed", error="上游 500 ret=1015", error_kind="upstream")
    db.execute("UPDATE tasks SET api_key_id = ? WHERE id = ?", (kid, tid))

    biz = {"Authorization": f"Bearer {raw}"}
    r = client.get(f"/v1/tasks/{tid}", headers=biz)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failure"
    assert "error_kind" not in body and "credits" not in body
    assert "ret=1015" not in r.text

    admin = client.get(f"/v1/tasks/{tid}", headers=auth).json()
    assert admin["error_kind"] == "upstream"      # 运维面保留原生归因
    assert admin["error"] == "上游 500 ret=1015"


def test_credits_hides_pool_from_business_key(client, auth):
    from app import repo

    raw = repo.create_key("业务额度", quota=10)
    biz = {"Authorization": f"Bearer {raw}"}
    body = client.get("/v1/credits", headers=biz).json()
    assert body["quota"] == 10 and body["remaining"] == 10
    assert body["unlimited"] is False
    assert "pool" not in body and "accounts" not in body

    admin = client.get("/v1/credits", headers=auth).json()
    assert "pool" in admin and "accounts" in admin["pool"]


def test_degradations_persist_for_ops_but_not_clients(client, auth):
    """吸附过程要可维护，但不该出现在任何客户端响应里。"""
    from app import repo, service

    # n=9 超出 v55 的 1..8 档 ⇒ 吸附为 8，并记一条 degradation
    task = service.create_task(api_key_id=None, model="seedream-5.5",
                               prompt="猫", size="2048x2048", count=9)
    stored = repo.get_task(task["id"])
    assert stored["count"] == 8
    assert any("张数" in d for d in stored["degradations"])

    panel = client.get(f"/panel/api/tasks/{task['id']}", headers=auth).json()
    assert panel["degradations"] == stored["degradations"]


# ----------------------------------------------------- 能力清单：对外 ID 收敛
# 上游 model_req_key（high_aes_* / jm_image_model_*）与积分单价都是渠道内部
# 信息：客户端只需要"能选什么参数"，不需要知道它对应哪家的哪个接入点。

def test_models_never_expose_upstream_keys_or_prices(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    text = r.text
    assert "high_aes_" not in text
    assert "jm_image_model" not in text
    assert '"prices"' not in text           # 上游单价 = 成本结构
    assert '"live"' not in text and '"source"' not in text

    body = r.json()
    assert body["data"] and all(m["id"] for m in body["data"])
    assert sum(1 for m in body["data"] if m["default"]) == 1
    # 后编辑清单只留可路由字段
    for tool in body["tools"]:
        # needs_mask / ratios / scales 都是**可路由**信息：不列出来调用方只能猜
        assert set(tool) == {"id", "name", "kind", "input", "resolutions",
                             "needs_mask", "ratios", "scales"}


def test_public_model_id_is_stable_and_idempotent():
    from app.service import public_model_id

    assert public_model_id("high_aes_general_v55") == "seedream-5.5"
    assert public_model_id("seedream-5.5") == "seedream-5.5"   # 幂等
    assert public_model_id("jimeng-pro-hd") == "jimeng-pro-hd"  # 后编辑原名
    assert public_model_id("") == ""
    # 未登记的新模型走稳定短码，不漏上游 key
    stray = public_model_id("vendor_secret_model_v9")
    assert stray.startswith("m-") and "vendor" not in stray
    assert stray == public_model_id("vendor_secret_model_v9")


def test_unsupported_capability_hides_internal_reason(monkeypatch):
    """拒绝理由里的 entity_seg / unified_generate_v2 只进日志。"""
    import pytest

    from app import service
    from app.upstream import postedit as _pe

    # 能力表已全部接入，用**注入**的未支持条目测机制。
    # 必须用 monkeypatch：直接改全局字典会让后续测试看到脏数据（踩过一次）。
    monkeypatch.setitem(_pe.UNSUPPORTED, "jimeng-probe-unsup",
                        "走 entity_seg / save_local_item_by_cut，形态未取证")
    monkeypatch.setitem(_pe.ALIASES, "probe-unsup", "jimeng-probe-unsup")
    with pytest.raises(Exception) as ei:
        service.create_task(api_key_id=None, model="probe-unsup",
                            prompt="", size="2048x2048", count=1)
    msg = str(ei.value)
    assert "不支持" in msg
    for internal in ("entity_seg", "save_local_item_by_cut", "unified_generate_v2",
                     "未取证"):
        assert internal not in msg


def test_panel_tools_expose_internals_but_client_does_not():
    """面板可见 ≠ 客户端可见。

    面板靠 these 字段做运维（形态、取证记录、gen_type），客户端只需要能路由的。
    这条测试钉住两个方向的边界：**删掉面板字段**（运维瞎）和**漏进客户端**（泄内部）
    都会红。
    """
    from app.upstream import postedit

    panel = {t["id"]: t for t in postedit.panel_models()}
    assert set(panel) == set(postedit.ABILITIES)
    for t in panel.values():
        for k in ("shape", "ability", "ability_code", "gen_type", "notes"):
            assert k in t, (t["id"], k)

    client = {t["id"]: t for t in postedit.public_models()}
    for t in client.values():
        for k in ("shape", "ability", "ability_code", "gen_type", "notes", "outpaint"):
            assert k not in t, (t["id"], k)
