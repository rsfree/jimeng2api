#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型能力表：**上游数据优先，静态快照兜底，且兜底必须可见**。

`get_common_config` 是免费只读接口，按账号下发：模型、比例→像素、张数选项、
积分单价、输入图上限。代码里那张快照（`models_snapshot.json`，2026-11-18 抓）
只在**读不到上游**时使用，并会在面板里标注"快照"而不是假装实时。

🔴 别按模型名猜能力：`Seedream 5.5` 的 key 是 `high_aes_general_v55`；
名单会变，以这里解析出的表为准。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).with_name("models_snapshot.json")


@dataclass
class ModelSpec:
    key: str
    name: str
    tip: str = ""
    is_new: bool = False
    feats: list[str] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)
    default_count: int = 1
    resolutions: dict[str, dict] = field(default_factory=dict)
    prices: dict[str, dict] = field(default_factory=dict)
    max_images: int | None = None
    max_prompt_length: int | None = None

    def buckets(self) -> list[str]:
        return list(self.resolutions.keys())

    def count_options(self) -> list[int]:
        return list(self.counts) or [self.default_count or 1]

    def size_options(self) -> list[dict]:
        """所有 (分辨率, 比例) 组合，供面板下拉。"""
        out: list[dict] = []
        for bucket, cfg in self.resolutions.items():
            for s in cfg.get("sizes") or []:
                out.append({
                    "resolution": bucket, "ratio": s.get("ratio"),
                    "width": s.get("w"), "height": s.get("h"),
                    "label": f"{s.get('w')}×{s.get('h')}",
                })
        return out

    def price(self, resolution: str, mode: str = "t2i") -> int | None:
        table = self.prices.get(mode) or {}
        if resolution in table and table[resolution] is not None:
            return table[resolution]
        return table.get("default")

    def to_dict(self) -> dict:
        """**存储形态**：字段名与 dataclass 一一对应，`ModelSpec(**d)` 可无损还原。

        ⚠️ 这里曾经输出 `new` 而不是 `is_new`，导致能力表缓存第一次写入后
        再读取就 `TypeError` —— 面板刷新 30 分钟后整页 500。对外展示另走
        `to_api()`，不要为省一个函数把两种形态混在一起。
        """
        return {
            "key": self.key, "name": self.name, "tip": self.tip,
            "is_new": self.is_new, "feats": self.feats,
            "counts": self.counts, "default_count": self.default_count,
            "resolutions": self.resolutions, "prices": self.prices,
            "max_images": self.max_images,
            "max_prompt_length": self.max_prompt_length,
        }

    def to_api(self) -> dict:
        """**展示形态**：给面板/`/v1/models` 用，带 `new` 别名与展开的 `sizes`。"""
        return self.to_dict() | {"new": self.is_new, "sizes": self.size_options()}

    @classmethod
    def from_dict(cls, d: dict) -> "ModelSpec":
        clean = {k: v for k, v in d.items()
                 if k in cls.__dataclass_fields__}
        if "is_new" not in clean and "new" in d:
            clean["is_new"] = bool(d["new"])
        return cls(**clean)


@dataclass
class Registry:
    models: list[ModelSpec]
    captured_at: int
    live: bool
    source: str = ""

    @property
    def by_key(self) -> dict[str, ModelSpec]:
        return {m.key: m for m in self.models}

    def get(self, key: str) -> ModelSpec | None:
        return self.by_key.get(key)

    def default(self) -> ModelSpec | None:
        if not self.models:
            return None
        return self.models[0]

    def to_dict(self) -> dict:
        return {
            "captured_at": self.captured_at,
            "live": self.live,
            "source": self.source,
            "models": [m.to_api() for m in self.models],
        }


def _price(node: Any) -> dict:
    out: dict[str, Any] = {}
    if not isinstance(node, dict):
        return out
    out["default"] = (node.get("default") or {}).get("amount")
    for rule in node.get("rules") or []:
        amount = ((rule.get("benefit_key") or {}).get("amount"))
        for res in rule.get("resolution") or []:
            out[res] = amount
    return out


def _max_images(raw: Any) -> int | None:
    if isinstance(raw, list):
        vals = [x.get("max_image_num") or 0 for x in raw if isinstance(x, dict)]
        if not vals:
            return None
        return max(vals) or None
    if isinstance(raw, int):
        return raw
    return None


def normalize(cfg: dict) -> list[ModelSpec]:
    """把 `get_common_config.data` 归一化成 ModelSpec 列表（字段名两版兼容）。"""
    out: list[ModelSpec] = []
    for m in cfg.get("model_list") or []:
        cc = ((m.get("commercial_config") or {})
              .get("image_model_commerce_config") or {})
        res_map: dict[str, dict] = {}
        for bucket, rc in (m.get("resolution_map") or {}).items():
            res_map[bucket] = {
                "name": rc.get("resolution_name") or bucket,
                "sizes": [{
                    "ratio": s.get("ratio_type"),
                    "w": s.get("width"), "h": s.get("height"),
                } for s in (rc.get("image_ratio_sizes") or [])],
            }
        key = m.get("model_req_key") or ""
        if not key:
            continue
        out.append(ModelSpec(
            key=key,
            name=m.get("model_name") or key,
            tip=m.get("model_tip") or "",
            is_new=bool(m.get("is_new_model")),
            feats=list(m.get("feats") or []),
            counts=list(m.get("generate_count_options") or []),
            default_count=int(m.get("default_generate_count") or 1),
            resolutions=res_map,
            prices={"t2i": _price(cc.get("base")),
                    "i2i": _price(cc.get("blend"))},
            max_images=_max_images(m.get("input_image_limit")),
            max_prompt_length=((m.get("extra") or {}).get("max_prompt_length")),
        ))
    return out


def from_snapshot() -> Registry:
    raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    specs = [ModelSpec(
        key=m["key"], name=m["name"], tip=m.get("tip", ""),
        is_new=bool(m.get("new")), feats=m.get("feats") or [],
        counts=m.get("counts") or [], default_count=m.get("default_count") or 1,
        resolutions=m.get("resolutions") or {}, prices=m.get("prices") or {},
        max_images=m.get("max_images"),
        max_prompt_length=m.get("max_prompt_length"),
    ) for m in raw.get("models") or []]
    return Registry(models=specs,
                    captured_at=int(raw.get("captured_at") or 0),
                    live=False, source="静态快照")


def build(cfg: dict) -> Registry:
    return Registry(models=normalize(cfg), captured_at=int(time.time()),
                    live=True, source="上游实读")
