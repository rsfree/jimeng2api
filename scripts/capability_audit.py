#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力自检：**零请求**，只验草稿结构是否还符合取证。

    uv run python scripts/capability_audit.py            # 全部能力
    uv run python scripts/capability_audit.py --only 消除笔
    uv run python scripts/capability_audit.py --verbose

为什么要有它：每个能力的契约来自不同时间的抓包（`docs/evidence/`），
而代码在持续演进。测试能防回归，但它跑在 CI 里、失败信息面向开发者；
这个脚本面向运维，一眼看「哪个能力偏离了取证、偏在哪个字段」。

三条检查：
1. **可构造**：占位输入能否走通 build_submit_body（形态/必填/枚举）；
2. **对拍**：有抓包样本的能力，键集必须与样本一致（值不比，因为占位不同）；
3. **不变量**：那些"键存在与否本身就是契约"的点（消除笔无 prompt 键等）。

它不做网络、不读数据库、不计费。任何需要账号的动作都不在这里。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.upstream import postedit  # noqa: E402

EVIDENCE_DIR = Path("docs/evidence")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: 能力 → (证据文件, 构造参数)。证据为 None 表示"只有作品引用样本，无法对拍上传路径"。
CASES: dict[str, tuple[str | None, dict]] = {
    "jimeng-pro-hd": ("2026-11-24-pro-hd-4k-generate.json",
                      {"source": "uri", "resolution": "2k"}),
    # 2026-11-20 起支持上传图（origin_image 用对象形态），所以按 uri 检查
    "jimeng-hd": (None, {"source": "uri"}),
    "jimeng-detail-fix": (None, {"source": "item"}),
    "jimeng-inpaint": ("2026-11-19-inpaint-generate.json",
                       {"source": "mask", "prompt": "换个天空"}),
    "jimeng-erase": ("2026-11-19-erase-generate.json",
                     {"source": "mask", "prompt": ""}),
    "jimeng-smart-edit": ("2026-11-19-smart-edit-generate.json",
                          {"source": "signed", "prompt": "改成夜景",
                           "resolution": "1.5k",
                           "markers": [{"type": "brush",
                                        "points": [[0.4, 0.6], [0.5, 0.7]]}]}),
    "jimeng-outpaint": ("2026-11-19-outpaint-generate.json",
                        {"source": "uri", "prompt": "扩展成森林", "ratio": "16:9"}),
    # 分割能力没有可提交的草稿
    "jimeng-cutout": (None, {"source": "none"}),
}

#: 那些"键存在与否就是契约"的点。写成 (能力, 检查函数, 说明)
INVARIANTS = [
    ("jimeng-erase",
     lambda d: "prompt" not in _ug(d)["core_param"],
     "core_param 无 prompt 键（消除笔不吃提示词）"),
    ("jimeng-erase",
     lambda d: "option" not in _ug(d)["unified_ability_list"][0]["ability_param"],
     "ability_param 无 option 键"),
    ("jimeng-inpaint",
     lambda d: "prompt" in _ug(d)["core_param"],
     "core_param 有 prompt 键（局部重绘吃提示词）"),
    ("jimeng-outpaint",
     lambda d: "out_painting" in _ug(d)["unified_ability_list"][0]["ability_param"]["option"],
     "option 里有 out_painting（扩图几何）"),
    ("jimeng-smart-edit",
     lambda d: "core_param" not in _ug(d, "unified_generate_v2"),
     "能力节点无 core_param（v2 的 prompt 挂在自己身上）"),
    ("jimeng-smart-edit",
     lambda d: isinstance(d["component_list"][0]["abilities"]
                          ["unified_generate_v2"]["unified_ability_list"][0]["extra"], str),
     "extra 是 JSON 字符串而不是对象"),
]


def _ug(draft: dict, key: str = "unified_generate") -> dict:
    return draft["component_list"][0]["abilities"][key]


def _shape(o, path="", out=None):
    """压平成 {路径: 归一化值}，用于键集比较。"""
    out = [] if out is None else out
    if isinstance(o, dict):
        for k in sorted(o):
            _shape(o[k], f"{path}.{k}", out)
    elif isinstance(o, list):
        out.append((path + "[]", f"list[{len(o)}]"))
        for i in o:
            _shape(i, path + "[]", out)
    else:
        if isinstance(o, str) and UUID_RE.match(o):
            v = "<uuid>"
        elif path.endswith("created_time_in_ms") or path.endswith(("created_at", "updated_at")):
            v = "<ts>"
        else:
            v = o
        out.append((path, v))
    return out


def _make_source(ability: postedit.Ability, spec: dict):
    kind = spec.get("source")
    if kind == "none":
        return None
    if kind == "item":
        return postedit.build_source(item_id=7696399061636910361,
                                     origin_history_id=47490120999948)
    # 形态决定来源：prohd 与 legacy 用 build_source，unified 族各有自己的构造函数。
    # 混用会得到 KeyError（origin_image vs origin），这条分派本身就是个回归点。
    if ability.shape == "unified_v2":
        return postedit.build_smart_edit_source(
            origin_uri="tos-audit-o", image_url="https://cdn/audit.png",
            width=1024, height=1024, markers=spec.get("markers"))
    if ability.shape == "unified":
        return postedit.build_unified_source(
            origin_uri="tos-audit-o",
            mask_uri=("tos-audit-m" if kind == "mask" else ""),
            width=1024, height=1024, ratio=spec.get("ratio") or "original")
    return postedit.build_source(image_uri="tos-audit-o", width=1024, height=1024)


def audit_one(key: str, *, verbose: bool) -> tuple[bool, list[str]]:
    ability = postedit.get(key)
    notes: list[str] = []
    if ability is None:
        return False, [f"能力表里没有 {key}"]
    evidence, spec = CASES.get(key, (None, {"source": "uri"}))

    if ability.shape == "segment":
        notes.append("同步分割能力，没有可提交的草稿（跳过）")
        return True, notes

    try:
        source = _make_source(ability, spec)
        body = postedit.build_submit_body(
            ability=ability, source=source, submit_id="audit-0000",
            aid=0, resolution=spec.get("resolution") or (ability.resolutions[0]
                                                         if ability.resolutions else ""),
            workspace_id="0", prompt=spec.get("prompt") or "")
        draft = json.loads(body["draft_content"])
    except Exception as e:  # noqa: BLE001
        return False, [f"构造失败：{type(e).__name__}: {e}"]

    summary = postedit.draft_summary(body["draft_content"])
    notes.append(f"shape={ability.shape} code={ability.ability_code or ability.ability} "
                 f"gt={ability.gen_type}")

    # 2) 与证据对拍（只比键集与常量值，跳过我传的占位）
    if evidence and (EVIDENCE_DIR / evidence).exists():
        real = json.loads((EVIDENCE_DIR / evidence).read_text(encoding="utf-8"))
        r = dict(_shape(json.loads(real["draft_content"])))
        m = dict(_shape(draft))
        extra = set(m) - set(r)
        missing = set(r) - set(m)
        if extra:
            notes.append(f"⚠ 比证据多出键：{sorted(extra)[:4]}")
        if missing:
            notes.append(f"⚠ 比证据缺失键：{sorted(missing)[:4]}")
        if not extra and not missing:
            notes.append(f"键集与 {evidence} 一致（{len(r)} 项）")
        ok = not extra and not missing
    else:
        notes.append("无抓包样本（legacy 族只有作品引用），只做构造检查")
        ok = True

    # 3) 不变量
    for abl_key, check, desc in INVARIANTS:
        if abl_key != key:
            continue
        try:
            passed = bool(check(draft))
        except Exception:  # noqa: BLE001
            passed = False
        if not passed:
            notes.append(f"✗ 不变量破坏：{desc}")
            ok = False
        elif verbose:
            notes.append(f"✓ {desc}")

    if verbose:
        notes.append("摘要：" + json.dumps(summary, ensure_ascii=False)[:220])
    return ok, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只查某个能力（key 或中文名）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    targets = ([postedit.normalize_model(args.only)] if args.only
               else list(postedit.ABILITIES))
    bad = 0
    print("== 能力自检（零请求 · 零计费）==\n")
    for key in targets:
        ok, notes = audit_one(key, verbose=args.verbose)
        mark = "✓" if ok else "✗"
        print(f"{mark} {key}")
        for n in notes:
            print(f"    {n}")
        print()
        if not ok:
            bad += 1
    print(f"结论：{len(targets) - bad}/{len(targets)} 通过")
    if not postedit.UNSUPPORTED:
        print("不支持清单：空（8 项能力全部接入）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
