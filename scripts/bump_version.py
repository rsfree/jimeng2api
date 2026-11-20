#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把版本号写回所有载体，并校验它们一致。

    uv run python scripts/bump_version.py 0.1.1      # 写回
    uv run python scripts/bump_version.py --check    # 只校验一致性

为什么要这个脚本：版本号散在**三处**，漏一处就会漂 ——
`app/__init__.py` 的 `__version__`（`/healthz` 报的就是它）、两个 compose 的镜像 tag。
图片镜像 tag 漏改时，`docker compose up -d --build` 会把新代码跑在旧 tag 名下，
排查时看到"版本没变"却以为是部署没生效。

每条替换都断言**恰好命中 1 次**：正则失配时静默改成 0 处，是最难查的一类发版事故。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    # (相对路径, 正则, 替换模板)
    ("app/__init__.py", r'__version__ = "[^"]+"', '__version__ = "{v}"'),
    ("docker-compose.yml", r"image: jimeng2api:[\w.\-]+", "image: jimeng2api:{v}"),
    # 生产实际执行的就是这一份（部署时 tar 到 /opt/jimeng2api 根目录覆盖）
    ("docker-compose.prod.yml", r"image: jimeng2api:[\w.\-]+",
     "image: jimeng2api:{v}"),
    # deploy/ 下那份是给人看的部署样例，也得同步，否则照着它部署会拉起旧镜像
    ("deploy/ai-prod/docker-compose.yml", r"image: jimeng2api:[\w.\-]+",
     "image: jimeng2api:{v}"),
]


def read_current() -> str:
    s = (ROOT / "app/__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__ = "([^"]+)"', s)
    if not m:
        raise SystemExit("app/__init__.py 里找不到 __version__")
    return m.group(1)


def check() -> int:
    cur = read_current()
    bad = []
    for rel, pat, _ in TARGETS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        hits = re.findall(pat, text)
        if len(hits) != 1:
            bad.append(f"{rel}: 正则命中 {len(hits)} 次（应为 1）")
            continue
        found = re.search(r"[\d]+\.[\d]+\.[\d]+[\w.\-]*", hits[0])
        if not found or found.group(0) != cur:
            bad.append(f"{rel}: {hits[0]!r} 与 __version__={cur} 不一致")
    if bad:
        print("版本不一致：")
        for b in bad:
            print("  -", b)
        return 1
    print(f"版本一致：{cur}（{len(TARGETS)} 处）")
    return 0


def bump(version: str) -> int:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"版本号必须是 X.Y.Z：{version!r}")
    for rel, pat, tpl in TARGETS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(pat, tpl.format(v=version), text)
        if n != 1:
            raise SystemExit(f"{rel}: 正则命中 {n} 次（应为 1），拒绝写入")
        path.write_text(new, encoding="utf-8")
        print(f"  {rel} -> {version}")
    return check()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", nargs="?", help="目标版本（不带 v），如 0.1.1")
    ap.add_argument("--check", action="store_true", help="只校验一致性")
    a = ap.parse_args()
    if a.check:
        return check()
    if not a.version:
        raise SystemExit("用法：bump_version.py 0.1.1 | --check")
    print(f"写回 {a.version}：")
    return bump(a.version)


if __name__ == "__main__":
    raise SystemExit(main())
