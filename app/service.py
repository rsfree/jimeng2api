#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务编排：建任务、选号、提交、轮询、对账。

主线（与参考实现一致）：

    POST 只落库 → 协调器提交 → 协调器轮询 → 终态落库
    ↑ 请求内零上游往返     ↑ 唯一执行者，受并发闸门约束

为什么不在请求里直接提交：建任务是**计费动作**，需要重试、需要限并发、
需要在账号之间调度 —— 塞进请求线程会让这三件事都做不干净。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import time
import uuid
from typing import Sequence

import httpx

from . import db, pool as pool_mod, repo
from .config import settings
from .models import ModelSpec, Registry
from .upstream import video, postedit, protocol, tts, prompt_enhance, subject
from .upstream.errors import JimengError, JimengParamError

log = logging.getLogger("jimeng2api.service")

MAX_IMAGE_BYTES = 12 * 1024 * 1024


#: 上游 `model_req_key` → 对外模型 ID。
#:
#: 上游 key 形如 `high_aes_general_v55` / `jm_image_model_lib_f`，带 vendor
#: 前缀与内部版本号 —— 客户端不该看到它，否则等于把渠道的血缘关系摊开。
#: **只加不改**：改一个 ID 等于断掉一批已接入的客户端。
PUBLIC_MODEL_IDS: dict[str, str] = {
    "high_aes_general_v55": "seedream-5.5",
    "high_aes_general_v50p_large": "seedream-5.0-pro",
    "high_aes_general_v50_ultra": "seedream-5.0-aurora",
    "high_aes_general_v50_surge": "seedream-5.0-surge",
    "high_aes_general_v50_flash": "seedream-5.0-flash",
    "high_aes_general_v50": "seedream-5.0-lite",
    "high_aes_general_v43": "seedream-4.7",
    "high_aes_general_v42": "seedream-4.6",
    "high_aes_general_v41": "seedream-4.1",
    "high_aes_general_v40l": "seedream-4.5",
    "high_aes_general_v40": "seedream-4.0",
    "high_aes_general_v30l_art_fangzhou:general_v3.0_18b": "seedream-3.1",
    "high_aes_general_v30l:general_v3.0_18b": "seedream-3.0",
    "dreamina_crowd_test": "aura-5.5",
    "jimeng_smart_image_v1": "imagemax-1.0",
    "jm_image_model_lib_f": "image-2.5-flash",
    "jm_image_model_lib_s": "image-2.5-pro",
    "jm_image_model_oa_it": "image-2",
    "jm_image_model_gg_bp_21": "n-2.1",
    "jm_image_model_gg_bp": "n-pro",
    "jm_image_model_yc_mj82": "v8.2",
    "jm_image_model_anycook": "anycook-1.0",
}


def public_model_id(model: str) -> str:
    """上游 key → 对外 ID。**幂等**：已经是对外 ID / 后编辑 key 时原样返回。

    表里没有的新模型走 `m-<10 位 hash>` 兜底：宁可名字难看，也不能把一个
    上游 key 漏出去；也不能因为没登记就拒绝服务。
    """
    if not model:
        return ""
    if model in PUBLIC_MODEL_IDS:
        return PUBLIC_MODEL_IDS[model]
    if model in PUBLIC_MODEL_IDS.values() or postedit.is_postedit(model):
        return model
    return "m-" + hashlib.sha1(model.encode()).hexdigest()[:10]


# ------------------------------------------------------------------ 模型别名
def model_aliases(reg: Registry | None = None) -> dict[str, str]:
    """对外 ID / 上游 key / 面板名 / 常用别名 → 上游 key。

    面板与 OpenAI 风格接口共用同一份解析 —— 两套 alias 表迟早会分叉，
    然后同一句 `model: "seedream-5.5"` 在两边得到不同结果。

    上游 key 仍被接受（老客户端兼容），但它**不再出现在 `/v1/models` 里**：
    「能解析」与「对外公布」是两件事。
    """
    reg = reg or pool_mod.registry()
    out: dict[str, str] = {}
    for m in reg.models:
        out[public_model_id(m.key)] = m.key
        out[m.key] = m.key                 # 兼容旧调用，不对外公布
        slug = _slug(m.name)
        out[slug] = m.key
        out[slug.replace(".", "-")] = m.key
    default = repo.runtime_config()["default_model"]
    out.update({
        "default": default, "jimeng": default,
        "seedream-5.5": "high_aes_general_v55",
        "seedream-5.0-pro": "high_aes_general_v50p_large",
        "seedream-5.0-lite": "high_aes_general_v50",
        "seedream-5.0-flash": "high_aes_general_v50_flash",
        "seedream-4.7": "high_aes_general_v43",
        "seedream-4.6": "high_aes_general_v42",
        "seedream-4.5": "high_aes_general_v40l",
    })
    return {k: v for k, v in out.items() if v}


def _slug(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[\s_/]+", "-", s)
    return re.sub(r"[^a-z0-9.\-]", "", s)


# ------------------------------------------------------------ 模型重定向
#: 一条重定向链最多跟几跳。链式重定向是配置自由，不是运行时特性 ——
#: 超过 4 跳说明表已经绕成谜，报错比猜更安全。
REDIRECT_MAX_HOPS = 4

#: 对外名的字符集。允许 `/`（`google/imagen-4.0` 这类官方命名）与 `:`
#: （部分厂商用 `vendor:model`）；允许的字符都要能被 JSON 与 SQLite 原样保存。
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._:/@+\-]{0,127}$")


def normalize_alias(name: str) -> str:
    """对外名归一化：去空白 + 小写。解析与存储都用这一种形态。"""
    return (name or "").strip().lower()


def _follow_redirects(name: str, redirects: dict[str, str]) -> str:
    """沿映射走到终点；成环 / 超深抛参数错误（消息直接给运维看）。"""
    cur = (name or "").strip()
    if not cur:
        return ""
    seen = [cur.lower()]
    for _ in range(REDIRECT_MAX_HOPS):
        nxt = (redirects.get(cur.lower()) or "").strip()
        if not nxt:
            return cur
        low = nxt.lower()
        if low in seen:
            raise JimengParamError(
                "模型重定向成环：" + " → ".join(seen + [low]), kind="param")
        seen.append(low)
        cur = nxt
    raise JimengParamError(
        f"模型重定向超过 {REDIRECT_MAX_HOPS} 跳：" + " → ".join(seen),
        kind="param")


def resolve_redirect(name: str) -> str:
    """只走**用户配置**的重定向，不判断目标是否是已知模型。

    单独抽出来是给"能力分流"用的：`jimeng-cutout` 是同步能力，入口要先用
    归一后的名字判形态；等 `resolve_model_name` 才发现别名指向抠图，
    请求已经被当成 t2i 排进队列了。
    """
    return _follow_redirects(name, repo.redirect_map())


def _classify(final: str, aliases: dict[str, str]) -> tuple[str, str] | None:
    """终点名 → ("model", 上游 key) / ("postedit", 能力 id) / None。"""
    key = aliases.get((final or "").strip().lower())
    if key:
        return "model", key
    ability = postedit.get(final)
    if ability:
        return "postedit", ability.key
    return None


def resolve_model_name(name: str, reg: Registry | None = None) -> str:
    """对外名 → 上游 key（或后编辑能力 id）。

    顺序：**用户重定向优先**，其次内置别名表（对外 ID / 上游 key / 面板名 /
    slug）。用户重定向排前面，是因为它是运维显式写的意图；内置表只是默认值。
    目标失效时把"重定向到了哪"写进错误里 —— 否则调用方只看到"模型未知"，
    根本不知道是别名配错了还是自己写错了。
    """
    raw = (name or "").strip()
    if not raw:
        raise JimengParamError("模型不能为空", kind="param")
    reg = reg or pool_mod.registry()
    target = resolve_redirect(raw)
    hit = _classify(target, model_aliases(reg))
    if hit:
        return hit[1]
    if target.lower() != raw.lower():
        # **不回显 target**：运维可能把目标填成上游 key，而"上游 key 不出网关"
        # 是硬口径 —— 报错文案不能成为绕过它的通道。精确目标在面板可见。
        raise JimengParamError(
            f"模型 {raw!r} 的重定向目标已失效，请联系网关管理员", kind="param")
    raise JimengParamError(f"未知模型 {raw!r}", kind="param")


def echo_model_id(requested: str, canonical: str) -> str:
    """响应里的 `model` 回显什么。

    只有**用户配置的重定向**才回显原名 —— 客户端用它自己的官方名发请求，
    回 `seedream-5.5` 会让它以为别名没生效。内置别名（`default` / `jimeng`）
    与旧的上游 key 仍折成对外 ID：这两类在加这个功能之前就存在，改回显
    等于悄悄改了老客户端的响应契约。
    """
    raw = (requested or "").strip()
    if raw and raw.lower() in repo.redirect_map():
        return raw
    return public_model_id(canonical or raw)


def validate_redirect(alias: str, target: str, *, reg: Registry | None = None,
                      check_alias: bool = True) -> dict:
    """校验一条重定向并返回归一化结果。失败抛 `JimengParamError`。

    `check_alias=False` 供"只改目标/备注"的场景使用：别名一旦入库，
    即使后来能力表新增了同名模型，也不该因为改个备注就改不动。
    """
    a = normalize_alias(alias)
    t = (target or "").strip()
    if not a:
        raise JimengParamError("对外模型名不能为空", kind="param")
    if not _ALIAS_RE.match(a):
        raise JimengParamError(
            "对外模型名只允许字母、数字与 . _ : / @ + -（最长 128，不区分大小写）",
            kind="param")
    reg = reg or pool_mod.registry()
    aliases = model_aliases(reg)
    if check_alias and (a in aliases or postedit.is_postedit(a)):
        raise JimengParamError(
            f"{a!r} 已是内置模型名或工具名；重定向不能遮蔽内置行为，请换一个对外名",
            kind="param")
    if not t:
        raise JimengParamError("目标模型不能为空", kind="param")
    if len(t) > 200:
        raise JimengParamError("目标模型名过长（≤ 200）", kind="param")
    # 在现有表上叠加这条边再解析：既校验目标，也能发现"新边让链成环"
    probe = dict(repo.redirect_map())
    probe[a] = t
    final = _follow_redirects(a, probe)
    hit = _classify(final, aliases)
    if hit is None:
        raise JimengParamError(
            f"目标 {t!r} 不在当前能力表里；可先「重新读取能力表」再确认模型名",
            kind="param")
    kind, key = hit
    if kind == "postedit":
        ability = postedit.get(key)
        if ability and ability.experimental and not settings.postedit_experimental:
            raise JimengParamError(
                f"{ability.name} 当前未开放（experimental）", kind="param")
    return {"alias": a, "target": t, "kind": kind, "key": key,
            "public": public_model_id(key) if kind == "model" else key}


def redirect_views(reg: Registry | None = None) -> list[dict]:
    """面板视图：每条重定向的**真实落点**与可用性。

    解析失败不抛异常 —— 一条坏配置不能把整页打成 500，只能标红让人来修。
    """
    reg = reg or pool_mod.registry()
    aliases = model_aliases(reg)
    enabled_map = repo.redirect_map()
    out: list[dict] = []
    for row in repo.list_redirects():
        item = dict(row)
        probe = dict(enabled_map)
        probe[row["alias"]] = row["target"]   # 停用的行也按"启用后"解析
        try:
            final = _follow_redirects(row["alias"], probe)
            hit = _classify(final, aliases)
            if hit is None:
                raise JimengParamError(
                    f"目标 {row['target']!r} 不在当前能力表里", kind="param")
            kind, key = hit
            if kind == "postedit":
                ability = postedit.get(key)
                if (ability and ability.experimental
                        and not settings.postedit_experimental):
                    raise JimengParamError(
                        f"{ability.name} 当前未开放（experimental）", kind="param")
            item |= {
                "ok": True, "kind": kind, "key": key,
                "public": public_model_id(key) if kind == "model" else key,
                "resolved": final, "error": "",
            }
        except JimengParamError as e:
            item |= {"ok": False, "kind": "", "key": "", "public": "",
                     "resolved": "", "error": str(e)}
        out.append(item)
    return out


def redirect_view(rid: int, reg: Registry | None = None) -> dict:
    return next((r for r in redirect_views(reg) if r["id"] == rid), {})


def enabled_redirects(reg: Registry | None = None) -> list[dict]:
    """对外公布用的别名：启用且目标当前可用。"""
    return [r for r in redirect_views(reg) if r["enabled"] and r["ok"]]


def redirect_target_options(reg: Registry | None = None) -> list[dict]:
    """面板下拉：目标只列**现在真能提交**的模型与工具。"""
    reg = reg or pool_mod.registry()
    out = [{
        "value": public_model_id(m.key),
        "label": f"{m.name} · {public_model_id(m.key)}",
        "kind": "model",
    } for m in reg.models]
    out += [{
        "value": t["id"], "label": f"{t['name']} · {t['id']}", "kind": "postedit",
    } for t in postedit.public_models(
        include_experimental=settings.postedit_experimental)]
    return out


# ------------------------------------------------------------------ 入参解析
def resolve_request(*, registry: Registry, model: str, size: str,
                    count: int, resolution: str = "") -> dict:
    """把调用方参数吸附到上游能力表：模型、张数、分辨率桶、比例槽。

    任何"请求了 A、实际做 B"都进返回值 `degradations` —— 静默降级会让人
    按 A 的预期为 B 付钱。
    """
    if model:
        model = resolve_model_name(model, registry)
    spec = registry.get(model) if model else registry.default()
    if spec is None:
        raise JimengParamError(f"未知模型 {model!r}；可用：{list(registry.by_key)}",
                               kind="param")
    degradations: list[str] = []
    opts = spec.count_options()
    want = int(count or 1)
    final_count = want
    if opts and want not in opts:
        final_count = min(opts, key=lambda x: (abs(x - want), x))
        degradations.append(f"张数 {want} 不在该模型选项 {opts}，吸附为 {final_count}")
    try:
        width, height = protocol.parse_size(size)
    except ValueError as e:
        raise JimengParamError(str(e), kind="param") from e
    bucket = resolution or protocol.resolution_type_for_size(width, height)
    if spec.resolutions and bucket not in spec.resolutions:
        fallback = ("2k" if "2k" in spec.resolutions
                    else next(iter(spec.resolutions)))
        degradations.append(f"该模型无 {bucket} 档，改用 {fallback}")
        bucket = fallback
    # 比例槽吸附（比例不合法时上游会按最长边裁切，这里先算好再告知）
    ratio = protocol.ratio_for_size(width, height)
    exact = protocol.RATIOS[ratio]
    if abs(width / height - exact[0] / exact[1]) > 0.01:
        degradations.append(
            f"尺寸 {width}×{height} 比例不在 8 个槽内，按 {exact[0]}:{exact[1]} 提交")
    return {
        "spec": spec, "model": spec.key, "count": final_count,
        "width": width, "height": height, "resolution": bucket,
        "size": f"{width}x{height}", "ratio": ratio,
        "degradations": degradations,
    }


def estimate_credits(spec: ModelSpec, resolution: str, count: int,
                     mode: str) -> int | None:
    # 后编辑按"次"计价，common_config 里没有它的单价表 —— 宁可报未知，
    # 也不拿 t2i 的单价乘一遍（那会把预算做错）。
    if mode == "postedit":
        return None
    unit = spec.price(resolution, "i2i" if mode == "i2i" else "t2i")
    if unit is None:
        return None
    return int(unit) * max(1, count)


# ---------------------------------------------------------------- 输入图处理
def _decode_image(ref: str) -> tuple[bytes, str]:
    """把 `data:` / `http(s):` 引用变成字节。`tos-` 开头属于 URI 直传，不走这里。"""
    if ref.startswith("data:"):
        try:
            head, b64 = ref.split(",", 1)
            return base64.b64decode(b64, validate=True), head
        except (ValueError, binascii.Error) as e:
            raise JimengParamError("data URI 解码失败", kind="param") from e
    if ref.startswith(("http://", "https://")):
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as c:
                r = c.get(ref)
                r.raise_for_status()
                data = r.content
        except httpx.HTTPError as e:
            raise JimengParamError(f"拉取输入图失败：{e}", kind="param") from e
        if len(data) > MAX_IMAGE_BYTES:
            raise JimengParamError("输入图超过 12MB", kind="param")
        return data, r.headers.get("content-type", "")
    raise JimengParamError(
        "输入图需为 data URI、http(s) 链接或 tos image_uri", kind="param")


def prepare_image_uris(account_id: int, refs: Sequence[str]) -> list[str]:
    """本地图/外链 → `image_uri`。`tos-` 形态直接透传（零请求）。"""
    rt = pool_mod.pool.runtime(account_id)
    uris: list[str] = []
    for ref in refs:
        ref = (ref or "").strip()
        if not ref:
            continue
        if ref.startswith("tos-"):
            uris.append(ref)
            continue
        data, ctype = _decode_image(ref)
        if ctype and "image" not in ctype and not ref.startswith("data:image"):
            raise JimengParamError(f"输入不是图片（{ctype}）", kind="param")
        if not data:
            raise JimengParamError("输入图为空", kind="param")
        uris.append(rt.uploader.upload(data))
    if not uris:
        raise JimengParamError("没有可用的输入图", kind="param")
    return uris


# ---------------------------------------------------------------- 抠图
def cutout(*, api_key_id: int | None, image: str) -> dict:
    """抠图：上传 → 主体分割 → 直接交付透明底 PNG。

    **不走任务队列**：分割是同步接口（实测 < 2s），没有 submit_id 可轮询，
    也没有"排队"的意义。费用为 0（上传与 saliency_seg 都免费）。
    """
    ref = (image or "").strip()
    if not ref:
        raise JimengParamError("抠图需要一张输入图（image）", kind="param")
    if ref.startswith("item:"):
        raise JimengParamError(
            "抠图只收图片（data URI / http URL / tos uri），不收作品引用", kind="param")
    cfg = repo.runtime_config()
    cands = repo.dispatch_candidates(int(cfg["per_account"]),
                                     int(cfg["cooldown_risk"]))
    if not cands:
        cands = [a for a in repo.list_accounts() if a["enabled"]]
    if not cands:
        raise JimengError("账号池为空或全部不可用", kind="pool")
    account = cands[0]
    rt = pool_mod.pool.runtime(account["id"])
    if ref.startswith("tos-"):
        uri = ref
    else:
        data, ctype = _decode_image(ref)
        if ctype and "image" not in ctype and not ref.startswith("data:image"):
            raise JimengParamError(f"输入不是图片（{ctype}）", kind="param")
        if not data:
            raise JimengParamError("输入图为空", kind="param")
        uri = rt.uploader.upload(data)
    masks = rt.client.saliency_seg([uri])
    mask = (masks[0] or {}).get("mask") if masks else None
    url = (mask or {}).get("url") or ""
    if not url:
        raise JimengError("上游未返回可交付的抠图结果", kind="upstream")
    return {
        "model": "jimeng-cutout",
        "images": [{"url": url, "uri": mask.get("uri") or "",
                    "format": "png", "width": None, "height": None}],
        "credits": 0,
    }


# ---------------------------------------------------------------- 建任务
def _create_video_task(*, api_key_id: int | None, model: str, prompt: str,
                       resolution: str, duration: int | None,
                       ratio: str,
                       images: Sequence[str] | None = None) -> dict:
    """建视频任务。参数边界来自 `/mweb/v1/video_generate/get_common_config`。"""
    vm = video.get(model)
    if vm is None:
        raise JimengParamError(f"未知视频模型 {model!r}", kind="param")
    text = str(prompt or "").strip()
    if not text:
        raise JimengParamError("视频生成必须给 prompt", kind="param")

    degradations: list[str] = []
    # 时长：秒（UI 语义），落到 duration_ms。越界按边界吸附并在降级里说明
    sec = int(duration) if duration else (vm.durations[0] if vm.durations else 5)
    if sec * 1000 < video.DURATION_MIN_MS:
        degradations.append(f"时长最短 {video.DURATION_MIN_MS // 1000}s，已按最短处理")
        sec = video.DURATION_MIN_MS // 1000
    if sec * 1000 > video.DURATION_MAX_MS:
        degradations.append(f"时长最长 {video.DURATION_MAX_MS // 1000}s，已按最长处理")
        sec = video.DURATION_MAX_MS // 1000

    res = (resolution or "").strip().lower()
    if res not in vm.resolutions:
        if res:
            degradations.append(
                f"{vm.name} 无 {res} 档（可选 {list(vm.resolutions)}），"
                f"改用 {vm.resolutions[0]}")
        res = vm.resolutions[0]

    asp = (ratio or "").strip()
    if asp not in video.ASPECT_RATIOS:
        if asp:
            degradations.append(
                f"不支持比例 {asp}（可选 {list(video.ASPECT_RATIOS)}），改用 16:9")
        asp = "16:9"

    # 图生视频：0 张 = 文生视频，1 张 = 首帧，2 张 = 首帧 + 尾帧。
    # 超过 2 张直接拒 —— 多出来的那几张没有语义，静默丢掉会让用户以为生效了。
    frames = [x for x in (images or []) if x]
    if len(frames) > 2:
        raise JimengParamError(
            "视频最多两张图：第 1 张作首帧、第 2 张作尾帧（多传无意义）",
            kind="param")

    return repo.create_task(
        task_id=str(uuid.uuid4()), api_key_id=api_key_id, mode="video",
        model=vm.key, prompt=text, negative_prompt="", size="",
        width=0, height=0, resolution=res, count=1, seed=None,
        image_uris=frames, mask_uri="",
        degradations=degradations,
        extras={"video": {"duration_ms": sec * 1000, "aspect_ratio": asp,
                          "duration_s": sec}},
    )


def _create_postedit_task(*, api_key_id: int | None, model: str, prompt: str,
                          size: str, count: int, resolution: str,
                          images: list[str], mask: str | None,
                          ratio: str = "", scale: float | None = None,
                          markers: list[dict] | None = None,
                          extras: dict | None = None) -> dict:
    """后编辑建任务：**没有 prompt 也能跑**，但必须恰好一张输入图。

    这里只做形态校验与落库，真正的上传/提交在协调器里 —— 与 t2i/i2i 同一条
    主线（请求内零上游往返）。
    """
    ability = postedit.get(model)
    if ability is None:
        reason = postedit.unsupported_reason(model)
        if reason:
            # 拒绝理由含上游协议细节（entity_seg / unified_generate_v2…）：
            # 只进服务端日志，客户端得到的是"不支持 + 去哪看可用清单"。
            log.info("后编辑未开放：%s（%s）", model, reason)
            raise JimengParamError(
                f"{model} 当前不支持，可用清单见 GET /v1/models", kind="param")
        raise JimengParamError(
            f"{model} 不是可用模型，清单见 GET /v1/models", kind="param")
    if ability.shape == "segment":
        raise JimengParamError(
            f"{ability.name} 是同步分割能力，不建任务：请直接调用 "
            "POST /v1/images/generations（网关内部同步执行）", kind="param")
    if ability.experimental and not settings.postedit_experimental:
        log.info("后编辑未启用：%s（%s）", ability.key, ability.name)
        raise JimengParamError(f"{ability.name} 当前未开放", kind="param")
    if len(images) != 1:
        raise JimengParamError(
            "后编辑一次只处理一张图：`image` 放一个引用（多张请分多次调用）",
            kind="param")
    ref = (images[0] or "").strip()
    is_item = ref.startswith("item:")
    if ability.input == "item" and not is_item:
        raise JimengParamError(
            f"{ability.name} 只认账号已有作品："
            "image 传 `item:<item_id>:<history_id>`", kind="param")
    if ability.input == "uri" and is_item:
        raise JimengParamError(
            f"{ability.name} 只认上传图（data URI / http URL / tos uri），不收作品引用",
            kind="param")
    if is_item:
        parts = ref.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise JimengParamError(
                "item 引用需形如 `item:<item_id>:<history_id>`（两个都是纯数字）",
                kind="param")
    degradations: list[str] = []
    clean_markers: list[dict] = []
    if markers:
        if ability.shape != "unified_v2":
            degradations.append(f"{ability.name} 不使用 markers，已忽略")
        else:
            try:
                clean_markers = postedit.normalize_markers(markers)
            except ValueError as e:
                raise JimengParamError(f"markers 不合法：{e}", kind="param") from e
    if ability.shape == "unified_v2" and not str(prompt or "").strip():
        raise JimengParamError(
            f"{ability.name} 必须给 prompt（v2 用描述驱动整图编辑）", kind="param")
    if ability.needs_mask and not mask:
        raise JimengParamError(f"{ability.name} 必须给 mask（要重绘的区域）",
                               kind="param")
    if mask and not ability.needs_mask:
        # 收下、随任务保存，但**明确告知没生效** —— 静默丢弃会让调用方
        # 以为局部重绘生效了（image-adapter §0.1 第 4 条）。
        degradations.append(f"{ability.name} 不消费 mask，该字段未下发上游")
    if str(prompt or "").strip() and not ability.core_prompt:
        # 消除笔的 core_param 里没有 prompt 键（抓包如此）：收下落库便于追溯，
        # 但要说清楚它不影响结果，否则调用方会以为"让它变成 X"生效了。
        degradations.append(
            f"{ability.name} 不使用提示词，prompt 已忽略（未下发上游）")
    if int(count or 1) != 1:
        degradations.append(f"{ability.name} 恒出 1 张，请求的 {count} 张已忽略")
    if size and ability.key != "jimeng-outpaint":
        degradations.append(f"{ability.name} 的输出尺寸由上游决定，size={size} 已忽略")
    # 扩图的"比例 / 倍率"是互斥的两套模式（实测：选具体比例时倍率不参与）。
    # 这里只做归一与校验，几何计算交给 postedit.outpaint_geometry（纯函数）。
    out_ratio, out_scale = "", 1.5
    if ability.outpaint:
        req_ratio = (ratio or "").strip().lower() or "original"
        if req_ratio not in postedit.OUTPAINT_RATIOS:
            raise JimengParamError(
                f"{ability.name} 的比例 {ratio!r} 不可用，可选 "
                f"{[k for k in postedit.OUTPAINT_RATIOS if k != '1:1']}", kind="param")
        out_ratio = req_ratio
        if scale is not None:
            out_scale = float(scale)
            if out_scale not in postedit.OUTPAINT_SCALES:
                raise JimengParamError(
                    f"{ability.name} 的倍率 {scale} 不可用，可选 "
                    f"{list(postedit.OUTPAINT_SCALES)}", kind="param")
        if postedit.OUTPAINT_RATIOS[out_ratio][1] is not None and scale is not None:
            degradations.append(
                f"{ability.name} 选了具体比例 {out_ratio} 时倍率不参与（上游如此）")
    elif ratio or scale is not None:
        degradations.append(f"{ability.name} 不使用 ratio/scale，已忽略")

    # 档位只对 pro_hd 有意义（2k/4k/8k 同价，默认 2k）
    res = (resolution or "").strip().lower()
    if ability.resolutions:
        if res not in ability.resolutions:
            if res:
                degradations.append(
                    f"{ability.name} 无 {res} 档（可选 {list(ability.resolutions)}），改用 "
                    f"{ability.resolutions[0]}")
            res = ability.resolutions[0]
    else:
        res = ""

    task_id = str(uuid.uuid4())
    task = repo.create_task(
        task_id=task_id, api_key_id=api_key_id, mode="postedit",
        model=ability.key, prompt=str(prompt or ""), negative_prompt="",
        size=size or "", width=0, height=0, resolution=res, count=1,
        seed=None, image_uris=[ref], mask_uri=(mask or ""),
        extras={"ability": ability.ability, "gen_type": ability.gen_type,
                "ability_input": ability.input,
                "ratio": out_ratio, "scale": out_scale,
                "markers": clean_markers} | (extras or {}),
        degradations=degradations,
    )
    task["degradations"] = degradations
    return task


def create_task(*, api_key_id: int | None, model: str, prompt: str,
                size: str, count: int, resolution: str = "",
                negative_prompt: str = "", seed: int | None = None,
                images: Sequence[str] | None = None,
                mask: str | None = None,
                ratio: str = "", scale: float | None = None,
                markers: list[dict] | None = None,
                duration: int | None = None,
                extras: dict | None = None) -> dict:
    refs = [x for x in (images or []) if x]
    # 别名先归一：否则直接调本函数（脚本/测试）时，别名指向后编辑能力会被
    # 当成生成模型送进 resolve_request，报一个误导性的"未知模型"。
    model = resolve_redirect(model) if model else model
    # 视频是**第三套**草稿（video_base_component），参数集与图片/后编辑都不同，
    # 所以最先分流 —— 不能落到 resolve_request 里（那会报"未知模型"）。
    if video.is_video(model):
        return _create_video_task(
            api_key_id=api_key_id, model=model, prompt=prompt,
            resolution=resolution, duration=duration, ratio=ratio,
            images=refs)
    # 后编辑与 t2i/i2i 的草稿结构不同（无 core_param），先分流再解析模型
    if postedit.is_postedit(model):
        return _create_postedit_task(
            api_key_id=api_key_id, model=model, prompt=prompt, size=size,
            count=count, resolution=resolution, images=refs, mask=mask,
            ratio=ratio, scale=scale, markers=markers, extras=extras)
    reason = postedit.unsupported_reason(model)
    if reason:
        raise JimengParamError(f"{model} 暂不支持：{reason}", kind="param")
    reg = pool_mod.registry()
    resolved = resolve_request(registry=reg, model=model, size=size,
                               count=count, resolution=resolution)
    mode = "i2i" if refs else "t2i"
    if mode == "i2i" and not str(prompt or "").strip():
        raise JimengParamError("图生图必须给 prompt（描述要怎么改）", kind="param")
    if not str(prompt or "").strip():
        raise JimengParamError("prompt 不能为空", kind="param")
    task_id = str(uuid.uuid4())
    task = repo.create_task(
        task_id=task_id, api_key_id=api_key_id, mode=mode,
        model=resolved["model"], prompt=prompt,
        negative_prompt=negative_prompt, size=resolved["size"],
        width=resolved["width"], height=resolved["height"],
        resolution=resolved["resolution"], count=resolved["count"],
        seed=seed, image_uris=refs, mask_uri=(mask or ""),
        extras=extras,
        degradations=(
            resolved["degradations"]
            + ([f"{'图生图' if mode == 'i2i' else '文生图'} 不消费 mask，该字段未下发上游"]
               if mask else [])
        ),
    )
    task["degradations"] = task.get("degradations") or resolved["degradations"]
    return task


def wait_task(task_id: str, timeout: float) -> dict:
    """同步接口用：**只查本地库**，不碰上游。"""
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        task = repo.get_task(task_id)
        if task and task["status"] in ("succeeded", "failed"):
            return task
        time.sleep(0.4)
    return repo.get_task(task_id) or {}


# ---------------------------------------------------------------- 提交执行
def _mark_account_fault(account_id: int, err: JimengError) -> None:
    cfg = repo.runtime_config()
    if err.kind in ("auth", "quota"):
        repo.record_account_result(account_id, ok=False, error=str(err),
                                   kind=err.kind)
    elif err.kind == "rate":
        repo.set_cooldown(account_id, int(cfg["cooldown_rate"]), str(err))
    elif err.kind == "risk":
        repo.set_cooldown(account_id, int(cfg["cooldown_risk"]), str(err))


def submit_task(task: dict) -> None:
    """提交一条 queued/submitting 任务。**这里是唯一真正花钱的地方。**

    重试边界（重要）：
    * 上游**明确拒绝**（1015 / 额度 / 限流 / 风控）⇒ 任务肯定没建，换号重试安全；
    * 网络错误 / 超时 ⇒ **不知道建没建**，绝不重发 —— 宁可报"结果未知"，
      也不让同一份 prompt 被扣两次积分。
    """
    cfg = repo.runtime_config()
    task_id = task["id"]
    tried: set[int] = set()
    last_err: JimengError | None = None

    if not any(a["enabled"] for a in repo.list_accounts()):
        repo.mark_task_failed(task_id, "账号池为空或全部被禁用", "pool")
        return

    # `item:` 引用绑账号：作品属于哪个号，就只能用那个号提交。
    # 不锁的话多号池会随机打错号 —— 而「细节修复」用错是**计费失败**。
    pinned: int | None = None
    if task["mode"] == "postedit":
        first = ((task.get("image_uris") or [""])[0] or "").strip()
        if first.startswith("item:"):
            parts = first.split(":")
            if len(parts) == 3 and parts[1].isdigit():
                pinned = repo.account_for_item(parts[1])

    for _ in range(4):
        cands = [c for c in repo.dispatch_candidates(
            int(cfg["per_account"]), int(cfg["cooldown_risk"]))
            if c["id"] not in tried]
        if pinned is not None:
            if pinned in tried:
                break
            cands = [c for c in cands if c["id"] == pinned]
            if not cands:
                repo.mark_task_failed(
                    task_id,
                    f"该作品属于账号 #{pinned}，它当前不可用（禁用/冷却/在途已满）；"
                    "item 引用不能换号重试", "pool")
                return
        if not cands:
            break
        account = cands[0]
        tried.add(account["id"])
        db.execute("UPDATE tasks SET attempts = attempts + 1 WHERE id = ?",
                   (task_id,))
        try:
            uris = task["image_uris"]
            if task["mode"] == "i2i" and uris:
                uris = prepare_image_uris(account["id"], uris)
            elif task["mode"] == "postedit":
                uris, source_size = _prepare_postedit(account["id"], task)
                if source_size:
                    task.setdefault("extras", {})["source_size"] = source_size
                    # 落库：下次复用已上传的 tos uri 时 `_upload_one` 拿不到
                    # 尺寸，只能靠这里存下来的值，否则 width/height 退化成 0
                    db.execute("UPDATE tasks SET extras = ? WHERE id = ?",
                               (db.dumps(task["extras"]), task_id))
            _submit(account["id"], task, uris)
            repo.mark_task_running(
                task_id, account["id"],
                forecast=estimate_credits(
                    pool_mod.registry().get(task["model"]) or ModelSpec(
                        key=task["model"], name=task["model"]),
                    task["resolution"], task["count"], task["mode"]),
            )
            if uris != task["image_uris"]:
                db.execute("UPDATE tasks SET image_uris = ? WHERE id = ?",
                           (db.dumps(uris), task_id))
            return
        except JimengError as e:
            last_err = e
            _mark_account_fault(account["id"], e)
            if e.kind in ("auth", "quota", "rate", "risk"):
                continue  # 上游明确拒绝：任务未创建，换号安全
            repo.mark_task_failed(task_id, str(e), e.kind)
            return

    # 走到这里说明本轮没有提交成功：要么没号可用，要么全被明确拒绝
    msg = str(last_err) if last_err else "没有可用账号（全部冷却 / 禁用 / 失效）"
    attempts = int(db.scalar("SELECT attempts FROM tasks WHERE id = ?",
                             (task_id,), 0) or 0)
    if last_err is None:
        # 没有可用账号（冷却/在途满）：等，但只等到 10 分钟，避免任务永远排队
        age = db.now() - int(task["created_at"])
        if age > 600:
            repo.mark_task_failed(task_id, f"等待可用账号超时（{msg}）", "pool")
            return
        delay = 15
    else:
        if attempts >= 4 + int(cfg["max_retry"]):
            repo.mark_task_failed(task_id, msg, last_err.kind)
            return
        delay = min(60, 10 * max(1, attempts))
    repo.mark_task_queued_retry(task_id, f"{msg}；稍后重试")
    db.execute("UPDATE tasks SET next_poll_at = ? WHERE id = ?",
               (db.now() + delay, task_id))


def _upload_one(account_id: int, ref: str) -> tuple[str, int, int]:
    """单个引用 → `(image_uri, width, height)`。`tos-` 直传，其余走 STS 上传。"""
    ref = (ref or "").strip()
    if not ref:
        raise JimengParamError("空的图片引用", kind="param")
    if ref.startswith("tos-"):
        return ref, 0, 0
    data, ctype = _decode_image(ref)
    if ctype and "image" not in ctype and not ref.startswith("data:image"):
        raise JimengParamError(f"输入不是图片（{ctype}）", kind="param")
    if not data:
        raise JimengParamError("输入图为空", kind="param")
    uri = pool_mod.pool.runtime(account_id).uploader.upload(data)
    width, height = postedit.image_size(data)
    return uri, width, height


def _signed_url(account_id: int, uri: str, stored: dict) -> str:
    """取带签名的图片 URL（免费）。签名会过期，所以每次提交都要重取。"""
    cached = str(stored.get("url") or "")
    if cached:
        return cached
    try:
        info = pool_mod.pool.runtime(account_id).client.image_by_uri([uri]).get(uri) or {}
    except Exception:  # noqa: BLE001 —— 拿不到由调用方报"需要签名 URL"
        log.warning("取签名 URL 失败 uri=%s", uri, exc_info=True)
        return ""
    return str(info.get("image_url") or "")


def _prepare_postedit(account_id: int, task: dict) -> tuple[list[str], dict]:
    """后编辑的输入：作品引用直传，或本地/外链图上传成 uri。

    与 i2i 的区别是**要顺带读宽高** —— pro_hd 的 `origin_image` 与 unified 的
    `resource_list` 都带真实 `width/height`（抓包如此），拿不到就填 0，不编数字。
    """
    ability = postedit.get(task["model"])
    uris = [u for u in (task.get("image_uris") or []) if u]
    if not uris:
        raise JimengParamError("后编辑任务缺输入图", kind="param")
    ref = uris[0].strip()
    stored = (task.get("extras") or {}).get("source_size") or {}
    if ability is not None and ability.needs_mask:
        mask_ref = (task.get("mask_uri") or "").strip()
        if not mask_ref:
            raise JimengParamError(f"{ability.name} 缺 mask", kind="param")
        if ref.startswith("item:"):
            raise JimengParamError("需要 mask 的能力只收图片，不收作品引用",
                                   kind="param")
        o_uri, ow, oh = _upload_one(account_id, ref)
        m_uri, mw, mh = _upload_one(account_id, mask_ref)
        return [o_uri, m_uri], {"width": ow, "height": oh,
                                "mask_width": mw, "mask_height": mh}
    if ref.startswith("item:"):
        return [ref], {}
    uri, width, height = _upload_one(account_id, ref)
    if not width or not height:
        width = int(stored.get("width") or 0)
        height = int(stored.get("height") or 0)
    extra: dict = {"width": width, "height": height}
    if ability is not None and ability.shape == "unified_v2":
        # v2 的资源节点要**带签名的 URL**（不只是 uri）。这一步是免费只读，
        # 放在协调器里做，保持"请求内零上游往返"。
        url = _signed_url(account_id, uri, stored)
        if not url:
            raise JimengParamError(
                f"{ability.name} 需要图片的签名 URL，但上游未返回；请重试或换图",
                kind="upstream")
        extra["url"] = url
    return [uri], extra


def _submit(account_id: int, task: dict, uris: list[str]) -> str:
    """按任务形态选 t2i / i2i / postedit 提交。**这是唯一真正花钱的调用。**"""
    rt = pool_mod.pool.runtime(account_id)
    if task["mode"] == "postedit":
        ability = postedit.get(task["model"])
        if ability is None:
            raise JimengParamError(f"未知后编辑能力 {task['model']!r}", kind="param")
        ref = (uris or [""])[0]
        size = (task.get("extras") or {}).get("source_size") or {}
        if ability.shape == "unified_v2":
            source = postedit.build_smart_edit_source(
                origin_uri=uris[0], image_url=str(size.get("url") or ""),
                width=int(size.get("width") or 0),
                height=int(size.get("height") or 0),
                markers=(task.get("extras") or {}).get("markers") or [])
        elif ability.shape == "unified":
            extras = task.get("extras") or {}
            source = postedit.build_unified_source(
                origin_uri=uris[0], mask_uri=(uris[1] if len(uris) > 1 else ""),
                width=int(size.get("width") or 0),
                height=int(size.get("height") or 0),
                mask_width=int(size.get("mask_width") or 0),
                mask_height=int(size.get("mask_height") or 0),
                ratio=extras.get("ratio") or "original",
                scale=float(extras.get("scale") or 1.5))
        elif ref.startswith("item:"):
            _, item_id, history_id = ref.split(":", 2)
            source = postedit.build_source(item_id=int(item_id),
                                           origin_history_id=int(history_id))
        else:
            source = postedit.build_source(
                image_uri=ref, width=int(size.get("width") or 0),
                height=int(size.get("height") or 0))
        return rt.client.submit_postedit(
            ability=ability, source=source, submit_id=task["id"],
            resolution=task.get("resolution") or "2k", count=1,
            # prompt 必须显式传：漏了它上游就按"自由发挥"重绘/扩图，
            # 而且从预算和日志上都看不出来（局部重绘踩过一次）
            prompt=task.get("prompt") or "",
            extra=(task.get("extras") or {}).get("extra"),
            workspace_id=settings.postedit_workspace)
    if task["mode"] == "video":
        vm = video.get(task["model"])
        if vm is None:
            raise JimengParamError(f"未知视频模型 {task['model']!r}", kind="param")
        ex = (task.get("extras") or {}).get("video") or {}
        refs = [x for x in (task.get("image_uris") or []) if x]
        first = end = None
        if refs:
            # `tos-` 直传拿不到尺寸（上游 `image_by_uri` 也回 0），传 0 由上游自己读
            uri, w, h = _upload_one(account_id, refs[0])
            first = video.frame_node(uri, w, h)
        if len(refs) > 1:
            uri, w, h = _upload_one(account_id, refs[1])
            end = video.frame_node(uri, w, h)
        return rt.client.submit_video(
            model=vm, submit_id=task["id"], prompt=task["prompt"],
            duration_ms=int(ex.get("duration_ms") or 5000),
            resolution=task["resolution"] or vm.resolutions[0],
            aspect_ratio=ex.get("aspect_ratio") or "16:9",
            first_frame=first, end_frame=end)
    if task["mode"] == "i2i":
        return rt.client.submit_i2i(
            prompt=task["prompt"], image_uris=uris, model=task["model"],
            count=task["count"], width=task["width"], height=task["height"],
            submit_id=task["id"], resolution_type=task["resolution"])
    return rt.client.submit_t2i(
        prompt=task["prompt"], model=task["model"], count=task["count"],
        width=task["width"], height=task["height"], submit_id=task["id"],
        seed=task["seed"], negative_prompt=task["negative_prompt"],
        resolution_type=task["resolution"])


# ---------------------------------------------------------------- 轮询执行
def poll_once() -> None:
    cfg = repo.runtime_config()
    interval = float(cfg["poll_interval"])
    tasks = repo.due_tasks()
    if not tasks:
        return
    # 兜底：卡在 submitting 超过 2 分钟的任务（进程重启/提交线程崩溃）
    for row in repo.list_tasks(status="submitting", limit=20)["items"]:
        if db.now() - (row["updated_at"] or 0) > 120:
            repo.mark_task_failed(
                row["id"],
                "提交状态未知（进程中断）。上游**可能已创建并计费**，"
                "请先到即梦作品页确认，再决定是否重提", "unknown")

    by_account: dict[int, list[dict]] = {}
    for t in tasks:
        if not t["account_id"]:
            continue
        if db.now() - (t["started_at"] or 0) > int(cfg["task_timeout"]):
            repo.mark_task_failed(t["id"], "任务超时（上游未在预算内完成）",
                                  "timeout")
            continue
        by_account.setdefault(t["account_id"], []).append(t)

    for account_id, group in by_account.items():
        try:
            rt = pool_mod.pool.runtime(account_id)
            result = rt.client.fetch_many([t["id"] for t in group])
        except JimengError as e:
            if e.kind in ("auth", "quota"):
                repo.record_account_result(account_id, ok=False, error=str(e),
                                           kind=e.kind)
            elif e.kind in ("rate", "risk"):
                _mark_account_fault(account_id, e)
            for t in group:
                repo.update_task_poll(
                    t["id"], upstream_status=t["upstream_status"],
                    progress=t["progress"], next_poll_at=db.now() + 30)
            continue
        for t in group:
            st = result.get(t["id"]) or {}
            if st.get("ok"):
                images = st.get("images") or []
                repo.finish_task(t["id"], images=images,
                                 forecast=st.get("cost"),
                                 upstream_status=st.get("status"))
                # 记住产物归属：item 引用绑账号，后续要拿它锁定调度
                try:
                    repo.remember_assets(account_id, t["id"], images)
                except Exception:  # noqa: BLE001 —— 记账失败不该影响交付
                    log.warning("记录产物归属失败 task=%s", t["id"], exc_info=True)
                repo.record_account_result(account_id, ok=True)
                est = st.get("cost") or t.get("forecast_credits")
                if est:
                    repo.charge_key(t.get("api_key_id"), int(est))
                db.execute("UPDATE tasks SET charged_credits = COALESCE(charged_credits, ?) "
                           "WHERE id = ?", (None, t["id"]))
            elif st.get("failed"):
                msg = st.get("fail_message") or "上游生成失败"
                repo.mark_task_failed(t["id"], msg, "upstream")
                repo.record_account_result(account_id, ok=False, error=msg)
            else:
                repo.update_task_poll(
                    t["id"], upstream_status=st.get("status"),
                    progress=st.get("progress") or "",
                    next_poll_at=db.now() + max(1, int(interval)))


# ---------------------------------------------------------------- 对账/刷新
def reconcile(task_id: str) -> dict:
    """按 submit_id 在消耗记录里找真实扣费。

    上游消耗记录**延迟结算**：跑完立刻查可能还没有那条记录 ⇒
    `found=false` 不等于免费，面板必须这样写。
    """
    task = repo.get_task(task_id)
    if not task or not task.get("account_id"):
        return {"found": False, "reason": "任务没有绑定账号"}
    try:
        rt = pool_mod.pool.runtime(task["account_id"])
        # 每页最多 20 条（再大就 1000），最多翻 3 页 = 60 条
        hit = None
        cursor = "0"
        for _ in range(3):
            data = rt.client.credit_history(count=20, history_type=2,
                                            cursor=cursor)
            records = data.get("records") or []
            hit = next((r for r in records
                        if str(r.get("submit_id")) == task_id), None)
            if hit or not data.get("has_more"):
                break
            cursor = str(data.get("new_cursor") or "")
            if not cursor:
                break
    except JimengError as e:
        return {"found": False, "reason": str(e)}
    if not hit:
        # 免费档（会员 Lite 实测 0 积分）本来就不会产生消耗记录 —— 别把
        # "没有记录"说成"没结算"，两者要分开讲。
        return {"found": False,
                "reason": "最近 60 条消耗记录里没有这个 submit_id：可能尚未结算，"
                          "也可能本次为 0 积分（免费档）"}
    amount = hit.get("amount")
    try:
        amount_i = int(amount)
    except (TypeError, ValueError):
        amount_i = None
    old = task.get("charged_credits")
    repo.set_charged(task_id, amount_i)
    if amount_i is not None:
        base = old if old is not None else (task.get("forecast_credits") or 0)
        repo.charge_key(task.get("api_key_id"), amount_i - int(base))
    return {"found": True, "amount": amount, "record": hit}


def refresh_task_images(task_id: str) -> dict:
    task = repo.get_task(task_id)
    if not task:
        return {"ok": False, "error": "任务不存在"}
    uris = [img.get("uri") for img in task["images"] if img.get("uri")]
    if not uris or not task.get("account_id"):
        return {"ok": False, "error": "没有可刷新的 image_uri"}
    try:
        rt = pool_mod.pool.runtime(task["account_id"])
        mapping = rt.client.image_by_uri(uris)
    except JimengError as e:
        return {"ok": False, "error": str(e)}
    for img in task["images"]:
        item = mapping.get(img.get("uri") or "")
        if item and item.get("image_url"):
            img["url"] = item["image_url"]
    db.execute("UPDATE tasks SET images = ? WHERE id = ?",
               (db.dumps(task["images"]), task_id))
    return {"ok": True, "images": task["images"]}




# ---------------------------------------------------------------- 语音合成
def speech(*, account_id: int, model: str, text: str, voice_id: str,
           fmt: str, sample_rate: int, speed: float) -> tts.Audio:
    """同步 TTS：一次上游调用，无任务、无轮询、不计积分。

    与图片族共用账号池与连接池，但**不落 tasks 表** —— 没有 submit_id 可对账。
    失败由 `tts` 归一到 `TtsUpstreamError`，路由层再映射为 502/504。
    """
    rt = pool_mod.pool.runtime(int(account_id))
    return tts.synthesize(rt.client, text=text, voice_id=voice_id, fmt=fmt,
                          sample_rate=int(sample_rate), speed=float(speed))


# ---------------------------------------------------------------- 提示词增强
def enhance_prompt(*, account_id: int, prompt: str, model_req_key: str,
                   context: str = "T2V", ratio: str = "16:9",
                   resolution: str = "720p", duration_ms: int = 5000) -> dict:
    """短提示词 → 结构化脚本。免费，不走任务表（没有 submit_id 可对账）。"""
    rt = pool_mod.pool.runtime(int(account_id))
    return prompt_enhance.enhance(rt.client, prompt=prompt,
                                  model_req_key=model_req_key, context=context,
                                  ratio=ratio, resolution=resolution,
                                  duration_ms=duration_ms)


# ---------------------------------------------------------------- 主体（只读）
def list_subjects(*, account_id: int, cursor: int = 0, limit: int = 20,
                  source: str = "mine") -> dict:
    """列出账号下的主体（角色/商品）。只读、免费，不走任务表。"""
    subject.validate_args(cursor=cursor, limit=limit, source=source)
    rt = pool_mod.pool.runtime(int(account_id))
    return subject.list_subjects(rt.client, cursor=cursor, limit=limit, source=source)
