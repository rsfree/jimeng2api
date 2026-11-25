#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词增强：北向契约 + 请求体结构（离线）。"""
from __future__ import annotations

import json

import pytest

from app import prompt_api, service
from app.upstream import prompt_enhance


@pytest.fixture
def ok(monkeypatch):
    monkeypatch.setattr(prompt_api, "_pick_runtime", lambda: {"id": 1})

    def fake(**_kw):
        return {"original": "一只猫", "enhanced": "## 主体\n橘猫", "pe_task_id": "video-pe-x"}

    monkeypatch.setattr(service, "enhance_prompt", fake)


def post(client, auth, **body):
    payload = {"prompt": "一只猫在草地上奔跑"}
    payload.update(body)
    return client.post("/v1/prompts/enhance", json=payload, headers=auth)


def test_success(client, auth, ok):
    r = post(client, auth)
    assert r.status_code == 200
    body = r.json()
    assert body["original"] and body["enhanced"]
    assert body["credits"] == 0


def test_empty_prompt(client, auth, ok):
    r = post(client, auth, prompt="   ")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "prompt"


def test_overlong_prompt(client, auth, ok):
    r = post(client, auth, prompt="猫" * 2001)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "prompt"


def test_unknown_model(client, auth, ok):
    r = post(client, auth, model="nosuch")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "model"


def test_unknown_field(client, auth, ok):
    r = post(client, auth, foo=1)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "foo"


def test_bad_context(client, auth, ok):
    r = post(client, auth, context="I2V")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "context"


def test_bad_duration(client, auth, ok):
    for v in (3, 16, "x"):
        r = post(client, auth, duration=v)
        assert r.status_code == 400, v
        assert r.json()["error"]["param"] == "duration"


def test_bad_ratio(client, auth, ok):
    r = post(client, auth, ratio="2:1")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "ratio"


def test_missing_auth(client, ok):
    r = client.post("/v1/prompts/enhance", json={"prompt": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_upstream_failure_is_502(client, auth, monkeypatch):
    monkeypatch.setattr(prompt_api, "_pick_runtime", lambda: {"id": 1})

    def boom(**_kw):
        raise prompt_enhance.EnhanceError("x")

    monkeypatch.setattr(service, "enhance_prompt", boom)
    r = post(client, auth)
    assert r.status_code == 502


def test_timeout_is_504(client, auth, monkeypatch):
    monkeypatch.setattr(prompt_api, "_pick_runtime", lambda: {"id": 1})

    def slow(**_kw):
        raise prompt_enhance.EnhanceError("x", timeout=True)

    monkeypatch.setattr(service, "enhance_prompt", slow)
    r = post(client, auth)
    assert r.status_code == 504


# ---------------------------------------------------------------- 请求体结构
def test_body_is_snake_case():
    """上游字段全是 snake_case —— 用 camelCase 会 ret=1000。"""
    b = prompt_enhance.build_create_body(prompt="测试", model_req_key="dreamina_seedance_40_mini")
    item = b["items"][0]
    assert "pe_task_id" in item and "content_generator_type" in item
    assert "prompt_context" in item and "input_snapshot" in item
    snap = item["input_snapshot"]
    assert "pe_client_input" in snap and "model_req_key" in snap


def test_generation_params_is_json_string():
    b = prompt_enhance.build_create_body(prompt="x", model_req_key="k")
    gp = b["items"][0]["input_snapshot"]["generation_params"]
    assert isinstance(gp, str)
    parsed = json.loads(gp)
    assert parsed["aspectRatio"] == "16:9" and parsed["fps"] == 24


def test_prompt_context_is_t2v():
    b = prompt_enhance.build_create_body(prompt="x", model_req_key="k")
    item = b["items"][0]
    assert item["prompt_context"] == "T2V"
    assert item["content_generator_type"] == "video"


def test_target_image_trims_video_sections(client, auth, monkeypatch):
    """target=image 要裁掉音乐/情节，保留主体/场景/风格。"""
    monkeypatch.setattr(prompt_api, "_pick_runtime", lambda: {"id": 1})
    script = ("# 透传结果\n## 主体\n橘猫\n## 场景\n草地\n## 风格\n暖调\n"
              "## 音乐\n轻快吉他\n## 情节\n从左侧跑入，发出啪嗒声\n")

    def fake(**_kw):
        return {"original": "一只猫", "enhanced": script, "pe_task_id": "p"}

    monkeypatch.setattr(service, "enhance_prompt", fake)
    r = post(client, auth, target="image")
    assert r.status_code == 200
    body = r.json()
    assert body["target"] == "image"
    assert "橘猫" in body["enhanced"] and "草地" in body["enhanced"]
    assert "吉他" not in body["enhanced"] and "啪嗒" not in body["enhanced"]
    # 原始视频脚本要保留，便于调用方自己取舍
    assert "吉他" in body["enhanced_video"]


def test_target_video_is_default(client, auth, ok):
    body = post(client, auth).json()
    assert body["target"] == "video"
    assert "enhanced_video" not in body


def test_bad_target(client, auth, ok):
    r = post(client, auth, target="audio")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "target"


def test_to_image_prompt_keeps_visual_only():
    script = "## 主体\n猫\n## 音乐\n钢琴\n## 情节\n奔跑\n## 风格\n清新\n"
    out = prompt_enhance.to_image_prompt(script)
    assert "猫" in out and "清新" in out
    assert "钢琴" not in out and "奔跑" not in out


def test_to_image_prompt_passthrough_without_sections():
    plain = "一只猫在草地上奔跑"
    assert prompt_enhance.to_image_prompt(plain) == plain
