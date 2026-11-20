"""后编辑（智能超清 / 超清 / 细节修复 / 扩图）的形态门禁。

这些用例**不发任何上游请求**：断言落在草稿字段上，而且有多条直接拿
2026-11-19 的真实抓包（`docs/evidence/`）逐键对拍 —— 「跑通了」排除零个假设，
`origin_image` 是对象还是字符串才有信息量。unified 族（局部重绘 / 消除笔）
还额外断言**键的存在性**：`prompt` / `option` / `benefitCount` 在消除笔里是缺失，
不是空值，改成空值就与抓包不符。
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest

from app import service
from app.upstream import postedit
from app.upstream.errors import JimengParamError

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "docs/evidence"
EVIDENCE = EVIDENCE_DIR / "2026-11-19-pro-hd-generate.json"
EVIDENCE_INPAINT = EVIDENCE_DIR / "2026-11-19-inpaint-generate.json"
EVIDENCE_ERASE = EVIDENCE_DIR / "2026-11-19-erase-generate.json"
EVIDENCE_OUTPAINT = EVIDENCE_DIR / "2026-11-19-outpaint-generate.json"
EVIDENCE_EDIT = EVIDENCE_DIR / "2026-11-19-smart-edit-generate.json"


# ------------------------------------------------------------------ 抓包对拍
def test_prohd_draft_matches_captured_request():
    """智能超清的草稿必须与真实抓包同构（2026-11-19，提交成功、实扣 19 积分）。"""
    captured = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    real = json.loads(captured["draft_content"])
    ability = postedit.get("jimeng-pro-hd")
    ours = json.loads(postedit.build_postedit_draft(
        ability=ability,
        source=postedit.build_source(image_uri="tos-cn-i-x", width=1024, height=1024),
        resolution="2k",
    ))

    rc, oc = real["component_list"][0], ours["component_list"][0]
    assert ours["version"] == real["version"] == "3.3.40"
    assert ours["min_features"] == real["min_features"]
    assert oc["generate_type"] == rc["generate_type"] == "pro_hd"
    # 组件级没有数字 gen_type（legacy 才有）
    assert "gen_type" not in oc

    rp, op = rc["abilities"]["pro_hd"], oc["abilities"]["pro_hd"]
    assert op["core_param"]["model"] == rp["core_param"]["model"] == "image_upscale_paz"
    assert op["core_param"]["generate_type"] == 0
    assert op["resolution_type"] == rp["resolution_type"] == "2k"
    assert op["postedit_param"]["generate_type"] == rp["postedit_param"]["generate_type"] == 35

    # origin_image 必须是对象，且键集与抓包一致
    assert isinstance(op["postedit_param"]["origin_image"], dict)
    assert set(op["postedit_param"]["origin_image"]) == set(
        rp["postedit_param"]["origin_image"])
    assert op["postedit_param"]["origin_image"]["image_uri"] == "tos-cn-i-x"

    # pro_hd 没有 gen_option —— 多了会被上游当成另一种能力
    assert "gen_option" not in oc["abilities"]


def test_prohd_extend_and_metrics_follow_capture():
    captured = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    ability = postedit.get("jimeng-pro-hd")

    assert postedit.submit_extend(ability, "0") == {"workspace_id": 0}
    assert captured["extend"] == {"workspace_id": 24813558116108}

    m = json.loads(postedit.submit_metrics(
        ability, submit_id="sid", resolution="2k", width=1024, height=1024,
        image_uri="tos-cn-i-x"))
    real_m = json.loads(captured["metrics_extra"])
    assert m["toolItemId"] == real_m["toolItemId"]
    assert m["tool_entry"] == real_m["tool_entry"]
    scene = json.loads(m["sceneOptions"])[0]
    real_scene = json.loads(real_m["sceneOptions"])[0]
    assert scene["scene"] == real_scene["scene"] == "ImageProHD"
    assert scene["modelName"] == real_scene["modelName"] == "image_upscale_paz"
    assert scene["postEditSourceSize"] == real_scene["postEditSourceSize"]


# ------------------------------------------------------------------ legacy
def test_hd_legacy_draft_matches_history_shape():
    """`normal_hd` 的旧形态（本号历史 12 条）：有 gen_option、无 core_param。"""
    a = postedit.get("jimeng-hd")
    draft = json.loads(postedit.build_postedit_draft(
        ability=a,
        source=postedit.build_source(item_id=7696399061636910361,
                                     origin_history_id=47490120999948),
    ))
    comp = draft["component_list"][0]
    assert comp["gen_type"] == 13
    assert comp["generate_type"] == "normal_hd"
    node = comp["abilities"]["normal_hd"]
    assert node["postedit_param"]["item_id"] == 7696399061636910361
    assert "core_param" not in node
    assert comp["abilities"]["gen_option"]["gen_count"] == 1
    assert draft["version"] == "3.2.9"


def test_build_source_rejects_empty_input():
    with pytest.raises(ValueError):
        postedit.build_source()


def test_ability_aliases_and_unsupported():
    assert postedit.normalize_model("变清晰") == "jimeng-hd"
    assert postedit.normalize_model("UPSCALE") == "jimeng-hd"
    assert postedit.normalize_model("gpt-image-1") == ""
    assert postedit.normalize_model("消除笔") == "jimeng-erase"
    assert postedit.normalize_model("智能编辑") == "jimeng-smart-edit"
    # 当前能力表已无"未支持"条目（智能编辑 2026-11-19 接入 v2）
    assert postedit.UNSUPPORTED == {}


def test_no_capability_is_experimental_now():
    """扩图 2026-11-19 抓包后转正，当前**没有**实验能力。

    这条不是"顺手收紧"：`experimental` 的语义是"草稿形态未取证、默认关闭"。
    如果将来有人新增能力却忘了标 experimental，这里会红。
    """
    assert [a.key for a in postedit.ABILITIES.values() if a.experimental] == []


# ------------------------------------------------------------------ 图像尺寸
def test_image_size_reads_png_header():
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
           + struct.pack(">II", 640, 480) + b"\x08\x02\x00\x00\x00")
    assert postedit.image_size(png) == (640, 480)
    assert postedit.image_size(b"not-an-image") == (0, 0)


# ------------------------------------------------------------------ 业务层
def test_postedit_task_needs_exactly_one_image():
    with pytest.raises(JimengParamError):
        service.create_task(api_key_id=None, model="jimeng-pro-hd", prompt="",
                            size="", count=1,
                            images=["http://a/1.png", "http://a/2.png"])


def test_item_only_ability_rejects_upload_ref():
    with pytest.raises(JimengParamError) as e:
        service.create_task(api_key_id=None, model="jimeng-detail-fix", prompt="",
                            size="", count=1, images=["https://a/1.png"])
    assert "item:" in str(e.value)


def test_prohd_rejects_item_ref_and_accepts_uri():
    with pytest.raises(JimengParamError):
        service.create_task(api_key_id=None, model="jimeng-pro-hd", prompt="",
                            size="", count=1, images=["item:1:2"])
    task = service.create_task(
        api_key_id=None, model="jimeng-pro-hd", prompt="", size="",
        count=1, resolution="4k", images=["https://a/1.png"])
    assert task["mode"] == "postedit"
    assert task["resolution"] == "4k"


def test_resolution_is_snapped_with_degradation():
    task = service.create_task(
        api_key_id=None, model="jimeng-pro-hd", prompt="", size="",
        count=1, resolution="16k", images=["https://a/1.png"])
    assert task["resolution"] == "2k"
    assert any("16k" in d for d in task["degradations"])


def test_postedit_task_created_without_prompt():
    task = service.create_task(
        api_key_id=None, model="jimeng-hd", prompt="", size="2048x2048",
        count=1, images=["item:7696399061636910361:47490120999948"])
    assert task["mode"] == "postedit"
    assert task["extras"]["ability"] == "normal_hd"
    assert any("size" in d for d in task["degradations"])


def test_count_is_forced_to_one_with_degradation():
    task = service.create_task(
        api_key_id=None, model="jimeng-hd", prompt="", size="",
        count=4, images=["item:1:2"])
    assert task["count"] == 1
    assert any("张" in d for d in task["degradations"])


def test_experimental_gate_still_works(monkeypatch):
    """门禁本身要能拦住实验能力 —— 用注入的假能力测，不依赖真实能力的状态。"""
    probe = postedit.Ability(
        key="jimeng-probe-exp", ability="probe", gen_type=999,
        name="探针能力", input="uri", experimental=True, shape="legacy")
    monkeypatch.setitem(postedit.ABILITIES, "jimeng-probe-exp", probe)
    with pytest.raises(JimengParamError) as e:
        service.create_task(api_key_id=None, model="jimeng-probe-exp", prompt="",
                            size="", count=1, images=["https://a/1.png"])
    assert "未开放" in str(e.value)


def test_unsupported_ability_reports_reason(monkeypatch):
    """拒绝理由只给"不支持 + 去哪看清单"，不泄露上游协议细节（用注入条目测）。"""
    monkeypatch.setitem(postedit.UNSUPPORTED, "jimeng-probe-unsup",
                        "走 entity_seg / unified_generate_v2 的私有链路")
    monkeypatch.setitem(postedit.ALIASES, "probe-unsup", "jimeng-probe-unsup")
    with pytest.raises(JimengParamError) as e:
        service.create_task(api_key_id=None, model="probe-unsup", prompt="",
                            size="", count=1, images=["https://a/1.png"])
    msg = str(e.value)
    assert "不支持" in msg
    assert "entity_seg" not in msg and "unified_generate_v2" not in msg


def test_mask_is_stored_and_flagged_not_silently_dropped():
    """mask 收下并落库，但明确写进 degradations —— 静默丢弃会让人以为生效了。"""
    task = service.create_task(
        api_key_id=None, model="jimeng-pro-hd", prompt="", size="", count=1,
        images=["https://a/1.png"], mask="https://a/mask.png")
    assert task["mask_uri"] == "https://a/mask.png"
    assert any("mask" in d for d in task["degradations"])


def test_t2i_mask_is_flagged_too():
    task = service.create_task(
        api_key_id=None, model="seedream-5.5", prompt="猫",
        size="2048x2048", count=1, mask="https://a/m.png")
    assert task["mask_uri"] == "https://a/m.png"
    assert any("mask" in d for d in task["degradations"])


def test_hd_accepts_uploaded_image_as_object_node():
    """普通超清的上传图路径：`origin_image` 必须是**对象**。

    2026-11-19 用字符串提交被拒（ret=1000）；2026-11-20 换对象后 ret=0、
    出图 2048×2048 且未扣费。这条测试钉住"对象 vs 字符串"这个差别 ——
    它是两次实测之间唯一的变量。
    """
    import json as _json

    ability = postedit.get("jimeng-hd")
    assert ability.input == "both"

    task = service.create_task(
        api_key_id=None, model="jimeng-hd", prompt="", size="", count=1,
        images=["https://a/1.png"])
    assert task["mode"] == "postedit"

    draft = _json.loads(postedit.build_postedit_draft(
        ability=ability,
        source=postedit.build_source(image_uri="tos-x", width=1024, height=1024)))
    origin = draft["component_list"][0]["abilities"]["normal_hd"][
        "postedit_param"]["origin_image"]
    assert isinstance(origin, dict), "必须是对象；字符串会被上游 ret=1000 拒"
    assert origin["image_uri"] == "tos-x"
    assert origin["width"] == 1024 and origin["height"] == 1024

    # 作品引用那条老路径不能因此坏掉
    by_item = _json.loads(postedit.build_postedit_draft(
        ability=ability,
        source=postedit.build_source(item_id=1, origin_history_id=2)))
    param = by_item["component_list"][0]["abilities"]["normal_hd"]["postedit_param"]
    assert param["item_id"] == 1 and "origin_image" not in param


# ------------------------------------------------------------------ 提交体
def test_client_submit_postedit_body_shape(monkeypatch):
    from app.upstream.client import JimengClient

    c = JimengClient("sid", base="https://j.example", origin="https://j.example",
                     aid="513695", da_version="3.3.40", web_version="7.5.0",
                     appvr="8.4.0", pf="7", app_sdk_version="48.0.0", ua="ua")
    seen: dict = {}
    monkeypatch.setattr(c, "_post",
                        lambda path, body, **kw: seen.update(path=path, body=body) or {"ret": "0"})
    c.submit_postedit(
        ability=postedit.get("jimeng-pro-hd"),
        source=postedit.build_source(image_uri="tos-x", width=1024, height=1024),
        submit_id="sid", resolution="2k", workspace_id="0")
    body = seen["body"]
    assert seen["path"] == "/mweb/v1/aigc_draft/generate"
    assert isinstance(body["draft_content"], str)      # 双重编码，不是对象
    assert body["extend"] == {"workspace_id": 0}
    assert json.loads(body["draft_content"])["component_list"][0][
        "abilities"]["pro_hd"]["resolution_type"] == "2k"


# ------------------------------------------------------------------ 对外清单
def test_models_endpoint_lists_only_submittable_tools(client):
    """清单里出现的能力必须**当前真能提交**：实验能力不入单。

    内部标识（ability / gen_type / shape / notes）不进客户端响应 —— 它们
    是草稿构造与取证记录，泄露出去只会让客户端依赖内部字段。
    """
    body = client.get("/v1/models").json()
    ids = {t["id"] for t in body["tools"]}
    assert {"jimeng-pro-hd", "jimeng-hd", "jimeng-detail-fix",
            "jimeng-inpaint", "jimeng-erase", "jimeng-outpaint",
            "jimeng-cutout"} <= ids
    pro = next(t for t in body["tools"] if t["id"] == "jimeng-pro-hd")
    assert pro["resolutions"] == ["2k", "4k", "8k"]
    assert "ability" not in pro and "gen_type" not in pro


# ------------------------------------------------------------------ 交付形态
def test_postedit_sync_endpoint_returns_202_by_default(client, auth, monkeypatch):
    """后编辑在同步端点上默认不等待：直接 202 + poll_url。

    一次 2K 超清实测 38.5s，占着连接等到超时再降级，会让降级变成常态。
    """
    monkeypatch.setattr("app.api.executor_ready", lambda: True)
    r = client.post("/v1/images/generations", headers=auth, json={
        "model": "jimeng-pro-hd", "image": "https://a/1.png"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["task_id"]
    assert body["poll_url"].startswith("/async/v1/images/generations/")
    assert "默认异步" in body["detail"]
    assert r.headers.get("cache-control") == "no-store"


def test_explicit_wait_reenables_blocking(client, auth, monkeypatch):
    """显式传 wait 才等 —— "优先异步"不是"只能异步"。"""
    monkeypatch.setattr("app.api.executor_ready", lambda: True)
    r = client.post("/v1/images/generations", headers=auth, json={
        "model": "jimeng-pro-hd", "image": "https://a/1.png", "wait": 1})
    assert r.status_code == 202
    assert "等待预算" in r.json()["detail"]


def test_t2i_still_blocks_by_default(client, auth, monkeypatch):
    """文生图不受影响：仍然走等待路径（这里用 1s 预算，不真等 300s）。"""
    monkeypatch.setattr("app.api.executor_ready", lambda: True)
    r = client.post("/v1/images/generations", headers=auth, json={
        "model": "seedream-5.5", "prompt": "猫", "wait": 1})
    assert r.status_code == 202
    assert "等待预算" in r.json()["detail"]


# ------------------------------------------------------------------ 抠图（分割）
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_cutout_is_a_segment_ability():
    a = postedit.get("抠图")
    assert a is not None and a.shape == "segment" and a.input == "uri"
    assert postedit.unsupported_reason("抠图") == ""


def test_cutout_uploads_then_segments(monkeypatch):
    """抠图 = 上传 + saliency_seg，不建任务、不计费。"""
    from app import pool as pool_mod
    seen = {}

    class FakeUploader:
        def upload(self, data):
            seen["bytes"] = len(data)
            return "tos-x"

    class FakeClient:
        def saliency_seg(self, uris, mode="canvas"):
            seen["uris"] = list(uris)
            seen["mode"] = mode
            return [{"mask": {"uri": "tos-mask", "url": "https://cdn/m.png"}}]

    class FakeRT:
        uploader = FakeUploader()
        client = FakeClient()

    monkeypatch.setattr(pool_mod.pool, "runtime", lambda aid: FakeRT())
    monkeypatch.setattr("app.repo.dispatch_candidates", lambda *a: [{"id": 1}])

    res = service.cutout(api_key_id=None,
                         image="data:image/png;base64," + PNG_1PX)
    assert seen["uris"] == ["tos-x"] and seen["mode"] == "canvas"
    assert res["images"][0]["url"] == "https://cdn/m.png"
    assert res["credits"] == 0


def test_cutout_api_is_synchronous(client, auth, monkeypatch):
    monkeypatch.setattr("app.service.cutout", lambda **kw: {
        "model": "jimeng-cutout",
        "images": [{"url": "https://cdn/m.png", "format": "png"}],
        "credits": 0,
    })
    r = client.post("/v1/images/generations", headers=auth,
                    json={"model": "抠图", "image": "https://a/1.png"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"][0]["url"] == "https://cdn/m.png"
    assert body["credits"] == {"forecast": 0, "charged": 0}
    assert "task_id" not in body          # 没有任务，别造假一个


def test_cutout_async_endpoint_is_rejected(client, auth):
    r = client.post("/async/v1/images/generations", headers=auth,
                    json={"model": "抠图", "image": "https://a/1.png"})
    assert r.status_code == 400
    # 客户端面统一错误信封：{"error": {"code", "message"}}
    assert "同步" in r.json()["error"]["message"]


# ------------------------------------------------------------------ 局部重绘
def test_inpaint_draft_matches_captured_request():
    """局部重绘必须与 2026-11-19 的抓包同构（成功、1 积分 / 4 张）。"""
    cap = json.loads(EVIDENCE_INPAINT.read_text(encoding="utf-8"))
    real = json.loads(cap["draft_content"])
    ability = postedit.get("局部重绘")
    ours = json.loads(postedit.build_postedit_draft(
        ability=ability,
        source=postedit.build_unified_source(
            origin_uri="tos-o", mask_uri="tos-m", width=1024, height=1024)))

    rc, oc = real["component_list"][0], ours["component_list"][0]
    assert oc["generate_type"] == rc["generate_type"] == "unified_generate"
    ru, ou = rc["abilities"]["unified_generate"], oc["abilities"]["unified_generate"]
    assert ou["generate_type"] == ru["generate_type"] == 7      # InPaint
    assert ou["core_param"]["prompt"] == ru["core_param"]["prompt"] == ""

    r_ual = ru["unified_ability_list"][0]
    o_ual = ou["unified_ability_list"][0]
    assert o_ual["ability_code"] == r_ual["ability_code"] == "inpainting"

    r_res = r_ual["ability_param"]["resource_list"]
    o_res = o_ual["ability_param"]["resource_list"]
    assert [x["purpose"] for x in o_res] == [x["purpose"] for x in r_res] == [11, 12]
    assert [x["content"]["image"]["image_content"]["name"] for x in o_res] == ["origin", "mask"]
    # 资源节点的键集必须与抓包一致
    assert set(o_res[1]["content"]["image"]["image_content"]) == set(
        r_res[1]["content"]["image"]["image_content"])


def test_inpaint_metrics_follow_capture():
    cap = json.loads(EVIDENCE_INPAINT.read_text(encoding="utf-8"))
    m = json.loads(postedit.submit_metrics(
        postedit.get("局部重绘"), submit_id="sid", resolution="",
        width=1024, height=1024, image_uri="tos-o"))
    real = json.loads(cap["metrics_extra"])
    assert m["toolItemId"] == real["toolItemId"]
    assert m["tool_entry"] == real["tool_entry"]
    assert json.loads(m["sceneOptions"])[0]["scene"] == "ImageInPaintRepaintButton"


def test_inpaint_requires_mask():
    with pytest.raises(JimengParamError) as e:
        service.create_task(api_key_id=None, model="局部重绘", prompt="换个天空",
                            size="", count=1, images=["https://a/1.png"])
    assert "mask" in str(e.value)


def test_inpaint_stores_mask_and_does_not_flag_it():
    task = service.create_task(
        api_key_id=None, model="局部重绘", prompt="换个天空", size="", count=1,
        images=["https://a/1.png"], mask="https://a/m.png")
    assert task["mask_uri"] == "https://a/m.png"
    assert not any("不消费 mask" in d for d in task["degradations"])


def test_inpaint_prompt_goes_into_core_param():
    """提示词必须进 core_param —— 写死空串会让上游按"自由发挥"重绘。"""
    ability = postedit.get("局部重绘")
    draft = json.loads(postedit.build_postedit_draft(
        ability=ability,
        source=postedit.build_unified_source(
            origin_uri="tos-o", mask_uri="tos-m", width=1024, height=1024),
        prompt="换成一只黄色小鸭"))
    core = draft["component_list"][0]["abilities"]["unified_generate"]["core_param"]
    assert core["prompt"] == "换成一只黄色小鸭"


# ------------------------------------------------------------------ 消除笔
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _shape(o, path="", out=None):
    """把嵌套结构压平成 {路径: 值}，uuid 与时间戳归一。"""
    out = [] if out is None else out
    if isinstance(o, dict):
        for k in sorted(o):
            _shape(o[k], f"{path}.{k}", out)
    elif isinstance(o, list):
        out.append((path + "[]", f"list[{len(o)}]"))
        for i in o:
            _shape(i, path + "[]", out)
    else:
        if isinstance(o, str) and _UUID.match(o):
            v = "<uuid>"
        elif path.endswith("created_time_in_ms"):
            v = "<ms>"
        else:
            v = o
        out.append((path, v))
    return out


def _draft_diff(evidence, ability_key, origin="tos-O", mask="tos-M"):
    cap = json.loads(evidence.read_text(encoding="utf-8"))
    ours = json.loads(postedit.build_postedit_draft(
        ability=postedit.get(ability_key),
        source=postedit.build_unified_source(
            origin_uri=origin, mask_uri=mask, width=1024, height=1024),
        prompt=""))
    real = dict(_shape(json.loads(cap["draft_content"])))
    mine = dict(_shape(ours))
    diffs = {}
    for k in set(real) | set(mine):
        rv, mv = real.get(k, "<缺>"), mine.get(k, "<缺>")
        if rv != mv:
            diffs[k] = (rv, mv)
    return cap, diffs


def test_erase_draft_matches_captured_request():
    """消除笔草稿逐字段对拍（2026-11-19 抓包，零计费拦截所得）。"""
    cap, diffs = _draft_diff(EVIDENCE_ERASE, "消除笔")
    # 唯一允许的差异：我传的测试 uri 与抓包里真实的 tos uri
    uris = {k for k in diffs if k.endswith(("image_uri", ".uri"))}
    assert set(diffs) == uris, {k: v for k, v in diffs.items() if k not in uris}
    for k in uris:
        assert diffs[k][1] in ("tos-O", "tos-M"), (k, diffs[k])


def test_erase_key_presence_differs_from_inpaint():
    """键的**存在性**是契约：消除笔没有 prompt / option / benefitCount。

    这三处不能"优化"成空值或空对象 —— 抓包里它们是缺失，不是空。
    """
    ability = postedit.get("消除笔")
    draft = json.loads(postedit.build_postedit_draft(
        ability=ability,
        source=postedit.build_unified_source(
            origin_uri="tos-o", mask_uri="tos-m", width=1024, height=1024),
        prompt="擦掉这个"))
    ug = draft["component_list"][0]["abilities"]["unified_generate"]
    assert "prompt" not in ug["core_param"]
    assert ug["generate_type"] == 9
    param = ug["unified_ability_list"][0]["ability_param"]
    assert "option" not in param
    assert ug["unified_ability_list"][0]["ability_code"] == "inpainting_remove"

    scene = json.loads(postedit.submit_metrics(
        ability, submit_id="s", resolution="", width=1024, height=1024,
        image_uri="tos-o"))["sceneOptions"]
    assert "benefitCount" not in json.loads(scene)[0]


def test_inpaint_still_has_those_keys():
    """反向断言：局部重绘这三处**存在**，防止为了消除笔把通用分支改坏。"""
    ability = postedit.get("局部重绘")
    draft = json.loads(postedit.build_postedit_draft(
        ability=ability,
        source=postedit.build_unified_source(
            origin_uri="tos-o", mask_uri="tos-m", width=1024, height=1024),
        prompt="换成小鸭"))
    ug = draft["component_list"][0]["abilities"]["unified_generate"]
    assert ug["core_param"]["prompt"] == "换成小鸭"
    param = ug["unified_ability_list"][0]["ability_param"]
    assert param["option"] == {"type": "", "id": param["option"]["id"]}
    scene = json.loads(postedit.submit_metrics(
        ability, submit_id="s", resolution="", width=1024, height=1024,
        image_uri="tos-o"))["sceneOptions"]
    assert json.loads(scene)[0]["benefitCount"] == 1


def test_erase_metrics_follow_capture():
    cap = json.loads(EVIDENCE_ERASE.read_text(encoding="utf-8"))
    real = json.loads(cap["metrics_extra"])
    mine = json.loads(postedit.submit_metrics(
        postedit.get("消除笔"), submit_id="s", resolution="",
        width=1024, height=1024, image_uri="tos-o"))
    assert mine["toolItemId"] == real["toolItemId"] == "23722237505548"
    assert mine["tool_entry"] == real["tool_entry"]
    assert mine["tool_entry"]["modal_tool_key"] == "image_inpaint_remove"
    assert json.loads(mine["sceneOptions"])[0]["scene"] == "ImageInPaintEraserButton"


def test_erase_prompt_is_ignored_and_flagged():
    """消除笔不使用提示词：收下落库，但必须写进 degradations。"""
    task = service.create_task(
        api_key_id=None, model="消除笔", prompt="把它变成猫", size="", count=1,
        images=["https://a/1.png"], mask="https://a/m.png")
    assert task["mask_uri"] == "https://a/m.png"
    assert any("提示词" in d for d in task["degradations"])
    # 没传 prompt 时不该出现这条噪声
    quiet = service.create_task(
        api_key_id=None, model="消除笔", prompt="", size="", count=1,
        images=["https://a/1.png"], mask="https://a/m.png")
    assert not any("提示词" in d for d in quiet["degradations"])


def test_erase_requires_mask():
    with pytest.raises(JimengParamError) as e:
        service.create_task(api_key_id=None, model="消除笔", prompt="",
                            size="", count=1, images=["https://a/1.png"])
    assert "mask" in str(e.value)


# ------------------------------------------------------------------ 扩图
#: 抓包实测值 → 每边扩展量（**单边**，不是总扩展）。
#: 3:4 / 4:3 都是 1/6 是几何对称的巧合：长边放大 4/3，多出的 1/3 由两边均分。
_E = (16 / 9 - 1) / 2
@pytest.mark.parametrize("ratio,scale,expect", [
    ("3:4", 1.5, dict(top=1 / 6, bottom=1 / 6, left=0.0, right=0.0,
                      max_size=1365, image_ratio=2)),
    ("16:9", 2.0, dict(top=0.0, bottom=0.0, left=_E, right=_E,
                       max_size=1820, image_ratio=3)),
    ("4:3", 1.5, dict(top=0.0, bottom=0.0, left=1 / 6, right=1 / 6,
                      max_size=1365, image_ratio=4)),
    ("9:16", 1.5, dict(top=_E, bottom=_E, left=0.0, right=0.0,
                       max_size=1820, image_ratio=5)),
    ("original", 2.0, dict(top=0.5, bottom=0.5, left=0.5, right=0.5,
                           max_size=2048, image_ratio=1)),
])
def test_outpaint_geometry_matches_captures(ratio, scale, expect):
    """几何公式逐项对拍抓包。扩展量是**未取整**的理论值（3:4 的 top 恰为 1/6）。"""
    geo = postedit.outpaint_geometry(1024, 1024, ratio, scale)
    assert geo["max_size"] == expect["max_size"]
    assert geo["image_ratio"] == expect["image_ratio"]
    for k in ("top", "bottom", "left", "right"):
        # 抓包里 left/right 甚至彼此差 1 ulp（前端两个表达式），只比到 1e-12
        assert geo[k] == pytest.approx(expect[k], abs=1e-12), (k, geo[k], expect[k])


def test_outpaint_geometry_extends_short_side_only():
    """短边不动：宽图扩高、窄图扩宽。这是"不裁切"的含义。"""
    wide = postedit.outpaint_geometry(2000, 1000, "16:9")   # 已是 2:1，比 16:9 更宽
    assert wide["left"] == 0 and wide["right"] == 0
    assert wide["top"] > 0
    narrow = postedit.outpaint_geometry(1000, 2000, "16:9")  # 竖图 → 只扩宽
    assert narrow["top"] == 0 and narrow["bottom"] == 0
    assert narrow["left"] > 0


def test_outpaint_geometry_rejects_unknown_ratio():
    with pytest.raises(ValueError):
        postedit.outpaint_geometry(1024, 1024, "5:4")
    with pytest.raises(ValueError):
        postedit.outpaint_geometry(1024, 1024, "original", 0)


@pytest.mark.parametrize("evidence,ratio,scale,prompt", [
    ("2026-11-19-outpaint-generate.json", "16:9", 2.0, "扩展成森林背景"),
    ("2026-11-19-outpaint-noprompt.json", "16:9", 2.0, ""),
    ("2026-11-19-outpaint-ratio2x.json", "original", 2.0, ""),
    ("2026-11-19-outpaint-ratio34.json", "3:4", 1.5, ""),
    ("2026-11-19-outpaint-ratio916.json", "9:16", 1.5, ""),
    ("2026-11-19-outpaint-ratio43.json", "4:3", 1.5, ""),
])
def test_outpaint_draft_matches_captured_request(evidence, ratio, scale, prompt):
    """六份抓包逐字段对拍：只允许我传的测试 uri 不同。"""
    cap = json.loads((EVIDENCE_DIR / evidence).read_text(encoding="utf-8"))
    ability = postedit.get("扩图")
    src = postedit.build_unified_source(
        origin_uri="tos-O", width=1024, height=1024, ratio=ratio, scale=scale)
    ours = json.loads(postedit.build_postedit_draft(
        ability=ability, source=src, prompt=prompt))
    real = dict(_shape(json.loads(cap["draft_content"])))
    mine = dict(_shape(ours))
    def same(a, b):
        # 四边扩展是浮点：抓包里 left/right 差 1 ulp，严格相等会把噪声当差异
        if isinstance(a, float) and isinstance(b, float):
            return abs(a - b) <= 1e-12
        return a == b

    diffs = {k: (real.get(k, "<缺>"), mine.get(k, "<缺>"))
             for k in set(real) | set(mine)
             if not same(real.get(k, "<缺>"), mine.get(k, "<缺>"))}
    uris = {k for k in diffs if k.endswith(("image_uri", ".uri"))}
    assert set(diffs) == uris, {k: v for k, v in diffs.items() if k not in uris}
    for k in uris:
        assert diffs[k][1] == "tos-O", (k, diffs[k])


def test_outpaint_prompt_becomes_text_resource():
    """提示词**只在非空时**才多出一条 purpose=1 的文本资源（两份抓包对比）。"""
    ability = postedit.get("扩图")
    src = postedit.build_unified_source(origin_uri="tos-O", width=1024, height=1024)

    def resources(prompt):
        draft = json.loads(postedit.build_postedit_draft(
            ability=ability, source=src, prompt=prompt))
        return draft["component_list"][0]["abilities"]["unified_generate"][
            "unified_ability_list"][0]["ability_param"]["resource_list"]

    with_text = resources("扩展成森林背景")
    without = resources("")
    assert [r["purpose"] for r in with_text] == [1, 11]
    assert [r["purpose"] for r in without] == [11]
    assert with_text[0]["resource_type"] == 1
    assert with_text[0]["content"]["text"]["text_content"] == "扩展成森林背景"
    # 提示词同时写进 core_param（这是抓包确认的双写）
    draft = json.loads(postedit.build_postedit_draft(
        ability=ability, source=src, prompt="扩展成森林背景"))
    core = draft["component_list"][0]["abilities"]["unified_generate"]["core_param"]
    assert core["prompt"] == "扩展成森林背景"


def test_outpaint_metrics_lack_benefitcount_and_audit():
    """扩图的 metrics 比局部重绘**少**两个键（benefitCount / hasRejectedAudit）。"""
    ability = postedit.get("扩图")
    m = json.loads(postedit.submit_metrics(ability, submit_id="s", resolution="",
                                           width=1024, height=1024, image_uri="tos-O"))
    assert "hasRejectedAudit" not in m
    scene = json.loads(m["sceneOptions"])[0]
    assert "benefitCount" not in scene
    assert scene["scene"] == "ImageOutPaintButton"
    assert m["toolItemId"] == "23736268868364"
    assert m["tool_entry"]["modal_tool_key"] == "image_outpaint"


def test_outpaint_ratio_and_scale_are_exclusive():
    """选具体比例时倍率不参与 —— 要明确告知，而不是静默忽略。"""
    task = service.create_task(
        api_key_id=None, model="扩图", prompt="", size="", count=1,
        images=["https://a/1.png"], ratio="16:9", scale=3)
    assert task["extras"]["ratio"] == "16:9"
    assert any("倍率不参与" in d for d in task["degradations"])


def test_outpaint_rejects_bad_ratio_and_scale():
    with pytest.raises(JimengParamError) as e:
        service.create_task(api_key_id=None, model="扩图", prompt="", size="",
                            count=1, images=["https://a/1.png"], ratio="5:4")
    assert "比例" in str(e.value)
    with pytest.raises(JimengParamError) as e2:
        service.create_task(api_key_id=None, model="扩图", prompt="", size="",
                            count=1, images=["https://a/1.png"], scale=7)
    assert "倍率" in str(e2.value)


def test_default_ratio_is_original():
    task = service.create_task(
        api_key_id=None, model="扩图", prompt="", size="", count=1,
        images=["https://a/1.png"])
    assert task["extras"]["ratio"] == "original"
    assert task["extras"]["scale"] == 1.5


def test_submit_wires_extras_into_unified_source(monkeypatch):
    """建任务 → _submit 的接线：extras 里的 ratio/scale 必须真的变成几何参数。

    这条覆盖的是**单测与抓包对拍之间的缝隙**：builder 正确不代表
    service 把参数传对了。
    """
    from app import pool as pool_mod

    task = service.create_task(
        api_key_id=None, model="扩图", prompt="扩展成森林", size="", count=1,
        images=["tos-cn-i-x"], ratio="16:9")
    task.setdefault("extras", {})["source_size"] = {"width": 1024, "height": 1024}
    seen = {}

    class FakeClient:
        def submit_postedit(self, **kw):
            seen.update(kw)
            return "sid"

    class FakeRT:
        client = FakeClient()

    monkeypatch.setattr(pool_mod.pool, "runtime", lambda aid: FakeRT())
    service._submit(1, task, ["tos-cn-i-x"])

    src = seen["source"]
    assert src["origin"]["uri"] == "tos-cn-i-x"
    assert "mask" not in src                     # 扩图没有蒙版
    geo = src["outpainting"]
    assert geo["image_ratio"] == 3               # 16:9
    assert geo["max_size"] == 1820
    assert geo["left"] == pytest.approx((16 / 9 - 1) / 2, abs=1e-12)

    draft = json.loads(postedit.build_postedit_draft(
        ability=seen["ability"], source=src, prompt=seen["prompt"]))
    ug = draft["component_list"][0]["abilities"]["unified_generate"]
    res = ug["unified_ability_list"][0]["ability_param"]["resource_list"]
    assert [r["purpose"] for r in res] == [1, 11]   # 提示词 + 原图


def test_draft_summary_reads_unified_shape():
    """dry-run 摘要不能对 unified 族整片 null —— 那是诊断工具的唯一输出。"""
    ability = postedit.get("消除笔")
    draft = postedit.build_postedit_draft(
        ability=ability,
        source=postedit.build_unified_source(
            origin_uri="tos-o", mask_uri="tos-m", width=1024, height=1024))
    s = postedit.draft_summary(draft)
    assert s["ability_code"] == "inpainting_remove"
    assert s["generate_type"] == 9
    assert "prompt" not in s["core_param_keys"]
    assert s["has_option"] is False
    assert [r["purpose"] for r in s["resources"]] == [11, 12]

    outpaint = postedit.draft_summary(postedit.build_postedit_draft(
        ability=postedit.get("扩图"),
        source=postedit.build_unified_source(
            origin_uri="tos-o", width=1024, height=1024, ratio="3:4")))
    assert outpaint["ability_code"] == "outpainting"
    assert outpaint["out_painting"]["max_size"] == 1365
    assert outpaint["out_painting"]["image_ratio"] == 2


# ------------------------------------------------------------------ 智能编辑（v2）
def test_smart_edit_draft_matches_captured_request():
    """v2 草稿逐字段对拍。54 个字段，只允许我传的测试 uri/url 不同。"""
    cap = json.loads(EVIDENCE_EDIT.read_text(encoding="utf-8"))
    real = dict(_shape(json.loads(cap["draft_content"])))
    abl = postedit.get("智能编辑")
    src = postedit.build_smart_edit_source(
        origin_uri="tos-O", image_url="https://cdn/X.png", width=1024, height=1024)
    ours = dict(_shape(json.loads(postedit.build_postedit_draft(
        ability=abl, source=src, prompt="把红色方块换成一只猫", resolution="1.5k"))))
    diffs = {k: (real.get(k, "<缺>"), ours.get(k, "<缺>"))
             for k in set(real) | set(ours)
             if real.get(k, "<缺>") != ours.get(k, "<缺>")}
    # 允许差异只有：我传的 uri/url，以及 extra 里内嵌的同一对值
    for k, (rv, ov) in diffs.items():
        assert "tos-O" in str(ov) or "cdn/X.png" in str(ov), (k, rv, ov)
    assert len(real) == 54


def test_smart_edit_extend_carries_root_model():
    """v2 的 extend 是**两个键**：root_model + workspace_id（不是只有 workspace）。"""
    e = postedit.submit_extend(postedit.get("智能编辑"), "0")
    assert e["root_model"] == "high_aes_general_v55"
    assert e["workspace_id"] == 0
    # 反向：unified 族只有 workspace_id
    assert "root_model" not in postedit.submit_extend(postedit.get("局部重绘"), "0")


def test_smart_edit_extra_is_nested_string():
    """`extra` 是 JSON **字符串**（画布快照），不是对象；且标记为空。"""
    cap = json.loads(EVIDENCE_EDIT.read_text(encoding="utf-8"))
    ug_real = json.loads(cap["draft_content"])["component_list"][0]["abilities"]["unified_generate_v2"]
    assert isinstance(ug_real["unified_ability_list"][0]["extra"], str)

    abl = postedit.get("智能编辑")
    draft = json.loads(postedit.build_postedit_draft(
        ability=abl,
        source=postedit.build_smart_edit_source(
            origin_uri="tos-O", image_url="https://cdn/X.png", width=1024, height=1024),
        prompt="把红色方块换成一只猫", resolution="1.5k"))
    ug = draft["component_list"][0]["abilities"]["unified_generate_v2"]
    extra = ug["unified_ability_list"][0]["extra"]
    assert isinstance(extra, str)
    snap = json.loads(extra)
    assert snap["schema"] == "dreamina.smart_edit.canvas"
    assert snap["restore_data"]["elements"] == []      # 标记未取证 → 恒空
    assert snap["submit"]["output_resolution"] == "1.5k"
    assert snap["submit"]["plain_prompt"] == "把红色方块换成一只猫"
    # 能力节点上的键：prompt/model 直接挂在这里，没有 core_param
    assert ug["prompt"] == "把红色方块换成一只猫"
    assert ug["model"] == "high_aes_general_v55"
    assert "core_param" not in ug
    assert set(ug["unified_ability_list"][0]) == {"type", "id", "ability_code",
                                                  "resource_list", "extra"}


def test_smart_edit_metrics_follow_capture():
    cap = json.loads(EVIDENCE_EDIT.read_text(encoding="utf-8"))
    real = json.loads(cap["metrics_extra"])
    mine = json.loads(postedit.submit_metrics(
        postedit.get("智能编辑"), submit_id="s", resolution="1.5k",
        width=1024, height=1024, image_uri="tos-O"))
    assert set(mine) == set(real)
    for k in ("markTagCnt", "markBoxCnt", "imageTextCnt", "imageArrowCnt"):
        assert mine[k] == real[k] == 0             # 不支持标记 → 恒 0，但键要在
    assert mine["tool_entry"]["panel_key"] == "dreamina_all_tools_category_featured"
    scene = json.loads(mine["sceneOptions"])[0]
    assert scene["modelReqKey"] == "high_aes_general_v55"
    assert scene["imageInputNum"] == 1
    assert scene["resolutionType"] == "1.5k"


def test_smart_edit_requires_prompt():
    with pytest.raises(JimengParamError) as e:
        service.create_task(api_key_id=None, model="智能编辑", prompt="  ",
                            size="", count=1, images=["https://a/1.png"])
    assert "prompt" in str(e.value)


def test_smart_edit_resolution_tiers():
    """档位表来自 bundle（1k~8k），默认 1.5k —— 唯一跑过真实提交的档。"""
    abl = postedit.get("智能编辑")
    assert abl.resolutions[0] == "1.5k"
    assert set(abl.resolutions) == {"1k", "1.5k", "2k", "3k", "4k", "8k"}

    default = service.create_task(
        api_key_id=None, model="智能编辑", prompt="把红块换成猫", size="",
        count=1, images=["https://a/1.png"])
    assert default["resolution"] == "1.5k"

    for tier in ("2k", "4k", "8k"):
        t = service.create_task(
            api_key_id=None, model="智能编辑", prompt="x", size="", count=1,
            images=["https://a/1.png"], resolution=tier)
        assert t["resolution"] == tier, tier

    bad = service.create_task(
        api_key_id=None, model="智能编辑", prompt="x", size="", count=1,
        images=["https://a/1.png"], resolution="16k")
    assert bad["resolution"] == "1.5k"
    assert any("改用 1.5k" in d for d in bad["degradations"])


# ------------------------------------------------- 画布元素校验（移植自前端 bundle）
# 源: chunks/2v.*.js 的 `s(e)`（`t.elements.every(s)` 就是它）。移植而非重写：
# 字段名、可空性、枚举值都与前端逐条对齐，这样"符合契约"变成可执行断言。
def _is_num(v):
    import math
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _is_obj(v):
    return isinstance(v, dict)


def _opt_str(v):
    return v is None or isinstance(v, str)


def _pt(e):
    return _is_obj(e) and _is_num(e.get("x")) and _is_num(e.get("y"))


def _bounds(e):
    return _pt(e) and _is_num(e.get("width")) and _is_num(e.get("height"))


def validate_canvas_element(e) -> bool:
    """前端 `s(e)` 的等价实现：请求里 restore_data.elements 的每一项都要过它。"""
    if not _is_obj(e):
        return False
    if not (isinstance(e.get("id"), str) and _is_num(e.get("z_index"))
            and _is_num(e.get("created_at")) and _is_num(e.get("updated_at"))
            and isinstance(e.get("type"), str)):
        return False
    t = e["type"]
    if t == "marker":
        rec = e.get("recognition")
        return (_is_num(e.get("marker_index")) and e.get("mode") in ("point", "box")
                and _bounds(e.get("bounds")) and _is_obj(rec)
                and rec.get("status") in ("idle", "pending", "success", "failed")
                and _opt_str(rec.get("label"))
                and (rec.get("thumbnail") is None
                     or (_is_obj(rec["thumbnail"]) and _opt_str(rec["thumbnail"].get("uri"))
                         and _opt_str(rec["thumbnail"].get("url")))))
    if t == "brush":
        pts = e.get("points")
        st = e.get("style")
        return (isinstance(pts, list) and all(_pt(x) for x in pts)
                and _is_obj(st) and isinstance(st.get("color"), str)
                and _is_num(st.get("opacity")))
    if t == "arrow":
        st = e.get("style")
        return (_pt(e.get("start")) and _pt(e.get("end")) and _is_obj(st)
                and isinstance(st.get("color"), str)
                and st.get("variant") in ("arrow", "line"))
    if t == "text":
        st = e.get("style")
        return (isinstance(e.get("text"), str) and _bounds(e.get("bounds"))
                and _is_obj(st) and isinstance(st.get("color"), str)
                and _is_num(st.get("font_size")))
    return False


def _extra_elements(elements):
    src = postedit.build_smart_edit_source(
        origin_uri="tos-O", image_url="https://cdn/X.png",
        width=1024, height=1024, markers=elements)
    draft = json.loads(postedit.build_postedit_draft(
        ability=postedit.get("智能编辑"), source=src,
        prompt="改成夜景", resolution="1.5k"))
    return json.loads(
        draft["component_list"][0]["abilities"]["unified_generate_v2"]
        ["unified_ability_list"][0]["extra"])["restore_data"]["elements"]


def test_brush_and_arrow_pass_frontend_validator():
    """生成的 elements 必须过前端校验器（字段名/类型/可空性逐条对齐）。"""
    els = _extra_elements([
        {"type": "brush", "points": [[0.40, 0.60], [0.42, 0.62], [0.45, 0.66]]},
        {"type": "arrow", "start": [0.1, 0.1], "end": [0.3, 0.35], "variant": "line"},
    ])
    assert len(els) == 2
    assert [e["type"] for e in els] == ["brush", "arrow"]
    assert [e["z_index"] for e in els] == [1, 2]
    assert els[0]["style"] == {"color": "#E51739", "opacity": 1.0}
    assert els[1]["style"]["variant"] == "line"
    for e in els:
        assert validate_canvas_element(e), e
        # 时间是**秒**（前端 ×1000 才变毫秒）
        assert 1_700_000_000 < e["created_at"] < 2_000_000_000


def test_markers_reject_pixel_coordinates():
    """坐标必须是 0-1 归一化。像素坐标要当场报错，而不是画到画布外。"""
    with pytest.raises(ValueError) as e:
        postedit.build_brush_marker([[420.0, 690.0], [500.0, 720.0]])
    assert "0-1" in str(e.value)


def test_markers_reject_unopened_types():
    """marker（框选识别）与 text 不开放：要 recognition 状态机 / 排版度量。"""
    for kind in ("marker", "text"):
        with pytest.raises(ValueError) as e:
            postedit.normalize_markers([{"type": kind, "text": "x"}])
        assert "未开放" in str(e.value)


def test_markers_reject_unknown_type():
    with pytest.raises(ValueError) as e:
        postedit.normalize_markers([{"type": "circle"}])
    assert "不认识" in str(e.value)


def test_submit_wires_markers_into_extra(monkeypatch):
    """建任务 → _submit 的接线：markers 必须真的进 `extra.restore_data.elements`。"""
    from app import pool as pool_mod

    task = service.create_task(
        api_key_id=None, model="智能编辑", prompt="把红块换成猫", size="", count=1,
        images=["tos-cn-i-x"],
        markers=[{"type": "brush", "points": [[0.42, 0.70], [0.58, 0.84]]},
                 {"type": "arrow", "start": [0.1, 0.1], "end": [0.3, 0.35]}])
    task.setdefault("extras", {})["source_size"] = {
        "width": 1024, "height": 1024, "url": "https://cdn/x.png"}
    seen = {}

    class FakeClient:
        def submit_postedit(self, **kw):
            seen.update(kw)
            return "sid"

    class FakeRT:
        client = FakeClient()

    monkeypatch.setattr(pool_mod.pool, "runtime", lambda aid: FakeRT())
    service._submit(1, task, ["tos-cn-i-x"])

    src = seen["source"]
    assert src["origin"]["url"] == "https://cdn/x.png"     # v2 必须有签名 URL
    assert len(src["markers"]) == 2
    draft = json.loads(postedit.build_postedit_draft(
        ability=seen["ability"], source=src, prompt=seen["prompt"],
        resolution=task["resolution"]))
    extra = json.loads(draft["component_list"][0]["abilities"]
                       ["unified_generate_v2"]["unified_ability_list"][0]["extra"])
    els = extra["restore_data"]["elements"]
    assert [e["type"] for e in els] == ["brush", "arrow"]
    for e in els:
        assert validate_canvas_element(e), e


def test_capability_audit_passes_for_all_abilities():
    """`scripts/capability_audit.py` 的检查必须全过 —— 它同时也是回归网。

    这条测试的价值不在"再跑一遍断言"，而在**保证自检脚本本身没坏**：
    脚本会随能力演进被修改，改坏了没人发现就等于失去了体检手段。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "capability_audit", Path(__file__).resolve().parents[1]
        / "scripts" / "capability_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in postedit.ABILITIES:
        ok, notes = mod.audit_one(key, verbose=False)
        assert ok, f"{key}: {notes}"


def test_submit_wires_legacy_uri_as_object_node(monkeypatch):
    """legacy 族上传图的接线：`_submit` 必须把 uri 送进 `origin_image` **对象**。

    `_prepare_postedit` 会真上传，所以这里 mock 掉它 —— 要验的是"拿到 uri 之后
    怎么塞进草稿"，那正是 2026-11-19 出错的地方（塞成了字符串）。
    """
    from app import pool as pool_mod

    task = service.create_task(
        api_key_id=None, model="超清", prompt="", size="", count=1,
        images=["https://a/1.png"])
    seen = {}

    class FakeClient:
        def submit_postedit(self, **kw):
            seen.update(kw)
            return "sid"

    class FakeRT:
        client = FakeClient()

    monkeypatch.setattr(pool_mod.pool, "runtime", lambda aid: FakeRT())
    service._submit(1, task, ["tos-cn-i-uploaded"])

    src = seen["source"]
    assert "item_id" not in src
    assert src["image_uri"] == "tos-cn-i-uploaded"
    draft = json.loads(postedit.build_postedit_draft(
        ability=seen["ability"], source=src))
    origin = draft["component_list"][0]["abilities"]["normal_hd"][
        "postedit_param"]["origin_image"]
    assert isinstance(origin, dict) and origin["image_uri"] == "tos-cn-i-uploaded"


def test_outpaint_max_size_is_ceiling_not_exact():
    """`max_size` 是**上限**。800×600 扩 16:9 我们给 1067，上游出 1066（floor）。

    这条不是在测"我们算错了"，而是在钉住一个**已知偏差**：客户端若拿
    `max_size` 去校验产物尺寸会以为出错。改动这条时先想清楚是改公式还是改预期。
    """
    geo = postedit.outpaint_geometry(800, 600, "16:9")
    assert geo["max_size"] == 1067           # round(600 * 16/9)
    # 上游实际输出 1066 = floor(600 * (1 + 2/6))
    assert 800 * (1 + 2 * geo["left"]) == pytest.approx(1066.6667, abs=1e-3)
    assert int(800 * (1 + 2 * geo["left"])) == 1066
    # 四边扩展本身必须精确等于 1/6（这才是上游真正消费的量）
    assert geo["left"] == pytest.approx(1 / 6, abs=1e-12)
    assert geo["top"] == 0 and geo["bottom"] == 0

    # 正方形那次两种取整恰好相等，所以不能拿它当"max_size == 输出"的证据
    sq = postedit.outpaint_geometry(1024, 1024, "16:9")
    assert sq["max_size"] == 1820            # round 与 floor(1820.44) 同为 1820


# ---------------------------------------------------------- 产物归属（绑账号）
def test_remember_assets_and_lookup():
    """成功出图要记下 item 属于哪个号 —— item 引用不能跨号。"""
    from app import repo

    item = "7700000000000000001"
    repo.remember_assets(1, "task-x", [
        {"item_id": item, "history_id": "51990000000001"},
        {"item_id": "", "history_id": "51990000000001"},   # 空 item_id 跳过
    ])
    assert repo.account_for_item(item) == 1
    assert repo.account_for_item("7700000000000000002") is None
    assert repo.account_for_item("") is None


def test_item_reference_pins_account(monkeypatch):
    """item 引用必须落到产出它的那个号；指定的号不可用时**直接失败**。

    为什么不能降级换号：`detail_fix` 用错号会 `generate_failed` **且计费**。
    宁可明确报错，也不让它在别的号上烧钱。
    """
    from app import pool as pool_mod
    from app import repo

    item, hist = "7700000000000000003", "51990000000003"
    repo.remember_assets(2, "task-y", [{"item_id": item, "history_id": hist}])

    task = service.create_task(
        api_key_id=None, model="细节修复", prompt="", size="", count=1,
        images=[f"item:{item}:{hist}"])
    assert task["mode"] == "postedit"

    # 只用 1 号在线：2 号（作品归属号）不可用 ⇒ 必须失败，不能拿 1 号顶
    monkeypatch.setattr(repo, "list_accounts", lambda: [{"id": 1, "enabled": 1}])
    monkeypatch.setattr(repo, "dispatch_candidates",
                        lambda *a, **k: [{"id": 1, "enabled": 1}])
    called = {"submit": 0}
    monkeypatch.setattr(service, "_submit",
                        lambda *a, **k: called.__setitem__("submit", called["submit"] + 1))
    service.submit_task(task)
    assert called["submit"] == 0, "不该用错误的号提交"

    row = repo.get_task(task["id"])
    assert row["status"] == "failed"
    assert "属于账号 #2" in (row["error"] or "")
