# -*- coding: utf-8 -*-
"""视频生成：`video_base_component` + `gen_video`。

与图片（`aigc_draft/generate` + `component_list[].abilities.<能力>`）**不是一套**：
视频组件只有 `generate_type` / `abilities` / `process_type` 三个字段，参数全在
`abilities.gen_video.text_to_video_params` 里，且 `generate_type` 是**字符串**枚举。

契约来源（2026-11-20）：
* 结构：前端 bundle 的 `serializable()` 装饰器（`lib-agreement-1.*.js`）；
* 取值：`POST /mweb/v1/video_generate/get_common_config`（19 个模型的 model_req_key）；
* 实测：`dreamina_seedance_40_mini` / 720p / 4s 提交成功，48 次轮询后 success，
  产物 1280×720 mp4、可下载（HTTP 206 / video/mp4）。扣 **24 积分**。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .postedit import uid

#: 视频组件的 `generate_type`（字符串枚举，不是 gen_type 数字）
GENERATE_TYPE = "gen_video"
#: `DAVideoProcessType.VideoGen`
PROCESS_TYPE = 1
#: `DAVideoMode._default`
VIDEO_MODE = 2

#: `/mweb/v1/video_generate/get_common_config` 下发的边界
DURATION_MIN_MS = 4000
DURATION_MAX_MS = 15000
RESOLUTIONS = ("720p", "1080p")
#: 实测过的比例（"16:9" 这个写法被上游接受）
ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")

#: 实测单价（积分/秒，720p）。**不同模型差 3 倍**，所以放在模型上而不是全局：
#:   2.0 mini   4s=24、5s=30  → ≈6
#:   2.5 Flash  4s=76         → ≈19
#: 只用于面板提示与选型，**不参与计费**（真实扣费以上游为准，见 reconcile）。
#: 0 表示未实测，面板显示"—"而不是编一个数。
CREDITS_UNKNOWN = 0.0


@dataclass(frozen=True)
class VideoModel:
    """一个视频模型。`key` 是上游 `model_req_key`，只留在服务端。"""

    key: str                    # 对外 id（如 jimeng-video）
    model_req_key: str          # 上游 key（如 dreamina_seedance_40_mini）
    name: str
    aliases: tuple[str, ...] = ()
    durations: tuple[int, ...] = (5000,)     # 秒级档位，UI 上的 5s/10s
    resolutions: tuple[str, ...] = RESOLUTIONS
    experimental: bool = False
    #: 实测单价（积分/秒，720p）；0 = 未实测
    credits_per_second: float = CREDITS_UNKNOWN
    notes: str = ""
    extra: dict = field(default_factory=dict)


#: 先开放**有明确显示名**的四个主力；其余（recast / Wan / Vidu / HappyHorse）
#: 的显示名为空，语义未取证，等用到再加。
MODELS: dict[str, VideoModel] = {
    m.key: m for m in (
        VideoModel(
            key="jimeng-video", model_req_key="dreamina_seedance_40_mini",
            name="视频生成（Seedance 2.0 mini）",
            aliases=("视频生成", "视频", "seedance-mini"),
            durations=(5, 10), credits_per_second=6.0,
            notes="默认模型；实测 4s=24 积分、5s=30 积分（720p）",
        ),
        VideoModel(
            key="jimeng-video-fast", model_req_key="dreamina_seedance_40",
            name="视频生成（Seedance 2.0 Fast）",
            aliases=("视频快速",), durations=(5, 10),
        ),
        VideoModel(
            key="jimeng-video-pro", model_req_key="dreamina_seedance_40_pro",
            name="视频生成（Seedance 2.0）",
            aliases=("视频专业",), durations=(5, 10),
        ),
        VideoModel(
            key="jimeng-video-25", model_req_key="dreamina_seedance_45_pro",
            name="视频生成（Seedance 2.5）",
            aliases=("视频2.5",), durations=(5, 10),
        ),
        VideoModel(
            key="jimeng-video-25-flash", model_req_key="dreamina_seedance_45_flash",
            name="视频生成（Seedance 2.5 Flash）",
            aliases=("视频2.5快速", "seedance-flash"), durations=(5, 10),
            credits_per_second=19.0,
            notes="实测 4s=76 积分（720p），是 2.0 mini 的 3.2 倍；耗时约 108 秒",
        ),
    )
}

ALIASES: dict[str, str] = {}
for _m in MODELS.values():
    for _a in (_m.key, _m.name, *_m.aliases):
        ALIASES.setdefault(_a, _m.key)


def normalize_model(name: str) -> str:
    raw = (name or "").strip()
    if raw in ALIASES:
        return ALIASES[raw]
    low = raw.lower()
    for alias, key in ALIASES.items():
        if alias.lower() == low:
            return key
    return ""


def get(name: str) -> VideoModel | None:
    return MODELS.get(normalize_model(name))


def is_video(name: str) -> bool:
    return bool(normalize_model(name))


def public_models() -> list[dict]:
    """对外清单：只给能路由的字段，**不含 model_req_key**。"""
    return [{
        "id": m.key, "name": m.name, "kind": "video",
        "durations": list(m.durations), "resolutions": list(m.resolutions),
        "ratios": list(ASPECT_RATIOS),
        # `text` 只需 prompt；`text+image` 还可传 1~2 张图（首帧 / 首帧+尾帧）
        "input": "text+image",
        "max_images": 2,
        # 实测单价；0 表示没测过（不编数字）
        "credits_per_second": m.credits_per_second or None,
    } for m in MODELS.values() if not m.experimental]


def panel_models() -> list[dict]:
    """面板用：含 `model_req_key`（运维要靠它对上游日志）。"""
    return [{
        "id": m.key, "name": m.name, "model_req_key": m.model_req_key,
        "durations": list(m.durations), "resolutions": list(m.resolutions),
        "aliases": list(m.aliases), "notes": m.notes,
        "credits_per_second": m.credits_per_second or None,
    } for m in MODELS.values()]


def frame_node(uri: str, width: int = 0, height: int = 0,
               fmt: str = "") -> dict:
    """首/尾帧。前端构造是**扁平**的 `{width,height,image_uri,format}`，
    不是图片侧那种完整 image node —— 别套错。"""
    node: dict = {"image_uri": uri, "width": int(width or 0),
                  "height": int(height or 0)}
    if fmt:
        node["format"] = fmt
    return node


def _gen_input(*, prompt: str, duration_ms: int, resolution: str,
               first_frame: dict | None = None,
               end_frame: dict | None = None) -> dict:
    node = {
        "type": "", "id": uid(),
        "prompt": prompt,
        "video_mode": VIDEO_MODE,
        "fps": 24,
        "duration_ms": int(duration_ms),
        "resolution": resolution,
    }
    if first_frame:
        node["first_frame_image"] = first_frame
    if end_frame:
        node["end_frame_image"] = end_frame
    return node


def build_draft(model: VideoModel, *, prompt: str, duration_ms: int,
                resolution: str, aspect_ratio: str,
                first_frame: dict | None = None,
                end_frame: dict | None = None) -> str:
    """构造 `draft_content`。字段名照抄前端 `serializable()`。"""
    comp_id = uid()
    draft = {
        "type": "draft", "id": uid(), "min_version": "3.0.2",
        "min_features": [], "is_from_tsn": True, "version": "3.3.40",
        "main_component_id": comp_id,
        "component_list": [{
            "type": "video_base_component", "id": comp_id,
            "min_version": "3.0.2", "aigc_mode": "workbench",
            "generate_type": GENERATE_TYPE,
            "process_type": PROCESS_TYPE,
            "abilities": {
                "type": "", "id": uid(),
                "gen_video": {
                    "type": "", "id": uid(),
                    "text_to_video_params": {
                        "type": "", "id": uid(),
                        "video_gen_inputs": [_gen_input(
                            prompt=prompt, duration_ms=duration_ms,
                            resolution=resolution,
                            first_frame=first_frame, end_frame=end_frame)],
                        "video_aspect_ratio": aspect_ratio,
                        "model_req_key": model.model_req_key,
                        "priority": 0,
                    },
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


def build_submit_body(model: VideoModel, *, submit_id: str, prompt: str,
                      duration_ms: int, resolution: str,
                      aspect_ratio: str, aid: str = "",
                      first_frame: dict | None = None,
                      end_frame: dict | None = None) -> dict:
    """提交体。`extend.root_model` = model_req_key（视频侧就是这么推的）。"""
    return {
        "extend": {"root_model": model.model_req_key, "workspace_id": 0},
        "submit_id": submit_id,
        "metrics_extra": json.dumps(
            {"promptSource": "custom", "generateCount": 1}),
        "draft_content": build_draft(
            model, prompt=prompt, duration_ms=duration_ms,
            resolution=resolution, aspect_ratio=aspect_ratio,
            first_frame=first_frame, end_frame=end_frame),
        "http_common_info": {"aid": aid or ""},
    }
