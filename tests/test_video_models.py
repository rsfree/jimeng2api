#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频模型表：新增模型不得破坏路由，实验模型默认不公布。"""
from __future__ import annotations

from app.upstream import video


def test_keys_unique_and_resolvable():
    keys = [m.key for m in video.MODELS.values()]
    assert len(keys) == len(set(keys))
    for m in video.MODELS.values():
        assert video.get(m.key) is m
        assert video.get(m.name) is m, m.name
        assert video.normalize_model(m.key)


def test_upstream_keys_unique():
    req = [m.model_req_key for m in video.MODELS.values()]
    assert len(req) == len(set(req)), "同一个上游 key 不能挂两个对外模型"


def test_aliases_do_not_shadow_builtin():
    for m in video.MODELS.values():
        for a in m.aliases:
            assert video.ALIASES[a] in video.MODELS, a


def test_experimental_hidden_by_default():
    default = video.public_models()
    assert all(not video.MODELS[e["id"]].experimental for e in default)
    with_exp = video.public_models(include_experimental=True)
    assert len(with_exp) > len(default)
    assert set(e["id"] for e in with_exp) >= set(e["id"] for e in default)


def test_public_models_hide_upstream_key():
    for e in video.public_models(include_experimental=True):
        assert "model_req_key" not in e
        assert e["kind"] == "video"
        assert e["input"] == "text+image"


def test_new_models_declare_no_fake_price():
    """未实测的模型不能编价格 —— credits_per_second=0 对外是 None。"""
    for e in video.public_models(include_experimental=True):
        if video.MODELS[e["id"]].experimental:
            assert e["credits_per_second"] is None
