#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词增强（南向）：把一句短提示词扩写成结构化导演脚本。

**实测（2026-11-25）**：免费、异步两步（create → query），约 5–15 秒。
输入「一只猫在草地上奔跑」，输出带 `## 主体 / ## 场景 / ## 风格 / ## 音乐` 的完整脚本。

三个必须踩对的细节，错任何一个都返回 `ret=1000 invalid parameter`：

1. **字段全是 snake_case**：`pe_task_id` / `content_generator_type` / `input_snapshot`，
   不是 JS 源码里的 camelCase（源码经过属性名转换，外观会骗人）；
2. **`generation_params` 和 `rich_prompt_snapshot` 是 JSON 字符串**，不是对象；
3. **`prompt_context` 是 `"T2V"`**（大写），不是 `"video"`；`content_generator_type` 才是 `"video"`。

发现路径：`aria-label="优化提示词"` 的按钮在**视频面板**（不是图片面板），
点它抓包才拿到真实结构。UI 上找不到入口时，去 JS bundle 搜接口名反查调用点也行。
"""
from __future__ import annotations

import json
import time
import uuid

CREATE = "/mweb/v1/prompt_enhancement/create"
QUERY = "/mweb/v1/prompt_enhancement/query"

#: 提示词上下文类型 —— 上游枚举只有这三个（JS 里 `c={T2V,R2V,F2V}`）：
#:
#:   T2V  文生视频：纯提示词（**已实测**，19.5 秒返回导演脚本）
#:   R2V  参考生视频：带参考图，对应 UI 的「参考内容 / omniReference」
#:   F2V  帧生视频：首帧或首尾帧驱动
#:
#: ⚠️ 只支持**视频**。实测 `content_generator_type=image` 与 `audio` 均返回
#: `ret=1000`；图片模型的 `get_common_config` 里也没有 `support_prompt_enhancement`。
CONTEXTS = ("T2V", "R2V", "F2V")

#: 已实测可用的上下文（R2V/F2V 需要参考图/帧，尚未验证）。
CONTEXTS_VERIFIED = ("T2V",)

#: 轮询参数
POLL_TIMES = 30
POLL_INTERVAL = 2.0

#: 这些状态代表任务已结束
_FINAL = ("succeeded", "failed", "timeout")

#: 上游 query 接受的完整状态集。**必须精确匹配** —— 多传一个非法值
#: （例如 "cancelled"）会让整个查询返回 ret=1000，表现为"永远查不到结果"。
_STATUS_LIST = ["created", "running", "succeeded", "failed", "timeout"]


class EnhanceError(Exception):
    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


def _generation_params(*, ratio: str, resolution: str, duration_ms: int) -> str:
    """上游要的是**字符串化的 JSON**，不是对象。"""
    return json.dumps({"feature": "omniReference", "aspectRatio": ratio,
                       "resolution": resolution, "durationMs": int(duration_ms),
                       "fps": 24}, separators=(",", ":"))


def _rich_snapshot(prompt: str) -> str:
    return json.dumps({"feature": "omniReference",
                       "contents": [{"type": "text", "text": prompt}],
                       "references": [], "resources": []}, ensure_ascii=False,
                      separators=(",", ":"))


def build_create_body(*, prompt: str, model_req_key: str, context: str = "T2V",
                      ratio: str = "16:9", resolution: str = "720p",
                      duration_ms: int = 5000) -> dict:
    return {"items": [{
        "pe_task_id": f"video-pe-{uuid.uuid4()}",
        "content_generator_type": "video",
        "prompt_context": context,
        "input_snapshot": {
            "pe_client_input": {"user_prompt": prompt, "media_desc": ""},
            "model_req_key": model_req_key,
            "generation_params": _generation_params(
                ratio=ratio, resolution=resolution, duration_ms=duration_ms),
            "prompt_segments": [{"type": "text", "text": prompt}],
            "rich_prompt_snapshot": _rich_snapshot(prompt),
        },
    }]}


def enhance(client, *, prompt: str, model_req_key: str, context: str = "T2V",
            ratio: str = "16:9", resolution: str = "720p",
            duration_ms: int = 5000) -> dict:
    """返回 `{original, enhanced, pe_task_id}`；失败抛 `EnhanceError`。"""
    text = (prompt or "").strip()
    if not text:
        raise EnhanceError("prompt 不能为空")

    body = build_create_body(prompt=text, model_req_key=model_req_key,
                             context=context, ratio=ratio,
                             resolution=resolution, duration_ms=duration_ms)
    try:
        resp = client.post_json(CREATE, body)
    except Exception as exc:                      # noqa: BLE001
        raise EnhanceError(f"创建增强任务失败：{type(exc).__name__}") from exc

    results = ((resp.get("data") or {}).get("results")) or []
    pe_id = (results[0] or {}).get("pe_task_id") if results else None
    if not pe_id:
        raise EnhanceError("上游未返回任务 id")

    # create 有时会直接把结果带回来（status=succeeded），先看一次
    got = _extract(results[0])
    if got:
        return {"original": text, "enhanced": got, "pe_task_id": pe_id}

    qbody = {"offset": 0, "limit": 10,
             "list_filter": {"content_generator_type": "video",
                             "status_list": list(_STATUS_LIST)},
             "include_input_snapshot": False, "include_deleted": False}
    for _ in range(POLL_TIMES):
        time.sleep(POLL_INTERVAL)
        try:
            q = client.post_json(QUERY, qbody)
        except Exception as exc:                  # noqa: BLE001
            # 查询失败不能静默 —— 之前就是因为吞掉 ret=1000 才表现为"超时"
            raise EnhanceError(f"查询增强结果失败：{type(exc).__name__}") from exc
        for t in ((q.get("data") or {}).get("tasks") or []):
            if t.get("pe_task_id") != pe_id:
                continue
            status = t.get("status")
            if status == "succeeded":
                got = _extract(t)
                if not got:
                    raise EnhanceError("任务成功但没有 optimized_prompt")
                return {"original": text, "enhanced": got, "pe_task_id": pe_id}
            if status in _FINAL:
                raise EnhanceError(f"增强失败：{status}")
    raise EnhanceError("等待增强结果超时", timeout=True)


#: `target=image` 时保留的章节（视觉描述）。
IMAGE_KEEP = ("主体", "场景", "风格", "画面", "构图", "色彩", "光线", "光影",
              "氛围", "细节", "材质", "背景", "参考", "镜头")

#: `target=image` 时丢弃的章节（视频的时间/声音维度，静态图用不上）。
IMAGE_DROP = ("音乐", "情节", "声音", "台词", "对白", "音效", "旁白", "节奏",
              "运镜", "转场", "时长", "时间线", "动作过程")


def to_image_prompt(enhanced: str) -> str:
    """把视频导演脚本裁成图片提示词。

    上游只服务视频，输出里带「音乐 / 情节 / 声音」这些静态图用不上的维度。
    这里按 `## 标题` 分节，**白名单优先**（只留视觉相关），再兜底剔除黑名单，
    最后压成一段连续描述。

    没有 `##` 分节的输出（上游偶尔直出正文）原样返回 —— 宁可少裁，不要误删。
    """
    text = (enhanced or "").strip()
    if not text or "##" not in text:
        return text

    sections: list[tuple[str, list[str]]] = []
    cur_title, cur_lines = "", []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if cur_title or cur_lines:
                sections.append((cur_title, cur_lines))
            cur_title = stripped.lstrip("#").strip()
            cur_lines = []
        elif stripped:
            cur_lines.append(stripped)
    if cur_title or cur_lines:
        sections.append((cur_title, cur_lines))

    kept: list[str] = []
    for title, lines in sections:
        body = " ".join(lines).strip()
        if not body:
            continue
        if any(k in title for k in IMAGE_DROP):
            continue
        if title and not any(k in title for k in IMAGE_KEEP):
            continue                      # 有标题但不在白名单 → 跳过
        kept.append(body)

    return " ".join(kept).strip() or text


def _extract(task: dict | None) -> str:
    """兼容两种位置：create 的 results[0] 和 query 的 tasks[i]。"""
    if not isinstance(task, dict):
        return ""
    res = task.get("pe_result") or {}
    txt = res.get("optimized_prompt") or ""
    return txt if isinstance(txt, str) else ""
