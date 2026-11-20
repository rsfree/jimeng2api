# -*- coding: utf-8 -*-
"""视频：第三套草稿结构（video_base_component + gen_video）。

契约来源与实测见 `docs/UPSTREAM-VIDEO.md`。这里钉住三件事：
1. 草稿形状与前端 `serializable()` 一致；
2. 参数吸附（时长/分辨率/比例）不静默；
3. 产物解析认 `video.transcoded_video`（图片那条路解析不到视频）。
"""
from __future__ import annotations

import json

import pytest

from app import service
from app.upstream import protocol, video
from app.upstream.errors import JimengParamError


def test_default_model_is_seedance_20_mini():
    """默认视频模型与 UI 一致（`default_model_idx=3` → seadance_40_mini）。"""
    m = video.get("视频生成")
    assert m is not None
    assert m.key == "jimeng-video"
    assert m.model_req_key == "dreamina_seedance_40_mini"
    assert video.normalize_model("视频") == "jimeng-video"
    assert video.normalize_model("seedance-mini") == "jimeng-video"


def test_draft_matches_frontend_serializable():
    """字段名照抄前端装饰器：`generate_type` 是字符串、`process_type` 数字。"""
    body = video.build_submit_body(
        video.get("视频生成"), submit_id="sid-1", prompt="橘猫",
        duration_ms=5000, resolution="720p", aspect_ratio="16:9", aid="513695")
    draft = json.loads(body["draft_content"])
    comp = draft["component_list"][0]

    assert comp["type"] == "video_base_component"
    assert comp["generate_type"] == "gen_video"
    assert comp["process_type"] == 1
    assert set(comp["abilities"]) == {"type", "id", "gen_video"}

    tvp = comp["abilities"]["gen_video"]["text_to_video_params"]
    assert tvp["model_req_key"] == "dreamina_seedance_40_mini"
    assert tvp["video_aspect_ratio"] == "16:9"
    assert set(tvp["video_gen_inputs"][0]) >= {
        "prompt", "video_mode", "fps", "duration_ms", "resolution"}
    # root_model 由 model_req_key 推出（视频侧就是这么算的）
    assert body["extend"]["root_model"] == "dreamina_seedance_40_mini"


def test_public_models_hide_upstream_key():
    for m in video.public_models():
        assert "model_req_key" not in m
        assert not str(m.get("id", "")).startswith("dreamina")


def test_duration_and_ratio_are_clamped_with_reason():
    t = service.create_task(
        api_key_id=None, model="视频生成", prompt="猫", size="", count=1,
        duration=99, resolution="1080p", ratio="21:9")
    assert t["mode"] == "video"
    assert t["resolution"] == "1080p"
    assert t["extras"]["video"]["duration_s"] == 15      # 上限
    assert any("最长 15s" in d for d in t["degradations"])


def test_video_requires_prompt():
    with pytest.raises(JimengParamError) as e:
        service.create_task(api_key_id=None, model="视频生成", prompt="  ",
                            size="", count=1)
    assert "prompt" in str(e.value)


def test_parse_video_product():
    """视频产物在 `video.transcoded_video[档]`，取最高分辨率那一档。"""
    node = {
        "task": {"status": 50},
        "history_record_id": "51869467820556",
        "total_image_count": 1,
        "finished_image_count": 1,
        "item_list": [{
            "common_attr": {"id": "7710070900703661337",
                            "cover_url": "https://cdn/cover.jpeg"},
            "video": {
                "video_id": "v02870g10004dbvqq327dld",
                "duration": 5,
                "transcoded_video": {
                    "360p": {"video_url": "https://cdn/360.mp4", "width": 640,
                             "height": 360, "fps": 24},
                    "720p": {"video_url": "https://cdn/720.mp4", "width": 1280,
                             "height": 720, "fps": 24},
                },
            },
        }],
    }
    st = protocol.parse_task("sid-v", node)
    assert st["ok"] and st["history_id"] == "51869467820556"
    assert len(st["images"]) == 1
    it = st["images"][0]
    assert it["type"] == "video"
    assert it["url"] == "https://cdn/720.mp4"      # 取最大档
    assert it["resolution"] == "720p"
    assert it["format"] == "mp4"
    assert it["duration"] == 5
    assert it["cover_url"] == "https://cdn/cover.jpeg"
    assert it["item_id"] == "7710070900703661337"


def test_image_products_still_tagged():
    """图片项也要带 `type=image`，否则客户端只能靠"有没有 duration"反推。"""
    node = {
        "task": {"status": 50}, "total_image_count": 1,
        "finished_image_count": 1,
        "item_list": [{
            "image": {"large_images": [{"image_url": "https://cdn/a.png",
                                        "width": 1024, "height": 1024,
                                        "format": "png"}]},
            "common_attr": {"id": 111},
        }],
    }
    st = protocol.parse_task("sid-i", node)
    assert st["images"][0]["type"] == "image"


def test_submit_wires_video_params(monkeypatch):
    """建任务 → `_submit` 的接线：视频参数必须原样进提交体。

    不跑真实提交（24 积分/发），用 spy 验形状；端到端那一步已单独实测过
    （见 docs/UPSTREAM-VIDEO.md §0）。
    """
    from app import pool as pool_mod

    task = service.create_task(
        api_key_id=None, model="视频生成", prompt="橘猫伸懒腰", size="", count=1,
        duration=7, resolution="1080p", ratio="9:16")
    seen = {}

    class FakeClient:
        def submit_video(self, **kw):
            seen.update(kw)
            return "sid-v"

    class FakeRT:
        client = FakeClient()

    monkeypatch.setattr(pool_mod.pool, "runtime", lambda aid: FakeRT())
    service._submit(1, task, [])

    assert seen["model"].key == "jimeng-video"
    assert seen["duration_ms"] == 7000
    assert seen["resolution"] == "1080p"
    assert seen["aspect_ratio"] == "9:16"
    assert seen["prompt"] == "橘猫伸懒腰"


def test_first_frame_is_flat_node_not_image_component():
    """首帧是**扁平** `{width,height,image_uri,format}`，不是图片侧的 image node。

    这两种形态在项目里都出现过（`origin_image` 是完整 node），套错不会报错，
    只会在上游静默失效 —— 所以单独钉一条。
    """
    fn = video.frame_node("tos-cn-i-x", 1024, 768, "png")
    assert fn == {"image_uri": "tos-cn-i-x", "width": 1024, "height": 768,
                  "format": "png"}
    # 不传 format 时不该出现空串键
    assert "format" not in video.frame_node("tos-a", 1, 1)

    body = video.build_submit_body(
        video.get("视频生成"), submit_id="s", prompt="动起来",
        duration_ms=5000, resolution="720p", aspect_ratio="16:9",
        first_frame=fn, end_frame=video.frame_node("tos-cn-i-y", 1024, 768, "png"))
    gi = json.loads(body["draft_content"])["component_list"][0]["abilities"][
        "gen_video"]["text_to_video_params"]["video_gen_inputs"][0]
    assert gi["first_frame_image"]["image_uri"] == "tos-cn-i-x"
    assert gi["end_frame_image"]["image_uri"] == "tos-cn-i-y"


def test_video_accepts_at_most_two_images():
    t = service.create_task(
        api_key_id=None, model="视频生成", prompt="猫", size="", count=1,
        images=["https://a/1.png", "https://a/2.png"])
    assert t["image_uris"] == ["https://a/1.png", "https://a/2.png"]
    with pytest.raises(JimengParamError) as e:
        service.create_task(
            api_key_id=None, model="视频生成", prompt="猫", size="", count=1,
            images=["https://a/1.png", "https://a/2.png", "https://a/3.png"])
    assert "最多两张" in str(e.value)


def test_submit_wires_frames(monkeypatch):
    """`_submit` 要把前两张图分别接成首帧/尾帧。"""
    from app import pool as pool_mod

    task = service.create_task(
        api_key_id=None, model="视频生成", prompt="猫", size="", count=1,
        images=["https://a/1.png", "https://a/2.png"])
    seen = {}
    calls = []

    def fake_upload(account_id, ref):
        calls.append(ref)
        return f"tos-{len(calls)}", 1024, 768

    class FakeClient:
        def submit_video(self, **kw):
            seen.update(kw)
            return "sid"

    class FakeRT:
        client = FakeClient()

    monkeypatch.setattr(pool_mod.pool, "runtime", lambda aid: FakeRT())
    monkeypatch.setattr(service, "_upload_one", fake_upload)
    service._submit(1, task, task["image_uris"])

    assert calls == ["https://a/1.png", "https://a/2.png"]
    assert seen["first_frame"]["image_uri"] == "tos-1"
    assert seen["end_frame"]["image_uri"] == "tos-2"
    assert seen["first_frame"]["width"] == 1024
