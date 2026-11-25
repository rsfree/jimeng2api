#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主体（只读列表）：参数校验与透传（离线）。"""
from __future__ import annotations

import pytest

from app import prompt_api, service
from app.upstream import subject


@pytest.fixture
def ok(monkeypatch):
    monkeypatch.setattr(prompt_api, "_pick_runtime", lambda: {"id": 1})

    def fake(**_kw):
        return {"subjects": [{"id": "1", "name": "主角"}],
                "next_cursor": 20, "has_more": True}

    monkeypatch.setattr(service, "list_subjects", fake)


def test_list_ok(client, ok):
    r = client.get("/v1/subjects")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["subjects"][0]["name"] == "主角"
    assert body["has_more"] is True


def test_passes_params(client, ok, monkeypatch):
    seen = {}

    def spy(**kw):
        seen.update(kw)
        return {"subjects": [], "next_cursor": None, "has_more": False}

    monkeypatch.setattr(service, "list_subjects", spy)
    client.get("/v1/subjects?cursor=40&limit=5&source=mine")
    assert seen["cursor"] == 40 and seen["limit"] == 5 and seen["source"] == "mine"


def test_bad_source(client, monkeypatch):
    monkeypatch.setattr(prompt_api, "_pick_runtime", lambda: {"id": 1})
    r = client.get("/v1/subjects?source=official")
    assert r.status_code == 400
    assert "subject_source" in r.json()["error"]["message"]


def test_bad_limit(client, monkeypatch):
    monkeypatch.setattr(prompt_api, "_pick_runtime", lambda: {"id": 1})
    r = client.get(f"/v1/subjects?limit={subject.MAX_LIMIT + 1}")
    assert r.status_code == 400


def test_no_account(client, monkeypatch):
    monkeypatch.setattr(prompt_api, "_pick_runtime", lambda: None)
    r = client.get("/v1/subjects")
    assert r.status_code == 502


def test_payload_uses_cursor_not_offset():
    """上游要的是 cursor —— 用 offset 会 ret=1000。"""
    import inspect
    sig = inspect.signature(subject.list_subjects)
    assert "cursor" in sig.parameters
    assert "offset" not in sig.parameters
