#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主体（角色/商品）南向适配：只读列表。

**实测（2026-11-25）**：`POST /mweb/v1/dreamina_subject/get` 返回
`{next_cursor, has_more, data_list}`，参数必须用 **`cursor`**（不是 `offset`）、
`subject_source`（`mine`）。

主体是一整套子系统，上游端点：
    create / update / delete / get / generate_voice
    get_gen_record_list / update_gen_record

**本模块只实现 `get`**。原因是 `create` 需要上传参考图并走一套"主体生成"流程
（可能计费、可能需要审核），没有真实素材无法验证；把没验证过的写进来，
违反"清单里出现就必须真能提交"的原则。

主体的用途：让同一角色/商品在多张生成图里保持一致（`@主体名` 引用）。
"""
from __future__ import annotations

GET = "/mweb/v1/dreamina_subject/get"

#: 主体来源。`mine` = 我创建的；上游可能还有 `official` 等，未取证。
SOURCES = ("mine",)

#: 单页上限（保守值，上游未声明）
MAX_LIMIT = 50


class SubjectError(Exception):
    """带 `param` 的参数错误，路由层据此回 400 并指向具体字段。"""

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param


def validate_args(*, cursor=0, limit: int = 20, source: str = "mine") -> tuple[int, int, str]:
    """只校验不调用。**必须在拿 runtime 之前跑** —— 参数错时不该去连上游。

    返回值是规整后的 `(cursor, limit, source)`。
    """
    src = (source or "mine").strip()
    if src not in SOURCES:
        raise SubjectError(f"不支持的 subject_source {src!r}，可用：{list(SOURCES)}",
                           param="subject_source")
    try:
        cur = int(cursor)
    except (TypeError, ValueError):
        raise SubjectError("cursor 必须是整数", param="cursor") from None
    if cur < 0:
        raise SubjectError("cursor 不能为负", param="cursor")
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        raise SubjectError("limit 必须是整数", param="limit") from None
    if not (1 <= lim <= MAX_LIMIT):
        raise SubjectError(f"limit 需在 1–{MAX_LIMIT} 之间", param="limit")
    return cur, lim, src


def list_subjects(client, *, cursor: int = 0, limit: int = 20,
                  source: str = "mine") -> dict:
    """返回 `{subjects, next_cursor, has_more}`。`subjects` 原样透传上游字段。

    上游的 `data_list` 元素结构未取证（当前账号没有主体数据），因此**不解析、
    不改名**：把整条记录透传给调用方，避免按猜测的字段名做映射。
    """
    cur, lim, src = validate_args(cursor=cursor, limit=limit, source=source)

    try:
        resp = client.post_json(GET, {"cursor": cur, "limit": lim,
                                      "subject_source": src})
    except Exception as exc:                      # noqa: BLE001
        raise SubjectError(f"查询主体失败：{type(exc).__name__}") from exc

    data = resp.get("data") or {}
    items = data.get("data_list") or []
    return {
        "subjects": items if isinstance(items, list) else [],
        "next_cursor": data.get("next_cursor"),
        "has_more": bool(data.get("has_more")),
    }
