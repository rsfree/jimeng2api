#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`/v1/audio/speech` 的北向契约 + 适配器映射（离线，零网络）。

判据来自 audio-adapter skill §9：不是「能跑通一个 curl」，而是这些用例全过。
所有上游调用都被替身接住，测试里不出现真实会话、真实音频。
"""
from __future__ import annotations

import json

import pytest

from app import audio_api, service
from app.upstream import tts

MP3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 32
WAV = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 24
PCM = b"\x01\x02\x03\x04" * 16


@pytest.fixture
def ok(monkeypatch):
    """把上游替换成固定成功结果；返回可改写的 `box`。"""
    box = {"content": MP3, "media": "audio/mpeg", "ext": "mp3"}

    def fake_speech(**_kw):
        return tts.Audio(content=box["content"], media_type=box["media"],
                         extension=box["ext"])

    monkeypatch.setattr(audio_api, "_pick_runtime", lambda: {"id": 1})
    monkeypatch.setattr(service, "speech", fake_speech)
    return box


def post(client, auth, **body):
    payload = {"model": "seed-audio", "input": "你好世界", "voice": "huopo"}
    payload.update(body)
    return client.post("/v1/audio/speech", json=payload, headers=auth)


# ------------------------------------------------------------------ 成功路径
def test_default_format_returns_mp3(client, auth, ok):
    r = post(client, auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.content == MP3
    assert 'filename="speech.mp3"' in r.headers["content-disposition"]


@pytest.mark.parametrize("fmt,media,ext,blob", [
    ("wav", "audio/wav", "wav", WAV),
    ("pcm", "application/octet-stream", "pcm", PCM),
])
def test_declared_formats(client, auth, ok, fmt, media, ext, blob):
    ok.update(content=blob, media=media, ext=ext)
    r = post(client, auth, response_format=fmt)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(media)
    assert r.content == blob


def test_native_voice_id_passthrough(client, auth, ok):
    r = post(client, auth, voice="7366933865698431515")
    assert r.status_code == 200


def test_speed_bounds_accepted(client, auth, ok):
    for v in (0.5, 0.75, 1.0, 1.5, 2.0):
        assert post(client, auth, speed=v).status_code == 200


# ------------------------------------------------------------------ 参数拒绝
@pytest.mark.parametrize("fmt", ["ogg", "aac", "flac", "opus"])
def test_unsupported_format_is_400(client, auth, ok, fmt):
    r = post(client, auth, response_format=fmt)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["param"] == "response_format"
    assert err["type"] == "invalid_request_error"
    assert err["code"] is None          # 无标准码时必须为 null


@pytest.mark.parametrize("v", [0.4, 2.01, 5, -1])
def test_speed_out_of_range_is_400(client, auth, ok, v):
    r = post(client, auth, speed=v)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "speed"


def test_speed_nan_rejected(client, auth, ok):
    """NaN 不是合法 JSON，但 Python 的 json.loads 接受它 —— 必须挡住。"""
    raw = b'{"model":"seed-audio","input":"x","voice":"huopo","speed":NaN}'
    r = client.post("/v1/audio/speech", content=raw,
                    headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "speed"


def test_instructions_rejected(client, auth, ok):
    r = post(client, auth, instructions="温柔一点")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "instructions"


def test_unknown_field_rejected(client, auth, ok):
    r = post(client, auth, foo="bar")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "foo"


def test_unknown_model_rejected(client, auth, ok):
    r = post(client, auth, model="gpt-4o-mini-tts")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "model"


def test_unknown_voice_rejected(client, auth, ok):
    r = post(client, auth, voice="alloy")
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "voice"


@pytest.mark.parametrize("text", ["", "   ", "。！？，"])
def test_empty_or_punctuation_input(client, auth, ok, text):
    r = post(client, auth, input=text)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "input"


def test_overlong_input(client, auth, ok):
    r = post(client, auth, input="测" * (tts.MAX_CHARS + 1))
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "input"


def test_bad_sample_rate(client, auth, ok):
    r = post(client, auth, sample_rate=22050)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "sample_rate"


# ------------------------------------------------------------------ 传输层
def test_missing_auth_is_401(client, ok):
    r = client.post("/v1/audio/speech", json={"model": "seed-audio",
                                              "input": "x", "voice": "huopo"})
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["type"] == "authentication_error"
    assert err["code"] == "invalid_api_key"


def test_malformed_json(client, auth, ok):
    r = client.post("/v1/audio/speech", content=b"{not json",
                    headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_non_object_body(client, auth, ok):
    r = client.post("/v1/audio/speech", content=b"[]",
                    headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 400


def test_oversize_body_is_413(client, auth, ok):
    big = json.dumps({"model": "seed-audio", "input": "x" * 1100, "voice": "huopo",
                      "pad": "a" * (audio_api.MAX_BODY_BYTES + 10)}).encode()
    r = client.post("/v1/audio/speech", content=big,
                    headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "request_too_large"


# ------------------------------------------------------------------ 上游失败
def test_upstream_failure_is_502(client, auth, monkeypatch):
    monkeypatch.setattr(audio_api, "_pick_runtime", lambda: {"id": 1})

    def boom(**_kw):
        raise tts.TtsUpstreamError("上游拒绝合成")

    monkeypatch.setattr(service, "speech", boom)
    r = post(client, auth)
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"


def test_upstream_timeout_is_504(client, auth, monkeypatch):
    monkeypatch.setattr(audio_api, "_pick_runtime", lambda: {"id": 1})

    def slow(**_kw):
        raise tts.TtsUpstreamError("上游超时", timeout=True)

    monkeypatch.setattr(service, "speech", slow)
    r = post(client, auth)
    assert r.status_code == 504


def test_no_account_is_502(client, auth, monkeypatch):
    monkeypatch.setattr(audio_api, "_pick_runtime", lambda: None)
    r = post(client, auth)
    assert r.status_code == 502
    assert "账号" in r.json()["error"]["message"]


# ------------------------------------------------------------------ 纯函数
def test_speed_conversion():
    assert tts.speed_to_rate(1.0) == 0
    assert tts.speed_to_rate(0.5) == -50
    assert tts.speed_to_rate(2.0) == 100
    assert tts.speed_to_rate(0.7) == -30        # 浮点误差不得越界


def test_looks_like_rejects_mismatch():
    assert tts._looks_like("mp3", MP3)
    assert not tts._looks_like("mp3", WAV)      # 不许把 WAV 当 MP3 交付
    assert tts._looks_like("wav", WAV)
    assert not tts._looks_like("wav", MP3)


def test_capabilities_declaration():
    caps = tts.capabilities()
    assert "mp3" in caps["formats"] and "ogg" not in caps["formats"]
    assert caps["speed"]["min"] == 0.5 and caps["speed"]["max"] == 2.0
    assert caps["pitch"] == "fixed-0"
    assert caps["instructions"] is False


def test_capabilities_endpoint(client):
    r = client.get("/v1/audio/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["billing"] == "free" and body["upstream"] == "sync"


# ------------------------------------------------------------------ 音色清单
def test_voices_list(client):
    r = client.get("/v1/audio/voices")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["total"] == len(body["data"]) > 100
    first = body["data"][0]
    for k in ("name", "id", "gender", "age", "accent", "style", "category"):
        assert k in first, k
    assert first["id"].isdigit()


def test_voices_filter_gender(client):
    allv = client.get("/v1/audio/voices").json()["total"]
    male = client.get("/v1/audio/voices?gender=male").json()
    female = client.get("/v1/audio/voices?gender=female").json()
    assert 0 < male["total"] < allv
    assert 0 < female["total"] < allv
    assert all(v["gender"] == "male" for v in male["data"])
    assert all(v["gender"] == "female" for v in female["data"])


def test_voices_filter_accent_and_category(client):
    dialect = client.get("/v1/audio/voices?category=dialect").json()
    assert dialect["total"] > 0
    assert all(v["category"] == "dialect" for v in dialect["data"])
    dongbei = client.get("/v1/audio/voices?accent=dongbei").json()
    assert all(v["accent"] == "dongbei" for v in dongbei["data"])


def test_voices_query_string(client):
    r = client.get("/v1/audio/voices?q=紫薇").json()
    assert r["total"] >= 1
    assert any(v["name"] == "紫薇" for v in r["data"])


def test_voices_unknown_filter_is_empty(client):
    r = client.get("/v1/audio/voices?gender=nosuch").json()
    assert r["total"] == 0
    assert r["data"] == []


def test_voices_facets(client):
    facets = client.get("/v1/audio/voices").json()["facets"]
    assert "gender" in facets and "accent" in facets
    assert sum(facets["gender"].values()) >= 100


def test_capabilities_reports_voice_count(client):
    caps = client.get("/v1/audio/capabilities").json()
    assert caps["voice_count"] > 100


def test_upstream_rate_limit_is_429(client, auth, monkeypatch):
    """上游限流必须是 429（可退避），不能混进 502。"""
    monkeypatch.setattr(audio_api, "_pick_runtime", lambda: {"id": 1})

    def slow_down(**_kw):
        raise tts.TtsUpstreamError("上游限流", rate=True)

    monkeypatch.setattr(service, "speech", slow_down)
    r = post(client, auth)
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["type"] == "rate_limit_error"
    assert err["code"] == "rate_limit_exceeded"
    assert r.headers.get("retry-after") == "30"


def test_unknown_route_is_404(client):
    """404/405 走全局 handler，只有 code/message 两键 —— 见文档「已知差异」。

    这两个状态只出现在调用方拼错路径/方法时，不含业务语义，为它单独改全局
    信封会波及已在生产的图片族接口，不划算。
    """
    r = client.post("/v1/audio/nosuch", json={})
    assert r.status_code == 404
    assert "message" in r.json()["error"]


def test_wrong_method_is_405(client):
    r = client.get("/v1/audio/speech")
    assert r.status_code == 405
    assert "error" in r.json()
