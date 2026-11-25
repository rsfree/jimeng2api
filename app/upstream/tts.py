#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 TTS 南向适配器：即梦 `/mweb/v1/tts_generate` → 中立音频结果。

**能力矩阵在这里，不散落在分支里。** 每一条都对应 2026-11-25 的真实实测，
没有实测过的值不进这张表（见 `docs/UPSTREAM-TTS.md`）。

三个刻意的取舍：

* **只声明实测可用的编码**：`ogg`/`aac` 上游直接 `ret=1000`，不对外承诺。
* **语速两套坐标**：北向 `speed` 是倍率，上游 `speech_rate` 是百分比 `(speed-1)*100`。
  交集才是可接受区间，超出就 400 —— 钳位后返回 200 属于静默降级。
* **产物头必须校验**：上游返回的字节与声明格式不一致时抛错，不改扩展名冒充。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 与图片共用同一个域名与鉴权，只是路径不同。
PATH_TTS = "/mweb/v1/tts_generate"

#: 上游原生模型名。北向名 → 原生名的映射见 `MODEL_ALIASES`。
NATIVE_MODEL = "seed_audio"

#: 北向 `model` 可接受的值（含原生名）。**未命中一律 400，不静默换默认。**
MODEL_ALIASES: dict[str, str] = {
    "seed_audio": NATIVE_MODEL,
    "seed-audio": NATIVE_MODEL,
    # OpenAI 生态里最常见的两个名字，语义等价于本部署的默认合成模型
    "tts-1": NATIVE_MODEL,
    "tts-1-hd": NATIVE_MODEL,
}

#: 内置音色（保底）。**只放实测能出声的。**
_BUILTIN_VOICES: dict[str, str] = {
    "huopo": "7366933865698431515",       # 活泼女声
    "lively-female": "7366933865698431515",
}

#: 外部音色表：`data/voices.json`。存在则合并覆盖内置项。
#: 为什么单独放文件：音色 ID 只能靠 UI 逐个试听采集，属运营数据而非代码；
#: 放文件里可以在不重启代码库的前提下补齐（重启进程即生效）。
VOICES_FILE = Path(__file__).resolve().parents[2] / "data" / "voices.json"


def load_voices() -> dict[str, str]:
    """内置 + 外部文件合并。外部的键一律小写，值必须是纯数字 ID。"""
    merged = dict(_BUILTIN_VOICES)
    try:
        raw = json.loads(VOICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return merged
    if isinstance(raw, dict):
        for name, vid in raw.items():
            if isinstance(name, str) and isinstance(vid, str) and vid.isdigit():
                merged[name.strip().lower()] = vid
    return merged


#: 进程内生效的音色表。改完 `data/voices.json` 调 `reload_voices()` 或重启。
VOICES: dict[str, str] = load_voices()

#: 音色元数据（性别/年龄/口音/风格/场景）。由 `scripts/build_voice_meta.py` 生成，
#: 上游 feed 里 description/tags 全空，只能按名字推 —— 属运营数据，可手工校正。
VOICE_META_FILE = Path(__file__).resolve().parents[2] / "data" / "voice_meta.json"

#: 元数据缺失时的兜底，保证每个音色都有完整字段（筛选不会漏）。
_META_DEFAULT = {
    "gender": "unknown", "age": "adult", "accent": "standard",
    "style": ["通用"], "category": "general", "use_case": ["通用"],
}

#: 允许的筛选维度取值，供路由层校验。
META_DIMENSIONS = ("gender", "age", "accent", "category", "style")


def load_voice_meta() -> dict[str, dict]:
    try:
        raw = json.loads(VOICE_META_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def list_voices(*, gender: str = "", age: str = "", accent: str = "",
                category: str = "", style: str = "", q: str = "") -> list[dict]:
    """按维度筛选音色。空条件表示不筛；`style` 命中数组任一元素即可。"""
    meta = load_voice_meta()
    out: list[dict] = []
    for name, vid in VOICES.items():
        m = dict(_META_DEFAULT)
        m.update(meta.get(name) or {})
        item = {"name": name, "id": vid, **m}
        if gender and item["gender"] != gender:
            continue
        if age and item["age"] != age:
            continue
        if accent and item["accent"] != accent:
            continue
        if category and item["category"] != category:
            continue
        if style and style not in (item.get("style") or []):
            continue
        if q and q not in name:
            continue
        out.append(item)
    out.sort(key=lambda x: (x["category"], x["name"]))
    return out


def voice_facets() -> dict:
    """各维度的取值与数量，给客户端做筛选器。"""
    meta = load_voice_meta()
    facets: dict[str, dict[str, int]] = {d: {} for d in META_DIMENSIONS}
    for name in VOICES:
        m = dict(_META_DEFAULT)
        m.update(meta.get(name) or {})
        for dim in ("gender", "age", "accent", "category"):
            k = m.get(dim) or "unknown"
            facets[dim][k] = facets[dim].get(k, 0) + 1
        for st in (m.get("style") or ["通用"]):
            facets["style"][st] = facets["style"].get(st, 0) + 1
    return {d: dict(sorted(v.items(), key=lambda kv: -kv[1])) for d, v in facets.items()}


def reload_voices() -> dict[str, str]:
    global VOICES
    VOICES = load_voices()
    return VOICES


DEFAULT_VOICE = "huopo"

#: 北向编码 → (上游 format, 响应媒体类型)。
#: `pcm` 是裸 PCM（无容器），按规范用 octet-stream。
FORMATS: dict[str, tuple[str, str]] = {
    "mp3": ("mp3", "audio/mpeg"),
    "wav": ("wav", "audio/wav"),
    "pcm": ("pcm", "application/octet-stream"),
}
DEFAULT_FORMAT = "mp3"

#: 实测全部生效的采样率（WAV 头逐档验证）。
SAMPLE_RATES = (8000, 16000, 24000, 32000, 44100, 48000)
DEFAULT_SAMPLE_RATE = 24000

#: 北向 `speed` 的可接受区间。由上游 `speech_rate ∈ [-50, 100]` 反推：
#: speed = 1 + rate/100 → [0.5, 2.0]。超出即 400，不钳位。
SPEED_MIN = 0.5
SPEED_MAX = 2.0

#: 单次文本上限。实测 1500 字以上会不稳定（1520 失败 / 1540 成功），
#: 取 1000 留足余量；超长由调用方切段。
MAX_CHARS = 1000

#: 上游在长文本/音色非法时返回的业务错误码（区别于参数错误的 1000）。
RET_BUSINESS = 3010


@dataclass
class Audio:
    """适配结果：字节 + 真实媒体类型 + 扩展名。三者一起走，不拆开。"""

    content: bytes
    media_type: str
    extension: str


class TtsParamError(Exception):
    """北向参数错误 —— 带 `param`，路由层据此回 400。"""

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param


class TtsUpstreamError(Exception):
    """上游失败 —— 路由层按类型映射为 429/502/504，对外不暴露 ret。

    三种类型必须分开，因为调用方的处置完全不同（skill §7）：
    限流可以退避重试，超时结果未知，其它失败直接报错。
    """

    def __init__(self, message: str, *, timeout: bool = False,
                 rate: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.rate = rate


# ------------------------------------------------------------------ 解析
def resolve_model(name: str) -> str:
    key = (name or "").strip()
    if not key:
        raise TtsParamError("model 不能为空", param="model")
    hit = MODEL_ALIASES.get(key.lower())
    if not hit:
        raise TtsParamError(
            f"不支持的 model {key!r}，可用：{sorted(MODEL_ALIASES)}", param="model")
    return hit


def resolve_voice(name: str) -> str:
    """别名 → ID；纯数字 ID 直传；其余 400。"""
    key = (name or "").strip()
    if not key:
        raise TtsParamError("voice 不能为空", param="voice")
    hit = VOICES.get(key.lower())
    if hit:
        return hit
    # 原生 ID 直传：上游音色 ID 是 16-20 位纯数字
    if key.isdigit() and 10 <= len(key) <= 20:
        return key
    raise TtsParamError(
        f"不支持的 voice {key!r}，可用别名：{sorted(VOICES)}，或传上游数字音色 ID",
        param="voice")


def resolve_format(name: str) -> tuple[str, str]:
    key = (name or "").strip().lower() or DEFAULT_FORMAT
    hit = FORMATS.get(key)
    if not hit:
        raise TtsParamError(
            f"不支持的 response_format {key!r}，可用：{sorted(FORMATS)}",
            param="response_format")
    return hit


def speed_to_rate(speed: Any) -> int:
    """倍率 → 上游百分比。越界 400，不做钳位。"""
    try:
        val = float(speed)
    except (TypeError, ValueError):
        raise TtsParamError("speed 必须是数字", param="speed") from None
    if val != val or val in (float("inf"), float("-inf")):  # NaN / inf
        raise TtsParamError("speed 必须是有限数字", param="speed")
    if val < SPEED_MIN or val > SPEED_MAX:
        raise TtsParamError(
            f"speed 需在 {SPEED_MIN}–{SPEED_MAX} 之间（本部署声明区间）", param="speed")
    rate = round((val - 1.0) * 100)
    # 浮点误差兜底：0.7 可能算出 -30.000000000000004
    if rate < -50 or rate > 100:
        raise TtsParamError("speed 超出本部署支持区间", param="speed")
    return int(rate)


def check_text(text: str) -> str:
    val = text if isinstance(text, str) else ""
    if not val.strip():
        raise TtsParamError("input 不能为空", param="input")
    n = len(val)
    if n > MAX_CHARS:
        raise TtsParamError(
            f"input 长度 {n} 超过本部署上限 {MAX_CHARS} 字符，请自行切段", param="input")
    # 上游对「纯空白/纯标点」返回 ret=1000；本地先挡掉，文案更准确
    if not any(ch.isalnum() for ch in val):
        raise TtsParamError("input 不含任何字母、数字或文字", param="input")
    return val


# ------------------------------------------------------------------ 产物校验
def _looks_like(fmt: str, data: bytes) -> bool:
    """按真实字节头判断编码，防止把 WAV 改名成 MP3 交付。"""
    if not data:
        return False
    if fmt == "wav":
        return data[:4] == b"RIFF"
    if fmt == "mp3":
        return data[:3] == b"ID3" or (
            len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
    if fmt == "pcm":
        # 裸 PCM 无头，只要不是其它容器就算合理
        return data[:4] not in (b"RIFF", b"OggS", b"fLaC") and data[:3] != b"ID3"
    return True


# ------------------------------------------------------------------ 调用
def synthesize(client, *, text: str, voice_id: str, fmt: str,
               sample_rate: int = DEFAULT_SAMPLE_RATE,
               speed: float = 1.0) -> Audio:
    """调上游并返回 `Audio`。**同步**，无 submit_id、无轮询。

    `client` 是 `app.upstream.client.JimengClient`（复用其鉴权、sign 与连接池）。
    """
    vendor_fmt, media_type = resolve_format(fmt)
    rate = speed_to_rate(speed)
    body = {
        "text": text,
        "id_info": {"id": voice_id, "item_platform": 4},
        "audio_config": {
            "format": vendor_fmt,
            "pitch_rate": 0,          # 实测只接受 0，不对外暴露
            "sample_rate": int(sample_rate),
            "speech_rate": rate,
        },
    }
    try:
        payload = client.post_json(PATH_TTS, body)
    except Exception as exc:                      # noqa: BLE001 - 归一后再抛
        raise _normalize(exc) from exc

    data = (payload.get("data") or {}).get("data") or ""
    if not data:
        raise TtsUpstreamError("上游未返回音频数据")
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception as exc:                      # noqa: BLE001
        raise TtsUpstreamError("上游返回的音频不是合法 base64") from exc
    if not raw:
        raise TtsUpstreamError("上游返回空音频")
    if not _looks_like(vendor_fmt, raw):
        raise TtsUpstreamError(
            f"上游返回的字节与声明的 {vendor_fmt} 不一致，拒绝改名交付")
    return Audio(content=raw, media_type=media_type, extension=vendor_fmt)


def _normalize(exc: Exception) -> Exception:
    """把 client 的异常收敛成适配器自己的两类。"""
    kind = getattr(exc, "kind", "")
    ret = getattr(exc, "ret", None)
    if kind == "timeout":
        return TtsUpstreamError("上游超时", timeout=True)
    if kind == "rate":
        return TtsUpstreamError("上游限流", rate=True)
    if ret == RET_BUSINESS or kind == "param":
        return TtsUpstreamError("上游拒绝合成（参数或音色不被接受）")
    return TtsUpstreamError("上游合成失败")


# ------------------------------------------------------------------ 能力矩阵
def capabilities() -> dict:
    """交付文档与 `/v1/audio/speech` 的自描述共用这一份。"""
    return {
        "models": sorted(MODEL_ALIASES),
        "voices": sorted(VOICES),
        "default_voice": DEFAULT_VOICE,
        "formats": sorted(FORMATS),
        "default_format": DEFAULT_FORMAT,
        "sample_rates": list(SAMPLE_RATES),
        "speed": {"min": SPEED_MIN, "max": SPEED_MAX, "default": 1.0},
        "max_chars": MAX_CHARS,
        "pitch": "fixed-0",
        "instructions": False,
        "stream": False,
    }
