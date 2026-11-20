#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后编辑（post-edit）能力层：智能超清 / 超清 / 细节修复 / 扩图。

## 两套草稿形态，不能混

本模块里 `Ability.shape` 决定用哪个 builder：

| shape | 能力 | 草稿特征 | 证据 |
|---|---|---|---|
| `prohd` | 智能超清（Paz） | `abilities.pro_hd` + `core_param.model=image_upscale_paz` + `resolution_type`，**没有** `gen_option` | **2026-11-19 真实提交成功**（2K，实扣 19 积分） |
| `legacy` | 超清 / 细节修复 | `abilities.<ability>.postedit_param` + `gen_option`，无 `core_param` | 超清：本号历史 12 条草稿；细节修复：技能实测 |

`origin_image` **是对象不是字符串**。2026-11-19 实测：传 `"tos-…"` 字符串得到
`ret=1000`，任务未创建、零计费 —— 那次失败换来的就是这个结论：

```json
{"type":"image","id":"…","source_from":"upload","platform_type":1,"name":"",
 "image_uri":"tos-…","width":1024,"height":1024,"format":"","uri":"tos-…"}
```

外层 `extend` 也按能力分：`prohd` 用 `{"workspace_id": …}`，`legacy` 用
`{"root_model": …}`。`workspace_id` 从 `/mweb/v1/workspace/list` 免费拿。

这里没有任何网络调用 —— 全部纯函数，能单测、能 dry-run。
"""
from __future__ import annotations

import json
import time
import struct
from dataclasses import dataclass
from typing import Any

from .protocol import _meta, uid

#: legacy 草稿的 min_version（照抄真实草稿）
MIN_VERSION = "3.2.9"
#: pro_hd 草稿的版本（照抄 2026-11-19 抓包）
PROHD_VERSION = "3.3.40"
#: 智能超清用的超分模型与场景名（抓包原文）
PROHD_MODEL = "image_upscale_paz"
PROHD_SCENE = "ImageProHD"
#: 工具市场条目 id（抓包原文，埋点用）
PROHD_TOOL_ITEM = "23750062126860"
#: unified_generate 草稿版本与工具条目（局部重绘抓包原文）
UNIFIED_VERSION = "3.3.40"
INPAINT_TOOL_ITEM = "23730762306060"
INPAINT_SCENE = "ImageInPaintRepaintButton"
#: 消除笔（InPaintRemove=9）的工具身份，抓包原文
ERASE_TOOL_ITEM = "23722237505548"
ERASE_SCENE = "ImageInPaintEraserButton"
#: 扩图（OutPaint=8）的工具身份，抓包原文
OUTPAINT_TOOL_ITEM = "23736268868364"
OUTPAINT_SCENE = "ImageOutPaintButton"
#: 智能编辑（unified_generate_v2 / smart_edit_v2），抓包原文
SMART_EDIT_TOOL_ITEM = "23797847084812"
SMART_EDIT_SCENE = "ImageSmartEdit"
SMART_EDIT_VERSION = "3.3.19"
#: 智能编辑走 v2 草稿，`extend` 里除了 workspace 还要 `root_model`
SMART_EDIT_MODEL = "high_aes_general_v55"
#: 画布默认值（抓包原文）。elements 为空时它们不被上游使用，只是画布快照。
SMART_EDIT_TOOL_OPTIONS = {
    "marker": {},
    "brush": {"color": "#E51739", "opacity": 1},
    "arrow": {"color": "#E51739", "variant": "arrow"},
    "text": {"color": "#E51739", "font_size": 0.035634743875278395},
    "eraser": {"size": 0.03333333333333333},
}
#: resource_list 的 purpose 编码（抓包：11=原图，12=蒙版）
PURPOSE_TEXT = 1
PURPOSE_ORIGIN = 11
PURPOSE_MASK = 12

#: 扩图 UI 的五档比例 → (image_ratio 枚举, 目标宽高比)。
#: 枚举不是按钮顺序：实测 原比例=1、3:4=2、16:9=3、4:3=4、9:16=5。
#: 目标比 None 表示"原比例"，此时用倍率（scale）等比放大。
OUTPAINT_RATIOS: dict[str, tuple[int, float | None]] = {
    "original": (1, None),
    "1:1": (1, None),
    "3:4": (2, 3 / 4),
    "16:9": (3, 16 / 9),
    "4:3": (4, 4 / 3),
    "9:16": (5, 9 / 16),
}
#: 原比例模式下的倍率（UI 的 1.5x / 2x / 3x）
OUTPAINT_SCALES = (1.5, 2.0, 3.0)


@dataclass(frozen=True)
class Ability:
    """一个后编辑能力的静态描述。

    `input` 是**载体约束**，不是偏好：
    * `item` —— 只认账号已有作品（`item_id` + `origin_history_id`）；
    * `uri`  —— 只认上传得到的 `image_uri`；
    * `both` —— 两种都收。
    """

    key: str                     # 对外模型名
    ability: str                 # 上游 `abilities` 里的键
    gen_type: int | None         # 上游 GenerateType 数字
    name: str
    input: str
    experimental: bool
    notes: str = ""
    shape: str = "legacy"        # legacy | prohd
    model: str = ""              # core_param.model（prohd）
    scene: str = ""              # sceneOptions.scene（prohd）
    min_features: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ()
    #: unified 协议的 ability_code（如 inpainting / inpainting_remove）
    ability_code: str = ""
    #: 是否必须给 mask（局部重绘/消除笔）
    needs_mask: bool = False
    #: 工具身份：metrics 的 toolItemId / tool_entry.modal_tool_key（抓包原文）
    tool_item: str = ""
    tool_key: str = ""
    #: `ability_param` 是否带空的 `option` 节点。消除笔的抓包里**没有**这个键，
    #: 与局部重绘不同 —— 键的存在性是契约的一部分，不能靠"空对象等价"糊过去。
    has_option: bool = True
    #: `core_param` 是否携带 `prompt` 键。消除笔不需要提示词，抓包里该键缺失。
    core_prompt: bool = True
    #: `sceneOptions[0]` 是否带 `benefitCount`。消除笔的抓包里没有这个键。
    scene_benefit: bool = True
    #: `metrics_extra` 是否带 `hasRejectedAudit`。扩图的抓包里没有这个键。
    metrics_audit: bool = True
    #: 扩图：`option.out_painting` 由 `outpaint_geometry()` 算出，
    #: 提示词会额外以 `purpose=1` 的文本资源进 `resource_list`。
    outpaint: bool = False
    #: v2 草稿的生成模型 key（智能编辑 = Seedream 的上游 key）。
    #: 同时进 `extend.root_model` 与 `sceneOptions[0].modelReqKey`。
    model_req_key: str = ""


ABILITIES: dict[str, Ability] = {
    "jimeng-pro-hd": Ability(
        key="jimeng-pro-hd", ability="pro_hd", gen_type=35,
        name="智能超清（Paz）", input="uri", experimental=False,
        shape="prohd", model=PROHD_MODEL, scene=PROHD_SCENE,
        min_features=("AIGC_GenerateType_ProHD", "AIGC_PostEdit_UpscaleModel"),
        resolutions=("2k", "4k", "8k"),
        notes="2026-11-19 真实提交成功（2K，实扣 19 积分），请求体见 docs/evidence/",
    ),
    "jimeng-hd": Ability(
        key="jimeng-hd", ability="normal_hd", gen_type=13,
        name="超清（普通）", input="both", experimental=False,
        notes=("2026-11-20 探测：上传图路径**可用且未扣费** —— 关键是 origin_image "
               "必须是**对象**（image 节点）；2026-11-19 用字符串被拒（ret=1000）。"
               "实测 1024→2048（2 倍），1 次样本余额不变"),
    ),
    "jimeng-detail-fix": Ability(
        key="jimeng-detail-fix", ability="super_resolution", gen_type=2,
        name="细节修复", input="item", experimental=False,
        notes="2026-09 实测：只认作品引用，带 origin_image 会 generate_failed 且计费",
    ),
    "jimeng-inpaint": Ability(
        key="jimeng-inpaint", ability="inpainting", gen_type=7,
        name="局部重绘", input="uri", experimental=False,
        shape="unified", ability_code="inpainting", needs_mask=True,
        tool_item=INPAINT_TOOL_ITEM, tool_key="image_inpaint",
        scene=INPAINT_SCENE,
        notes=("2026-11-19 实测通过（1 积分 / 4 张）：aigc_draft + unified_generate，"
               "原图 purpose=11、蒙版 purpose=12，见 docs/evidence/"),
    ),
    "jimeng-erase": Ability(
        key="jimeng-erase", ability="inpainting_remove", gen_type=9,
        name="消除笔", input="uri", experimental=False,
        shape="unified", ability_code="inpainting_remove", needs_mask=True,
        tool_item=ERASE_TOOL_ITEM, tool_key="image_inpaint_remove",
        scene=ERASE_SCENE, has_option=False, core_prompt=False,
        scene_benefit=False,
        notes=("2026-11-19 抓包（零计费拦截）：与局部重绘同构，差异为 "
               "gen_type=9、ability_code=inpainting_remove、core_param 无 prompt、"
               "ability_param 无 option；见 docs/evidence/2026-11-19-erase-generate.json"),
    ),
    "jimeng-cutout": Ability(
        key="jimeng-cutout", ability="saliency_seg", gen_type=None,
        name="抠图（去背景）", input="uri", experimental=False,
        shape="segment",
        notes=("2026-11-19 实测：saliency_seg 免费，返回的 PNG **alpha 通道即蒙版**，"
               "直接就是透明底结果（主体 rgb 保留）。不走 aigc_draft，没有 submit_id"),
    ),
    "jimeng-smart-edit": Ability(
        key="jimeng-smart-edit", ability="smart_edit_v2", gen_type=101,
        name="智能编辑", input="uri", experimental=False,
        shape="unified_v2", ability_code="smart_edit_v2",
        tool_item=SMART_EDIT_TOOL_ITEM, tool_key="smart_edit",
        scene=SMART_EDIT_SCENE, metrics_audit=True, core_prompt=True,
        model_req_key=SMART_EDIT_MODEL,
        #: 输出档位。档位表来自前端 bundle（1k/1.5k/2k/3k/4k/8k 的映射），
        #: **第一个是默认值**。只有 1.5k 跑过真实提交，其余档位未实测 ——
        #: 放开的理由是这个字段只是字符串透传，风险低于形态猜测。
        resolutions=("1.5k", "1k", "2k", "3k", "4k", "8k"),
        notes=("2026-11-19 抓包（零计费拦截）：unified_generate_v2，prompt 在能力节点上，"
               "多 extra 画布快照 + render_config + gen_option；"
               "标记（elements）未取证，当前只支持整图 + 提示词"),
    ),
    "jimeng-outpaint": Ability(
        key="jimeng-outpaint", ability="outpainting", gen_type=8,
        name="扩图", input="uri", experimental=False,
        shape="unified", ability_code="outpainting", needs_mask=False,
        tool_item=OUTPAINT_TOOL_ITEM, tool_key="image_outpaint",
        scene=OUTPAINT_SCENE, metrics_audit=False, scene_benefit=False,
        outpaint=True,
        notes=("2026-11-19 抓包（零计费拦截，5 份样本）：unified_generate + "
               "option.out_painting 四边扩展浮点 + max_size + image_ratio；"
               "比例与倍率互斥（选具体比例时倍率不参与），见 docs/evidence/"),
    ),
}

#: 明确**不支持**的能力 → 原因。命中时给 400 并说清缺什么。
UNSUPPORTED: dict[str, str] = {
}

#: 对外别名（大小写与中文都收）。
ALIASES: dict[str, str] = {
    "超清": "jimeng-hd", "变清晰": "jimeng-hd", "超分": "jimeng-hd",
    "hd": "jimeng-hd", "upscale": "jimeng-hd", "normal-hd": "jimeng-hd",
    "细节修复": "jimeng-detail-fix", "retouch": "jimeng-detail-fix",
    "智能超清": "jimeng-pro-hd", "pro-hd": "jimeng-pro-hd",
    "扩图": "jimeng-outpaint", "outpaint": "jimeng-outpaint",
    "抠图": "jimeng-cutout", "去背景": "jimeng-cutout", "cutout": "jimeng-cutout",
    "局部重绘": "jimeng-inpaint", "inpaint": "jimeng-inpaint",
    "消除笔": "jimeng-erase", "erase": "jimeng-erase",
    "智能编辑": "jimeng-smart-edit", "smart-edit": "jimeng-smart-edit",
}


def normalize_model(name: str) -> str:
    raw = (name or "").strip().lower()
    if not raw:
        return ""
    if raw in ABILITIES or raw in UNSUPPORTED:
        return raw
    return ALIASES.get(raw) or ALIASES.get(raw.replace("_", "-")) or ""


def get(name: str) -> Ability | None:
    return ABILITIES.get(normalize_model(name))


def is_postedit(name: str) -> bool:
    return bool(normalize_model(name))


def unsupported_reason(name: str) -> str:
    return UNSUPPORTED.get(normalize_model(name), "")


def panel_models() -> list[dict]:
    """面板用：**含内部字段**（ability / gen_type / shape / notes / experimental）。

    与 `public_models()` 的差别就是这份"多出来的东西" —— 运维要靠它判断
    "这个能力是什么形态、取证到哪一步"，而客户端只需要能路由的字段。
    面板只在 SSH 隧道后可见，所以这里不脱敏。
    """
    return [{
        "id": a.key, "name": a.name, "shape": a.shape,
        "ability": a.ability, "ability_code": a.ability_code,
        "gen_type": a.gen_type, "input": a.input,
        "needs_mask": a.needs_mask, "outpaint": a.outpaint,
        "experimental": a.experimental,
        "resolutions": list(a.resolutions),
        "ratios": [k for k in OUTPAINT_RATIOS if k != "1:1"] if a.outpaint else [],
        "scales": list(OUTPAINT_SCALES) if a.outpaint else [],
        "notes": a.notes,
    } for a in ABILITIES.values()]


def public_models(*, include_experimental: bool = False) -> list[dict]:
    """对外清单：只给**能用来发请求**的字段。

    `ability` / `gen_type` / `shape` 是草稿构造的内部标识；`notes` 是取证记录
    （含实扣积分与证据路径）。三者留在源码与面板，不进客户端响应。

    `include_experimental` 与建单准入条件同源：清单里出现的能力，一定是
    当前配置下真能提交的 —— 否则客户端会照单点菜，然后吃 400。
    """
    return [{
        "id": a.key, "name": a.name, "kind": "postedit",
        "input": a.input, "resolutions": list(a.resolutions),
        "needs_mask": a.needs_mask,
        # 扩图的两个可选参数：不列出来，调用方只能猜（或读文档）
        "ratios": [k for k in OUTPAINT_RATIOS if k != "1:1"] if a.outpaint else [],
        "scales": list(OUTPAINT_SCALES) if a.outpaint else [],
    } for a in ABILITIES.values()
        if include_experimental or not a.experimental]


# --------------------------------------------------------------- 图片尺寸
def image_size(data: bytes) -> tuple[int, int]:
    """从字节读宽高（PNG / JPEG / WebP），读不出返回 (0, 0)。

    上游的 `origin_image` 要带 `width/height`；拿不到真实值就填 0，
    不编数字 —— 尺寸参与超分倍率判断。
    """
    if not data:
        return 0, 0
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return int(w), int(h)
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg
        return 0, 0
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        if data[12:16] == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
        if data[12:16] == b"VP8 ":
            w = int.from_bytes(data[26:28], "little") & 0x3FFF
            h = int.from_bytes(data[28:30], "little") & 0x3FFF
            return w, h
    return 0, 0


# --------------------------------------------------------------- 草稿构造
def build_source(*, image_uri: str = "", width: int = 0, height: int = 0,
                 item_id: int | None = None,
                 origin_history_id: int | None = None) -> dict:
    """构造输入来源。作品引用与上传图互斥。"""
    if item_id and origin_history_id:
        return {"item_id": int(item_id), "origin_history_id": int(origin_history_id)}
    uri = (image_uri or "").strip()
    if uri:
        return {"image_uri": uri, "width": int(width or 0), "height": int(height or 0)}
    raise ValueError("后编辑需要作品引用(item_id+origin_history_id)或上传 uri")


def outpaint_geometry(width: int, height: int, ratio: str = "original",
                      scale: float = 1.5) -> dict:
    """扩图的 `option.out_painting`。纯函数，可脱离网络单测。

    UI 的两套模式（实测确认互斥）：

    * **具体比例** —— 短边不动，长边按目标比例扩展，倍率不参与；
    * **原比例** —— 四边各扩 `(scale-1)/2`，`max_size` = 长边 × scale。

    扩展量用**未取整**的理论尺寸算：抓包 3:4 的 top 是精确的 `1/6`，
    而不是取整后 `341/2/1024` 的值。只有 `max_size` 取整。
    """
    w, h = max(1, int(width or 0)), max(1, int(height or 0))
    key = (ratio or "original").strip().lower()
    if key not in OUTPAINT_RATIOS:
        raise ValueError(
            f"未知扩图比例 {ratio!r}，可选 {sorted(OUTPAINT_RATIOS)}")
    image_ratio, target = OUTPAINT_RATIOS[key]
    if target is None:
        # `scale or 1.5` 会把 0 当成缺省值吞掉 —— 0 必须被拒绝，不能被宽容
        k = 1.5 if scale is None else float(scale)
        if k <= 0:
            raise ValueError(f"扩图倍率必须为正数，收到 {scale!r}")
        out_w, out_h = w * k, h * k
    elif w / h < target:
        out_w, out_h = h * target, h        # 原图更窄 → 只扩宽
    else:
        out_w, out_h = w, w / target        # 原图更宽/相等 → 只扩高
    return {
        "bottom": (out_h - h) / 2 / h,
        "left": (out_w - w) / 2 / w,
        "right": (out_w - w) / 2 / w,
        "top": (out_h - h) / 2 / h,
        "max_size": round(max(out_w, out_h)),
        "image_ratio": image_ratio,
    }


def _element_base(kind: str, z_index: int, now: float | None) -> dict:
    ts = int(now if now is not None else time.time())
    return {"id": uid(), "z_index": int(z_index),
            "created_at": ts, "updated_at": ts, "type": kind}


def _norm_point(p, what: str = "point") -> dict:
    """0-1 归一化点。越界直接报错 —— 画到画布外的标记比报错更难查。"""
    if isinstance(p, dict):
        x, y = p.get("x"), p.get("y")
    elif isinstance(p, (list, tuple)) and len(p) == 2:
        x, y = p
    else:
        raise ValueError(f"{what} 需形如 {{'x':0.5,'y':0.5}} 或 [0.5, 0.5]")
    fx, fy = float(x), float(y)
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        raise ValueError(f"{what} 坐标必须在 0-1（归一化），收到 {fx}, {fy}")
    return {"x": fx, "y": fy}


def build_brush_marker(points, *, color: str = "#E51739",
                       opacity: float = 1.0, z_index: int = 1,
                       now: float | None = None) -> dict:
    """画笔涂抹元素。字段集来自前端校验函数（`z_index`/`created_at` 是秒）。"""
    pts = [_norm_point(pt) for pt in (points or [])]
    if len(pts) < 2:
        raise ValueError("brush 至少需要 2 个点")
    node = _element_base("brush", z_index, now)
    node["points"] = pts
    node["style"] = {"color": str(color), "opacity": float(opacity)}
    return node


def build_arrow_marker(start, end, *, color: str = "#E51739",
                       variant: str = "arrow", z_index: int = 1,
                       now: float | None = None) -> dict:
    """箭头/直线元素。`variant` 只能是 arrow 或 line（前端如此校验）。"""
    if variant not in ("arrow", "line"):
        raise ValueError(f"arrow 的 variant 只能是 arrow/line，收到 {variant!r}")
    node = _element_base("arrow", z_index, now)
    node["start"] = _norm_point(start, "start")
    node["end"] = _norm_point(end, "end")
    node["style"] = {"color": str(color), "variant": variant}
    return node


def normalize_markers(markers) -> list[dict]:
    """把请求里的 `markers` 归一成 elements。

    当前开放 `brush`（涂抹）与 `arrow`（箭头/直线）。

    `marker`（框选 + AI 识别）与 `text` **没有开放**：前者要 `recognition`
    状态机（idle/pending/success/failed + label/thumbnail），后者要排版度量
    （bounds 由字号与换行算出）。靠猜填不出可信值，宁可不收。
    """
    out: list[dict] = []
    for i, m in enumerate(markers or [], 1):
        if not isinstance(m, dict):
            raise ValueError(f"markers[{i}] 必须是对象")
        kind = str(m.get("type") or "").lower()
        if kind == "brush":
            out.append(build_brush_marker(
                m.get("points"), color=m.get("color") or "#E51739",
                opacity=float(m.get("opacity") or 1.0), z_index=i))
        elif kind == "arrow":
            out.append(build_arrow_marker(
                m.get("start"), m.get("end"),
                color=m.get("color") or "#E51739",
                variant=m.get("variant") or "arrow", z_index=i))
        elif kind in ("marker", "text"):
            raise ValueError(
                f"markers[{i}] 的 {kind!r} 未开放（marker 需要 recognition 状态机，"
                "text 需要排版度量），当前支持 brush / arrow")
        else:
            raise ValueError(
                f"markers[{i}] 的 type={kind!r} 不认识，支持 brush / arrow")
    return out


def build_smart_edit_source(*, origin_uri: str, image_url: str,
                            width: int = 0, height: int = 0,
                            markers=None) -> dict:
    """智能编辑的来源：**必须带签名 URL**。

    与其它 unified 能力的区别：v2 的 `resource_list` 里 `image` 节点是
    `image_uri` + `image_url` + 宽高，没有 `name` / `format` / `source_from`。
    `image_url` 从 `client.image_by_uri()`（免费只读）取，会过期。
    """
    if not origin_uri or not image_url:
        raise ValueError("智能编辑需要 image_uri 与带签名的 image_url")
    return {"origin": {"uri": origin_uri, "url": image_url,
                       "width": int(width or 0), "height": int(height or 0)},
            "markers": normalize_markers(markers)}


def build_unified_source(*, origin_uri: str, mask_uri: str = "",
                         width: int = 0, height: int = 0,
                         mask_width: int = 0, mask_height: int = 0,
                         ratio: str = "original", scale: float = 1.5) -> dict:
    """unified 协议的来源。蒙版可选（扩图没有蒙版）。"""
    if not origin_uri:
        raise ValueError("unified 需要原图 uri")
    src = {
        "origin": {"uri": origin_uri, "width": int(width or 0),
                   "height": int(height or 0)},
        "outpainting": outpaint_geometry(int(width or 0), int(height or 0),
                                         ratio=ratio, scale=scale),
    }
    if mask_uri:
        src["mask"] = {
            "uri": mask_uri,
            "width": int(mask_width or width or 0),
            "height": int(mask_height or height or 0),
        }
    return src


def _image_content(uri: str, width: int, height: int, name: str) -> dict:
    return {
        "type": "image", "id": uid(), "source_from": "", "platform_type": 1,
        "name": name, "image_uri": uri,
        "width": int(width or 0), "height": int(height or 0),
        "format": "", "uri": uri,
    }


def _resource(uri: str, width: int, height: int, name: str, purpose: int) -> dict:
    return {
        "type": "", "id": uid(),
        "content": {"type": "", "id": uid(),
                    "image": {"type": "", "id": uid(),
                              "image_content": _image_content(uri, width, height, name)}},
        "resource_type": 2, "purpose": purpose,
    }


def _text_resource(text: str) -> dict:
    """扩图的提示词是资源：`resource_type=1`、`purpose=1`、`content.text`。"""
    return {
        "type": "", "id": uid(),
        "content": {"type": "", "id": uid(),
                    "text": {"type": "", "id": uid(), "text_content": str(text)}},
        "resource_type": 1, "purpose": PURPOSE_TEXT,
    }


def _core_param(ability: Ability, prompt: str) -> dict:
    """`core_param` 的键集随能力变：消除笔不带 `prompt`（抓包如此）。"""
    node = {"type": "", "id": uid()}
    if ability.core_prompt:
        node["prompt"] = str(prompt or "")
    node["generate_type"] = 0
    return node


def _ability_param(ability: Ability, source: dict, prompt: str = "") -> dict:
    """`ability_param`。资源与 `option` 的形态都随能力变：

    * 扩图：提示词文本（purpose 1，仅非空时）+ 原图（11），`option.out_painting`；
    * 局部重绘：原图（11）+ 蒙版（12），`option` 空节点；
    * 消除笔：同局部重绘，但**没有** `option` 键。
    """
    res: list[dict] = []
    if ability.outpaint and str(prompt or "").strip():
        res.append(_text_resource(prompt))
    o = source["origin"]
    res.append(_resource(o["uri"], o["width"], o["height"], "origin", PURPOSE_ORIGIN))
    if ability.needs_mask:
        m = source["mask"]
        res.append(_resource(m["uri"], m["width"], m["height"], "mask", PURPOSE_MASK))
    node: dict[str, Any] = {"type": "", "id": uid(), "resource_list": res}
    if ability.outpaint:
        geo = source.get("outpainting") or {}
        node["option"] = {
            "type": "", "id": uid(),
            "out_painting": {
                "type": "", "id": uid(),
                "bottom": geo["bottom"], "left": geo["left"],
                "right": geo["right"], "top": geo["top"],
                "max_size": geo["max_size"], "image_ratio": geo["image_ratio"],
            },
        }
    elif ability.has_option:
        node["option"] = {"type": "", "id": uid()}
    return node


def _build_unified(ability: Ability, source: dict, version: str,
                   prompt: str = "") -> str:
    """`unified_generate` 草稿：局部重绘/消除笔这一族。

    与 pro_hd 的区别：多资源用 `resource_list` + `purpose` 编码
    （11=原图、12=蒙版），能力由 `ability_code` 选。
    """
    if "origin" not in source:
        raise ValueError("unified 草稿需要 origin 资源")
    if ability.needs_mask and "mask" not in source:
        raise ValueError(f"{ability.name} 的草稿需要 mask 资源")
    comp_id = uid()
    draft = {
        "type": "draft", "id": uid(), "min_version": "3.0.2",
        "min_features": [], "is_from_tsn": True, "version": version,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "image_base_component", "id": comp_id,
            "min_version": "3.0.2", "aigc_mode": "workbench",
            "metadata": _meta(),
            "generate_type": "unified_generate",
            "abilities": {
                "type": "", "id": uid(),
                "unified_generate": {
                    "type": "", "id": uid(),
                    "core_param": _core_param(ability, prompt),
                    "generate_type": ability.gen_type,
                    "unified_ability_list": [{
                        "type": "", "id": uid(), "min_features": [],
                        "ability_code": ability.ability_code,
                        "ability_param": _ability_param(ability, source, prompt),
                    }],
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


def _smart_edit_extra(source: dict, prompt: str, resolution: str) -> str:
    """`extra` 是**嵌套 JSON 字符串**（画布快照），不是对象。"""
    img = {"uri": source["origin"]["uri"], "url": source["origin"]["url"],
           "width": int(source["origin"]["width"] or 0),
           "height": int(source["origin"]["height"] or 0)}
    payload = {
        "schema": "dreamina.smart_edit.canvas",
        "version": 1,
        "restore_data": {
            "version": 1,
            "source_image": img,
            "elements": list(source.get("markers") or []),
            "prompt_contents": [{"type": "text", "text": prompt}],
            "tool_options": SMART_EDIT_TOOL_OPTIONS,
        },
        "submit": {
            "prompt": prompt,
            "plain_prompt": prompt,
            "output_resolution": resolution,
            "submitted_image": img,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _build_unified_v2(ability: Ability, source: dict, prompt: str,
                      resolution: str) -> str:
    """`unified_generate_v2` 草稿：智能编辑这一族。

    与 `unified_generate` 的结构差别不小：prompt 在能力节点上、
    资源节点带 `image_url`、多 `extra`（画布快照）/ `render_config` / `gen_option`，
    且**没有** `core_param` 与 `ability_param`。
    """
    if "origin" not in source:
        raise ValueError("unified_v2 草稿需要 origin 资源")
    text = str(prompt or "").strip()
    if not text:
        raise ValueError("智能编辑必须给提示词（v2 用 prompt 驱动）")
    img = source["origin"]
    comp_id = uid()
    draft = {
        "type": "draft", "id": uid(), "min_version": SMART_EDIT_VERSION,
        "min_features": ["AIGC_GenerateType_SmartEdit"], "is_from_tsn": True,
        "version": UNIFIED_VERSION,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "image_base_component", "id": comp_id,
            "min_version": "3.0.2", "aigc_mode": "workbench",
            "metadata": _meta(),
            "generate_type": "unified_generate_v2",
            "abilities": {
                "type": "", "id": uid(),
                "unified_generate_v2": {
                    "type": "", "id": uid(),
                    "min_version": SMART_EDIT_VERSION,
                    "min_features": ["AIGC_GenerateType_SmartEdit"],
                    "prompt": text,
                    "model": ability.model_req_key,
                    "generate_type": ability.gen_type,
                    "unified_ability_list": [{
                        "type": "", "id": uid(),
                        "ability_code": ability.ability_code,
                        "resource_list": [{
                            "type": "", "id": uid(),
                            "content": {"type": "", "id": uid(),
                                        "image": {"type": "", "id": uid(),
                                                  "image_uri": img["uri"],
                                                  "image_url": img["url"],
                                                  "width": int(img["width"] or 0),
                                                  "height": int(img["height"] or 0)}},
                            "resource_type": 2, "purpose": PURPOSE_ORIGIN,
                        }],
                        "extra": _smart_edit_extra(source, text, resolution),
                    }],
                    "render_config": {"type": "", "id": uid(),
                                      "resolution_type": resolution,
                                      "intelligent_ratio": True},
                    "gen_option": {"type": "", "id": uid(),
                                   "gen_count": 1, "gen_model_type": 2},
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


def _image_node(uri: str, width: int, height: int) -> dict:
    return {
        "type": "image", "id": uid(), "source_from": "upload",
        "platform_type": 1, "name": "", "image_uri": uri,
        "width": int(width or 0), "height": int(height or 0),
        "format": "", "uri": uri,
    }


def build_submit_body(*, ability: Ability, source: dict, submit_id: str,
                      aid: int, resolution: str = "2k", count: int = 1,
                      extra: dict[str, Any] | None = None,
                      workspace_id: str = "", prompt: str = "") -> dict:
    """提交体外层。**纯函数**：探针的 dry-run 与真实提交共用同一份构造。

    以前探针自己拼 body（还带 `extend.root_model`），它验证的就不是生产路径了。
    """
    o = source.get("origin") or source
    return {
        "extend": submit_extend(ability, workspace_id),
        "submit_id": submit_id,
        "metrics_extra": submit_metrics(
            ability, submit_id=submit_id, resolution=resolution,
            width=int(o.get("width") or 0), height=int(o.get("height") or 0),
            image_uri=str(o.get("uri") or source.get("image_uri") or "")),
        "draft_content": build_postedit_draft(
            ability=ability, source=source, resolution=resolution,
            count=count, extra=extra, prompt=prompt),
        "http_common_info": {"aid": int(aid)},
    }


def build_postedit_draft(*, ability: Ability, source: dict,
                         resolution: str = "2k", count: int = 1,
                         extra: dict[str, Any] | None = None,
                         version: str = "", prompt: str = "") -> str:
    """生成后编辑草稿（**JSON 字符串**，与 t2i/i2i 一样双重编码）。"""
    if not source:
        raise ValueError("后编辑草稿缺输入图")
    if ability.shape == "prohd":
        return _build_prohd(ability, source, resolution, version or PROHD_VERSION)
    if ability.shape == "unified":
        return _build_unified(ability, source, version or UNIFIED_VERSION,
                              prompt=prompt)
    if ability.shape == "unified_v2":
        return _build_unified_v2(ability, source, prompt, resolution)
    return _build_legacy(ability, source, count, extra, version or MIN_VERSION)


def _build_legacy(ability: Ability, source: dict, count: int,
                  extra: dict | None, version: str) -> str:
    comp_id = uid()
    postedit: dict[str, Any] = {
        "type": "", "id": uid(), "generate_type": ability.gen_type,
    }
    if "item_id" in source:
        postedit.update(source)
    else:
        # `origin_image` 是**对象**（image 节点），不是字符串。2026-11-19 用字符串
        # 提交被上游拒（ret=1000）；2026-11-20 换对象后 ret=0 且出图 2048×2048。
        # 注意这只对 normal_hd 验证过 —— detail_fix 带上传图仍会 generate_failed。
        postedit["origin_image"] = _image_node(
            source["image_uri"], int(source.get("width") or 0),
            int(source.get("height") or 0))
    if extra:
        postedit.update(extra)
    draft = {
        "type": "draft", "id": uid(), "min_version": version,
        "min_features": [], "is_from_tsn": True, "version": version,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "image_base_component", "id": comp_id,
            "min_version": "3.0.2", "aigc_mode": "workbench",
            "gen_type": ability.gen_type,
            "metadata": _meta(),
            "generate_type": ability.ability,
            "abilities": {
                "type": "", "id": uid(),
                ability.ability: {
                    "type": "", "id": uid(),
                    "postedit_param": postedit,
                },
                "gen_option": {
                    "type": "", "id": uid(),
                    "gen_count": max(1, int(count)), "generate_all": False,
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


def _build_prohd(ability: Ability, source: dict, resolution: str,
                 version: str) -> str:
    """智能超清：**有 core_param，没有 gen_option**，`origin_image` 是对象。"""
    if "item_id" in source:
        raise ValueError("智能超清只收上传图（origin_image 对象），不收作品引用")
    comp_id = uid()
    uri = source["image_uri"]
    draft = {
        "type": "draft", "id": uid(), "min_version": version,
        "min_features": list(ability.min_features),
        "is_from_tsn": True, "version": version,
        "main_component_id": comp_id,
        "component_list": [{
            "type": "image_base_component", "id": comp_id,
            "min_version": "3.0.2", "aigc_mode": "workbench",
            "metadata": _meta(),
            "generate_type": ability.ability,
            "abilities": {
                "type": "", "id": uid(),
                ability.ability: {
                    "type": "", "id": uid(),
                    "core_param": {
                        "type": "", "id": uid(),
                        "model": ability.model, "generate_type": 0,
                    },
                    "resolution_type": resolution,
                    "postedit_param": {
                        "type": "", "id": uid(),
                        "generate_type": ability.gen_type,
                        "origin_image": _image_node(
                            uri, source["width"], source["height"]),
                    },
                },
            },
        }],
    }
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------- 提交体
def submit_extend(ability: Ability, workspace_id: str = "") -> dict:
    """外层 `extend`。三种形态各不相同（抓包为准）：

    * pro_hd / unified：只有 `workspace_id`；
    * unified_v2：`root_model` **加** `workspace_id`；
    * legacy：只有 `root_model`（值为 abilities 里的键）。
    """
    if ability.shape in ("prohd", "unified", "unified_v2"):
        wid = str(workspace_id or "")
        body: dict[str, Any] = {}
        if ability.shape == "unified_v2":
            body["root_model"] = ability.model_req_key
        if wid.isdigit():
            body["workspace_id"] = int(wid)
        return body
    return {"root_model": ability.ability}


def _smart_edit_metrics(ability: Ability, *, submit_id: str, resolution: str,
                        image_uri: str) -> str:
    """智能编辑的 metrics 键集最长，且 `sceneOptions[0]` 结构与众不同。"""
    scene = {
        "type": "image", "modelReqKey": ability.model_req_key,
        "reportParams": {
            "enterSource": "generate",
            "extraVipFunctionKey": image_uri,
            "allowDuplicateShowAction": False,
            "reportVipFunctionDetailsOnKeyDownCapture": False,
            "skipReportVipFunctionDetailsOnClickCapture": False,
            "useVipFunctionDetailsReporterHoc": False,
        },
        "scene": ability.scene, "benefitCount": 1, "imageInputNum": 1,
        "resolutionType": resolution,
    }
    return json.dumps({
        "promptSource": "custom", "generateCount": 1,
        "toolItemId": ability.tool_item,
        "sceneOptions": json.dumps([scene], separators=(",", ":")),
        "enterFrom": "all_tools_click", "hasRejectedAudit": 0,
        # 标记计数：当前不支持标记，恒为 0（键必须存在，抓包如此）
        "markTagCnt": 0, "markBoxCnt": 0, "imageTextCnt": 0, "imageArrowCnt": 0,
        "generateId": submit_id, "isRegenerate": False,
        "tool_entry": {"modal_tool_key": ability.tool_key,
                       "item_code": ability.tool_item,
                       # 智能编辑在"精选"分类下，不是"图片"
                       "panel_key": "dreamina_all_tools_category_featured"},
    }, separators=(",", ":"))


def submit_metrics(ability: Ability, *, submit_id: str, resolution: str,
                   width: int, height: int, image_uri: str) -> str:
    """`metrics_extra`。pro_hd 与 legacy 的键集不同，照抓包分派。"""
    if ability.shape == "unified_v2":
        return _smart_edit_metrics(ability, submit_id=submit_id,
                                   resolution=resolution or "1.5k",
                                   image_uri=image_uri)
    if ability.shape == "unified":
        scene = {"type": "image", "scene": ability.scene}
        if ability.scene_benefit:
            scene["benefitCount"] = 1
        scene["reportParams"] = {"enterSource": "generate",
                                 "extraVipFunctionKey": image_uri}
        body = {
            "promptSource": "custom", "generateCount": 1,
            "toolItemId": ability.tool_item,
            "sceneOptions": json.dumps([scene], separators=(",", ":")),
            "enterFrom": "all_tools_click",
        }
        if ability.metrics_audit:
            body["hasRejectedAudit"] = 0
        body["generateId"] = submit_id
        body["isRegenerate"] = False
        body["tool_entry"] = {"modal_tool_key": ability.tool_key,
                              "item_code": ability.tool_item,
                              "panel_key": "dreamina_all_tools_category_image"}
        return json.dumps(body, separators=(",", ":"))
    if ability.shape != "prohd":
        return json.dumps(
            {"promptSource": "custom", "generateCount": 1},
            separators=(",", ":"))
    scene = {
        "type": "image", "scene": ability.scene,
        "proHDResolutionType": resolution, "modelName": ability.model,
        "postEditSourceSize": {"width": int(width), "height": int(height)},
        "reportParams": {"enterSource": "generate",
                         "extraVipFunctionKey": image_uri},
    }
    return json.dumps({
        "promptSource": "custom", "generateCount": 1,
        "toolItemId": PROHD_TOOL_ITEM,
        "sceneOptions": json.dumps([scene], separators=(",", ":")),
        "enterFrom": "all_tools_click", "hasRejectedAudit": 0,
        "generateId": submit_id, "isRegenerate": False,
        "tool_entry": {"modal_tool_key": "super_resolution",
                       "item_code": PROHD_TOOL_ITEM,
                       "panel_key": "dreamina_all_tools_category_featured"},
    }, separators=(",", ":"))


def draft_summary(draft_json: str) -> dict:
    """dry-run 用：把草稿还原成关键字段，便于断言与排障。

    三种形态都要能读：legacy / prohd 看 `postedit_param`，unified 看
    `unified_ability_list`。只认前者会让新能力的摘要整片 `null`。
    """
    j = json.loads(draft_json)
    comp = (j.get("component_list") or [{}])[0]
    abilities = comp.get("abilities") or {}
    key = next((k for k in abilities if k not in ("type", "id", "gen_option")), "")
    node = abilities.get(key) or {}
    if key == "unified_generate":
        return _unified_summary(j, comp, node)
    if key == "unified_generate_v2":
        return _unified_v2_summary(j, comp, node)
    return _legacy_summary(j, comp, abilities, node, key)


def _unified_v2_summary(j: dict, comp: dict, node: dict) -> dict:
    """v2 摘要：prompt/model 挂在能力节点上，标记在 extra 里，都不在 core_param。"""
    ual = (node.get("unified_ability_list") or [{}])[0]
    res = ual.get("resource_list") or []
    img = ((res[0] if res else {}).get("content") or {}).get("image") or {}
    snap: dict = {}
    try:
        snap = json.loads(ual.get("extra") or "{}")
    except ValueError:
        snap = {}
    restore = snap.get("restore_data") or {}
    return {
        "version": j.get("version"), "min_version": j.get("min_version"),
        "min_features": j.get("min_features"),
        "component_generate_type": comp.get("generate_type"),
        "ability": "unified_generate_v2",
        "ability_code": ual.get("ability_code"),
        "generate_type": node.get("generate_type"),
        "prompt": node.get("prompt"),
        "model": node.get("model"),
        "resolution": (node.get("render_config") or {}).get("resolution_type"),
        "gen_count": (node.get("gen_option") or {}).get("gen_count"),
        "resource": {"purpose": (res[0] if res else {}).get("purpose"),
                     "has_signed_url": bool(img.get("image_url")),
                     "width": img.get("width"), "height": img.get("height")},
        "elements": [{"type": e.get("type"), "z_index": e.get("z_index")}
                     for e in (restore.get("elements") or [])],
        "extra_schema": snap.get("schema"),
    }


def _unified_summary(j: dict, comp: dict, node: dict) -> dict:
    ual = (node.get("unified_ability_list") or [{}])[0]
    param = ual.get("ability_param") or {}
    res = param.get("resource_list") or []
    geo = ((param.get("option") or {}).get("out_painting") or None)
    return {
        "version": j.get("version"), "min_version": j.get("min_version"),
        "min_features": j.get("min_features"),
        "component_generate_type": comp.get("generate_type"),
        "ability": "unified_generate",
        "ability_code": ual.get("ability_code"),
        "generate_type": node.get("generate_type"),
        "core_param_keys": sorted((node.get("core_param") or {})),
        "prompt": (node.get("core_param") or {}).get("prompt"),
        "has_option": "option" in param,
        "resources": [{"purpose": r.get("purpose"),
                       "resource_type": r.get("resource_type"),
                       "kind": next((k for k in (r.get("content") or {})
                                     if k not in ("type", "id")), "")}
                      for r in res],
        "out_painting": ({k: v for k, v in geo.items() if k not in ("type", "id")}
                         if geo else None),
    }


def _legacy_summary(j: dict, comp: dict, abilities: dict,
                    node: dict, key: str) -> dict:
    param = node.get("postedit_param") or {}
    origin = param.get("origin_image")
    return {
        "version": j.get("version"), "min_version": j.get("min_version"),
        "min_features": j.get("min_features"),
        "gen_type": comp.get("gen_type"),
        "generate_type": comp.get("generate_type"),
        "ability": key,
        "core_param": node.get("core_param"),
        "resolution_type": node.get("resolution_type"),
        "postedit_generate_type": param.get("generate_type"),
        "origin_image_kind": ("object" if isinstance(origin, dict)
                              else "string" if isinstance(origin, str) else None),
        "origin_image_uri": (origin.get("image_uri") if isinstance(origin, dict)
                             else origin),
        "item_ref": bool(param.get("item_id")),
        "gen_count": (abilities.get("gen_option") or {}).get("gen_count"),
    }
