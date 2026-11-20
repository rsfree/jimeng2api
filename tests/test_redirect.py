"""模型重定向：对外名（官网命名）→ 本网关模型。

这几条钉住四件事：
1. 解析：大小写不敏感、支持链式、**成环必须拒绝**；
2. 边界：不能遮蔽内置模型名（否则老客户端的行为会被静默改掉）；
3. 对外：别名要出现在 `/v1/models`（不然客户端只能猜拼写），
   但上游 key / 单价这些内部信息不能跟着漏出去；
4. 回显：客户端传别名，响应就回别名；任务行里仍存上游 key。
"""
import pytest

from app import db, service
from app.api import GenerateRequest, _payload, ok_response
from app.upstream.errors import JimengParamError


def add(client, auth, alias, target, **extra):
    return client.post("/panel/api/model-redirects",
                       json={"alias": alias, "target": target, **extra},
                       headers=auth)


def test_alias_resolves_and_is_published(client, auth):
    r = add(client, auth, "GPT-Image-1", "seedream-5.5")
    assert r.status_code == 200, r.text
    assert r.json()["item"]["alias"] == "gpt-image-1"     # 归一化成小写
    assert r.json()["item"]["public"] == "seedream-5.5"

    # 大小写不敏感：官方 SDK 常带大小写
    assert service.resolve_model_name("gpt-image-1") == "high_aes_general_v55"
    assert service.resolve_model_name("GPT-IMAGE-1") == "high_aes_general_v55"

    body = client.get("/v1/models").json()
    hits = [m for m in body["data"] if m["id"] == "gpt-image-1"]
    assert len(hits) == 1
    assert hits[0]["alias_of"] == "seedream-5.5"
    assert hits[0]["default"] is False
    # 别名不能改变"只有一个默认模型"这条约束
    assert sum(1 for m in body["data"] if m["default"]) == 1
    # 公布别名不等于公布上游 key
    assert "high_aes_" not in client.get("/v1/models").text


def test_alias_can_target_postedit_tool(client, auth):
    r = add(client, auth, "upscale-hd", "jimeng-hd")
    assert r.status_code == 200, r.text
    assert service.resolve_model_name("upscale-hd") == "jimeng-hd"

    body = client.get("/v1/models").json()
    tools = {t["id"]: t for t in body["tools"]}
    assert "upscale-hd" in tools
    assert tools["upscale-hd"]["kind"] == "postedit"
    # 工具条目保持极窄字段集：别名不引入新字段（来源属于面板要解释的事）
    assert set(tools["upscale-hd"]) == {
        "id", "name", "kind", "input", "resolutions", "needs_mask",
        "ratios", "scales"}


def test_direct_create_task_resolves_alias_before_branching(client, auth):
    """`create_task` 的直接调用方（脚本）传别名也要走对分支。

    指向后编辑的别名若先被当成生成模型，报的是"未知模型"——一个会把人
    带去查能力表的假线索。
    """
    assert add(client, auth, "my-upscale", "jimeng-hd").status_code == 200
    with pytest.raises(JimengParamError) as ei:
        service.create_task(api_key_id=None, model="my-upscale", prompt="",
                            size="", count=1)
    assert "一张图" in str(ei.value)


def test_builtin_virtual_names_keep_old_echo():
    """`default` / 上游 key 的回显契约不因别名功能而变。"""
    assert service.echo_model_id("default", "high_aes_general_v55") \
        == "seedream-5.5"
    assert service.echo_model_id("high_aes_general_v55", "high_aes_general_v55") \
        == "seedream-5.5"


def test_alias_to_sync_segment_is_routed_before_queue(client, auth):
    """别名指向同步能力（抠图）时，入口必须认出形态。

    认不出的后果不是报错，而是把抠图请求排进任务队列 —— 一个永远查不到
    状态的壳。这里只验分流用的名字，不触发任何上游请求。
    """
    from app.upstream import postedit

    assert add(client, auth, "cutout-free", "jimeng-cutout").status_code == 200
    ability = postedit.get(service.resolve_redirect("cutout-free"))
    assert ability is not None and ability.shape == "segment"


@pytest.mark.parametrize("alias", [
    "seedream-5.5", "high_aes_general_v55", "jimeng-hd", "default", "jimeng",
])
def test_builtin_names_cannot_be_shadowed(client, auth, alias):
    r = add(client, auth, alias, "seedream-5.0")
    assert r.status_code == 400
    assert "内置" in r.json()["error"]["message"]


def test_alias_charset_is_restricted(client, auth):
    assert add(client, auth, "带空格 的名字", "seedream-5.5").status_code == 400
    assert add(client, auth, "差", "seedream-5.5").status_code == 400


def test_unknown_target_is_rejected(client, auth):
    r = add(client, auth, "gpt-5", "not-a-model")
    assert r.status_code == 400
    assert "能力表" in r.json()["error"]["message"]


def test_chain_resolves_and_cycle_is_rejected(client, auth):
    assert add(client, auth, "b-alias", "seedream-5.5").status_code == 200
    assert add(client, auth, "a-alias", "b-alias").status_code == 200
    assert service.resolve_model_name("a-alias") == "high_aes_general_v55"

    rows = client.get("/panel/api/model-redirects", headers=auth).json()["items"]
    bid = next(r["id"] for r in rows if r["alias"] == "b-alias")
    r = client.patch(f"/panel/api/model-redirects/{bid}",
                     json={"target": "a-alias"}, headers=auth)
    assert r.status_code == 400
    assert "成环" in r.json()["error"]["message"]
    # 拒绝之后原映射必须还在：失败的写入不能把表改坏
    assert service.resolve_model_name("b-alias") == "high_aes_general_v55"


def test_alias_with_slash_is_managed_by_id(client, auth):
    """模型名里允许 `/`（`google/imagen-4.0`），所以路由主键必须用 id。"""
    r = add(client, auth, "google/imagen-4.0", "seedream-4.0")
    assert r.status_code == 200, r.text
    rid = r.json()["item"]["id"]
    assert client.patch(f"/panel/api/model-redirects/{rid}",
                        json={"note": "别名"}, headers=auth).status_code == 200
    assert client.delete(f"/panel/api/model-redirects/{rid}",
                         headers=auth).status_code == 200


def test_disable_and_delete_take_effect(client, auth):
    r = add(client, auth, "gpt-image-1", "seedream-5.5")
    rid = r.json()["item"]["id"]

    assert client.patch(f"/panel/api/model-redirects/{rid}",
                        json={"enabled": False}, headers=auth).status_code == 200
    with pytest.raises(JimengParamError):
        service.resolve_model_name("gpt-image-1")
    assert not any(m["id"] == "gpt-image-1"
                   for m in client.get("/v1/models").json()["data"])

    assert client.delete(f"/panel/api/model-redirects/{rid}",
                         headers=auth).status_code == 200
    assert client.get("/panel/api/model-redirects", headers=auth).json()["items"] == []


def test_missing_redirect_returns_404(client, auth):
    assert client.patch("/panel/api/model-redirects/999",
                        json={"enabled": False}, headers=auth).status_code == 404
    assert client.delete("/panel/api/model-redirects/999",
                         headers=auth).status_code == 404


def test_bulk_import_reports_each_line(client, auth):
    r = client.post("/panel/api/model-redirects/import", json={
        "text": "# 注释\n"
                "gpt-image-1 => seedream-5.5\n"
                "doubao-seedream-4-0-250828 -> seedream-4.0\n"
                "没有分隔符\n",
    }, headers=auth)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["created"] == 2
    assert [x["ok"] for x in d["items"]] == [True, True, False]
    assert service.resolve_model_name("doubao-seedream-4-0-250828") \
        == "high_aes_general_v40"

    # 已存在：默认跳过；勾选覆盖才更新
    r2 = client.post("/panel/api/model-redirects/import", json={
        "text": "gpt-image-1 => seedream-4.6"}, headers=auth)
    assert r2.json()["items"][0]["ok"] is False
    assert service.resolve_model_name("gpt-image-1") == "high_aes_general_v55"
    r3 = client.post("/panel/api/model-redirects/import", json={
        "text": "gpt-image-1 => seedream-4.6", "overwrite": True}, headers=auth)
    assert r3.json()["updated"] == 1
    assert service.resolve_model_name("gpt-image-1") == "high_aes_general_v42"


def test_response_echoes_requested_alias(client, auth):
    """回显别名，任务行存上游 key —— 两者不能混。"""
    assert add(client, auth, "gpt-image-1", "seedream-5.5").status_code == 200
    payload = _payload(GenerateRequest(
        model="gpt-image-1", prompt="一只猫", size="2048x2048"))
    assert payload["model"] == "high_aes_general_v55"
    assert payload["extras"]["requested_model"] == "gpt-image-1"

    task = service.create_task(api_key_id=None, **payload)
    assert task["model"] == "high_aes_general_v55"
    assert ok_response(task)["model"] == "gpt-image-1"


def test_failed_redirect_is_visible_in_panel_not_500(client, auth):
    """能力表变了导致目标消失：页面标红 + 概览告警，但接口不能 500。"""
    assert add(client, auth, "gpt-image-1", "seedream-5.5").status_code == 200
    # 绕过校验直接改库，模拟"配置还在、能力表已经变了"
    db.execute("UPDATE model_redirects SET target = 'vendor_gone'"
               " WHERE alias = 'gpt-image-1'")

    items = client.get("/panel/api/model-redirects", headers=auth).json()["items"]
    assert items[0]["ok"] is False
    assert "能力表" in items[0]["error"]

    overview = client.get("/panel/api/overview", headers=auth).json()
    assert any("模型重定向" in a["text"] for a in overview["alerts"])

    with pytest.raises(JimengParamError) as ei:
        service.resolve_model_name("gpt-image-1")
    msg = str(ei.value)
    assert "gpt-image-1" in msg and "重定向" in msg
    # 目标可能被配成上游 key：错误文案里不能出现它（硬口径）
    assert "vendor_gone" not in msg
