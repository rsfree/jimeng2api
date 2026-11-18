import json
import uuid

from tests.conftest import ADMIN


def test_health_and_models_are_public(client):
    assert client.get("/healthz").json()["status"] == "ok"
    models = client.get("/v1/models").json()
    assert models["object"] == "list"
    assert any(m["id"] == "high_aes_general_v55" for m in models["data"])
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
    assert "执行器" in r.json()["detail"]


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
    tid = _async_task("failed", error="上游 500", error_kind="upstream")
    r = client.get(f"/async/v1/images/generations/{tid}", headers=auth)
    # 失败也用 200：4xx/5xx 会让客户端把"已失败的任务"当"查询失败"而永久重试
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failure"
    assert body["error"]["code"] == "task_failed"
    assert "上游 500" in body["error"]["message"]


def test_async_success_without_artifact_is_a_failure(client, auth):
    for images in ("[]", '[{}]'):        # 空数组 与 空对象都不算产物
        tid = _async_task("succeeded", images=images)
        body = client.get(f"/async/v1/images/generations/{tid}", headers=auth).json()
        assert body["status"] == "failure", images
        assert body["error"]["code"] == "empty_result", images


def test_async_poll_hides_other_keys_tasks(client, auth):
    r = client.get("/async/v1/images/generations/task_not_mine", headers=auth)
    assert r.status_code == 404
