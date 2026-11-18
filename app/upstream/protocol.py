#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""即梦上游的**纯协议层**：草稿构造、尺寸吸附、状态解析。

这里没有任何网络调用 —— 所有函数都能单测、能 dry-run。
`draft_content` 是**JSON 字符串**（上游双重编码），写成对象会得到 `1002`。
"""
from __future__ import annotations

import json
import math
import random
import uuid
from typing import Any, Sequence

#: 8 个比例槽（与网页端面板顺序一致）
RATIOS: dict[int, tuple[int, int]] = {
    1: (1, 1), 2: (3, 4), 3: (16, 9), 4: (4, 3),
    5: (9, 16), 6: (2, 3), 7: (3, 2), 8: (21, 9),
}
RATIO_LABEL: dict[int, str] = {
    1: "1:1", 2: "3:4", 3: "16:9", 4: "4:3",
    5: "9:16", 6: "2:3", 7: "3:2", 8: "21:9",
}

#: 任务状态码（前端 bundle 原文，两个模块一致）
TASK_STATUS: dict[int, str] = {
    0: "init", 10: "pre_check_reject", 20: "submitted", 30: "generate_failed",
    40: "post_check_reject", 42: "thinking", 45: "partial_success",
    50: "success", 100: "deleted",
}
TASK_TERMINAL = {10, 30, 40, 45, 50, 100}
TASK_OK = {45, 50}

BLEND_ABILITY_NAME = "byte_edit"


def uid() -> str:
    return str(uuid.uuid4())


def now_ms() -> int:
    import time
    return int(time.time() * 1000)


def parse_size(size: str) -> tuple[int, int]:
    """`2048x2048` / `2048*2048` / `2048X2048` → (w, h)。非法值抛 ValueError。"""
    raw = str(size or "").lower().replace("*", "x").replace("×", "x").strip()
    if "x" not in raw:
        raise ValueError(f"size 需形如 2048x2048，实得 {size!r}")
    w_s, h_s = raw.split("x", 1)
    w, h = int(w_s), int(h_s)
    if not (256 <= w <= 8192 and 256 <= h <= 8192):
        raise ValueError(f"size 越界（256~8192）：{w}x{h}")
    return w, h


def ratio_for_size(width: int, height: int) -> int:
    """把任意 w×h 吸附到 8 个比例槽之一（按面积比对数距离最近）。"""
    target = math.log(width / height)
    best, best_d = 1, 9e9
    for key, (rw, rh) in RATIOS.items():
        d = abs(target - math.log(rw / rh))
        if d < best_d:
            best, best_d = key, d
    return best


def resolution_type_for_size(width: int, height: int) -> str:
    """按长边吸附到上游的分辨率桶：1.5k / 2k / 4k。

    ⚠️ 上游 4K 像素不是整数倍（3:4 → 3520x4693），这里只做**桶判定**，
    不反推尺寸；真正的像素表由 `get_common_config` 下发。
    """
    long_side = max(width, height)
    if long_side <= 1600:
        return "1.5k"
    if long_side <= 2600:
        return "2k"
    return "4k"


def _meta() -> dict[str, Any]:
    return {
        "type": "", "id": uid(), "created_platform": 3,
        "created_platform_version": "", "created_time_in_ms": str(now_ms()),
        "created_did": "",
    }


def build_t2i_draft(*, prompt: str, model: str, count: int, width: int,
                    height: int, da_version: str, seed: int | None = None,
                    negative_prompt: str = "", sample_strength: float = 0.5,
                    resolution_type: str | None = None) -> str:
    if not prompt or not prompt.strip():
        raise ValueError("prompt 不能为空")
    comp_id = uid()
    draft = {
        "type": "draft", "id": uid(), "min_version": "3.0.2",
        "min_features": [], "is_from_tsn": True, "version": da_version,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "image_base_component", "id": comp_id,
            "min_version": "3.0.2", "aigc_mode": "workbench",
            "metadata": _meta(),
            "generate_type": "generate",
            "abilities": {
                "type": "", "id": uid(),
                "generate": {
                    "type": "", "id": uid(),
                    "core_param": {
                        "type": "", "id": uid(),
                        "model": model,
                        "prompt": prompt,
                        "negative_prompt": negative_prompt,
                        "seed": seed if seed is not None
                        else random.randint(1, 2 ** 32 - 1),
                        "sample_strength": sample_strength,
                        "image_ratio": ratio_for_size(width, height),
                        "large_image_info": {
                            "type": "", "id": uid(),
                            "height": height, "width": width,
                            "resolution_type": resolution_type
                            or resolution_type_for_size(width, height),
                        },
                        "intelligent_ratio": False,
                        "generate_type": 0,
                    },
                },
                "gen_option": {
                    "type": "", "id": uid(),
                    "gen_count": count, "generate_all": False,
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


def build_blend_draft(*, prompt: str, image_uris: Sequence[str], model: str,
                      count: int, width: int, height: int, da_version: str,
                      strength: float = 0.5,
                      resolution_type: str | None = None) -> str:
    """图生图（blend）。张数控制与文生图共用 `abilities.gen_option.gen_count`；
    `metrics_extra.generateCount` 只是埋点，管不住张数。"""
    uris = [u for u in image_uris if u]
    if not prompt or not prompt.strip():
        raise ValueError("图生图需要 prompt（描述要怎么改）")
    if not uris:
        raise ValueError("图生图需要至少一张 image_uri")
    imgs = [{
        "type": "image", "id": uid(), "source_from": "upload",
        "platform_type": 1, "name": "", "image_uri": u,
        "width": 0, "height": 0, "format": "", "uri": u,
    } for u in uris]
    comp_id = uid()
    draft = {
        "type": "draft", "id": uid(), "min_version": "3.0.2",
        "min_features": [], "is_from_tsn": True, "version": da_version,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "image_base_component", "id": comp_id,
            "min_version": "3.0.2", "aigc_mode": "workbench",
            "gen_type": 12, "generate_type": "blend",
            "abilities": {
                "type": "", "id": uid(),
                "blend": {
                    "type": "", "id": uid(), "min_features": [],
                    "core_param": {
                        "type": "", "id": uid(), "model": model,
                        "prompt": prompt, "sample_strength": strength,
                        "image_ratio": ratio_for_size(width, height),
                        "large_image_info": {
                            "type": "", "id": uid(),
                            "height": height, "width": width,
                            "resolution_type": resolution_type
                            or resolution_type_for_size(width, height),
                        },
                    },
                    "ability_list": [{
                        "type": "", "id": uid(), "name": BLEND_ABILITY_NAME,
                        "image_uri_list": list(uris), "image_list": imgs,
                        "strength": strength,
                    }],
                    "history_option": {"type": "", "id": uid()},
                    "prompt_placeholder_info_list": [
                        {"type": "", "id": uid(), "ability_index": 0}],
                    "postedit_param": {"type": "", "id": uid(),
                                       "generate_type": 0},
                },
                "gen_option": {
                    "type": "", "id": uid(),
                    "gen_count": count, "generate_all": False,
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


def submit_body(*, draft: str, submit_id: str, model: str, count: int,
                aid: str, mode: str) -> dict:
    """提交体。`draft_content` 直接放字符串（不要再 dumps 一次）。

    `metrics_extra` 两种能力形态不同，照抄抓包：t2i 有 generateId/isRegenerate，
    blend 没有；拼凑会让上游当成另一种能力。
    """
    if mode == "i2i":
        metrics = {
            "templateId": "", "generateCount": count,
            "promptSource": "custom", "templateSource": "",
            "lastRequestId": "", "originRequestId": "",
        }
    else:
        metrics = {
            "promptSource": "custom", "generateCount": count,
            "generateId": submit_id, "templateId": "0", "enterFrom": "click",
            "isRegenerate": False, "isBoxSelect": False, "isCutout": False,
        }
    return {
        "extend": {"root_model": model},
        "submit_id": submit_id,
        "metrics_extra": json.dumps(metrics, separators=(",", ":")),
        "draft_content": draft,
        "http_common_info": {"aid": int(aid)},
    }


def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def parse_task(submit_id: str, node: dict) -> dict:
    """把 `data[submit_id]` 翻译成统一结构。解析不到产物**不算成功**。"""
    task = node.get("task") or {}
    status_raw = _pick(task, "status", "task_status")
    if status_raw is None:
        status_raw = _pick(node, "status")
    try:
        status = int(status_raw)
    except (TypeError, ValueError):
        status = None
    finished = status in TASK_TERMINAL if status is not None else False
    ok = status in TASK_OK if status is not None else False
    out: dict[str, Any] = {
        "submit_id": submit_id,
        "status": status,
        "status_name": TASK_STATUS.get(status, f"unknown({status})")
        if status is not None else "unknown",
        "finished": finished,
        "ok": ok,
        "failed": bool(finished and not ok),
        "progress": "",
        "model": None,
        "cost": _pick(node, "forecast_generate_cost"),
        "fail_message": "",
        "images": [],
        "total": _pick(node, "total_image_count"),
        "finished_count": _pick(node, "finished_image_count"),
    }
    if out["failed"]:
        key = node.get("fail_starling_key") or ""
        msg = node.get("fail_starling_message") or ""
        out["fail_message"] = str(msg or key or out["status_name"]).strip()
    total, done = out["total"], out["finished_count"]
    if isinstance(total, int) and isinstance(done, int) and total > 0:
        out["progress"] = f"{done}/{total}"
    mi = node.get("model_info") or {}
    out["model"] = _pick(mi, "model_req_key", "model_name")
    for it in (node.get("item_list") or []):
        img = it.get("image") or {}
        large = img.get("large_images") or []
        url = w = h = fmt = None
        if large and isinstance(large[0], dict):
            url = large[0].get("image_url")
            w, h = large[0].get("width"), large[0].get("height")
            fmt = large[0].get("format")
        note = ""
        if not url:
            urls = ((it.get("common_attr") or {}).get("item_urls") or [])
            url = next((u for u in urls if u), None)
            note = "URL 取自 item_urls 兜底（可能带水印）" if url else ""
        if url:
            out["images"].append({
                "url": url, "width": w, "height": h,
                "format": fmt or img.get("format") or "png",
                "item_id": (it.get("common_attr") or {}).get("id"),
                "uri": img.get("image_uri") or "",
                "note": note,
            })
    # 🔴 45（partial_success）在**产物落库前**会短暂出现：
    # 2026-11-19 实跑（Seedream 5.0 Lite，1 张）t+7s 抓到 status=45、
    # finish_time=0、item_list=[]，t+18s 变 50 并带 1 张图。
    # 所以 45 + 未完成时间戳 = **还没完**，不能当终态；否则会把一次成功的
    # 生成记成"上游标记成功但未解析到产物"。
    finish_time = _pick(task, "finish_time") or node.get("finish_time") or 0
    if out["status"] == 45 and not finish_time:
        out["finished"] = False
        out["ok"] = False
        out["failed"] = False
        return out
    if out["ok"] and not out["images"]:
        # 状态说成功但没有可交付产物 ⇒ 按失败处理，不静默给空数组
        out["ok"] = False
        out["failed"] = True
        out["fail_message"] = "上游标记成功但未解析到产物"
    return out
