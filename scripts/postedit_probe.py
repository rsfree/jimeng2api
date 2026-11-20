#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后编辑能力探针：**默认只 dry-run，一个请求都不发**。

    uv run python scripts/postedit_probe.py --list
    uv run python scripts/postedit_probe.py --ability jimeng-hd --item 769…:474…
    uv run python scripts/postedit_probe.py --ability 扩图 --uri tos-cn-i-… --ratio 16:9
    # 真要提交（会扣积分）必须两个闸门都给：
    uv run python scripts/postedit_probe.py --ability 扩图 --uri … --go --yes-i-pay

为什么要两个闸门：`--go` 是"我知道这是真实提交"，`--yes-i-pay` 是
"我接受这次扣费"。合成一个参数会让"手滑多打一个字母"直接变成账单。

**提交体由 `postedit.build_submit_body` 构造**，与网关真实提交是同一份代码 ——
探针自己拼 body 的话，它验证的就不是生产路径（旧版曾拼出带
`extend.root_model` 的形态，那个字段在上游根本不存在）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

sys.path.insert(0, ".")

from app import db, pool as pool_mod, repo  # noqa: E402
from app.config import settings  # noqa: E402
from app.upstream import postedit  # noqa: E402

#: dry-run 时 aid 只能填占位：真实 aid 来自账号，拿不到就明说是占位
PLACEHOLDER_AID = 0


def _source(args, ability: postedit.Ability) -> dict:
    """按形态构造来源。三种形态互斥，**先按形态分派，再看载体**。"""
    if ability.shape == "segment":
        raise SystemExit(f"{ability.name} 是同步分割能力，没有可提交的草稿")
    if ability.shape == "unified_v2":
        if not args.uri:
            raise SystemExit("unified_v2 必须给 --uri <tos-…>")
        if not args.url:
            raise SystemExit("unified_v2 必须给 --url（带签名的图片地址）")
        if not args.prompt.strip():
            raise SystemExit("智能编辑必须给 --prompt")
        return postedit.build_smart_edit_source(
            origin_uri=args.uri, image_url=args.url,
            width=args.width, height=args.height,
            markers=json.loads(args.markers) if args.markers else [])
    if ability.shape == "unified":
        if args.item:
            raise SystemExit("unified 能力只收 --uri（作品引用仅 legacy 支持）")
        if not args.uri:
            raise SystemExit("unified 能力必须给 --uri <tos-…>")
        if ability.needs_mask and not args.mask:
            raise SystemExit(f"{ability.name} 必须给 --mask <tos-…>")
        return postedit.build_unified_source(
            origin_uri=args.uri, mask_uri=args.mask or "",
            width=args.width, height=args.height,
            ratio=args.ratio, scale=args.scale)
    if args.item:
        iid, hid = args.item.split(":", 1)
        return postedit.build_source(item_id=int(iid), origin_history_id=int(hid))
    if args.uri:
        return postedit.build_source(image_uri=args.uri)
    raise SystemExit("必须给 --item <item_id>:<history_id> 或 --uri <tos-…>")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="打印能力表")
    ap.add_argument("--ability", default="jimeng-hd")
    ap.add_argument("--item", default="", help="作品引用 item_id:history_id（legacy 能力）")
    ap.add_argument("--uri", default="", help="已上传素材 tos uri")
    ap.add_argument("--mask", default="", help="蒙版 tos uri（局部重绘 / 消除笔）")
    ap.add_argument("--url", default="", help="带签名的图片 URL（智能编辑需要）")
    ap.add_argument("--width", type=int, default=0, help="原图宽（unified 需要，填 0 会被上游判无尺寸）")
    ap.add_argument("--height", type=int, default=0, help="原图高")
    ap.add_argument("--ratio", default="original",
                    help="扩图比例：original / 3:4 / 16:9 / 4:3 / 9:16")
    ap.add_argument("--scale", type=float, default=1.5,
                    help="扩图倍率（仅 ratio=original 生效）：1.5 / 2 / 3")
    ap.add_argument("--prompt", default="", help="提示词（扩图写进 core_param 与文本资源）")
    ap.add_argument("--markers", default="",
                    help='智能编辑标记（JSON 数组）：\'[{"type":"brush","points":[[0.4,0.7]]}]\'')
    ap.add_argument("--go", action="store_true", help="真实提交（计费）")
    ap.add_argument("--yes-i-pay", action="store_true", help="确认接受扣费")
    ap.add_argument("--account", type=int, default=0, help="指定账号 id（默认取第一个可用）")
    ap.add_argument("--resolution", default="",
                    help="输出档位；留空取能力的默认档（pro_hd=2k、智能编辑=1.5k）")
    args = ap.parse_args()

    if args.list:
        for k, a in postedit.ABILITIES.items():
            flag = "实验" if a.experimental else "可用"
            extra = " needs_mask" if a.needs_mask else ""
            print(f"{k:20s} shape={a.shape:9s} code={a.ability_code or a.ability:18s} "
                  f"gt={str(a.gen_type):4s} [{flag}]{extra} {a.name}")
        print("\n不支持：")
        for k, why in postedit.UNSUPPORTED.items():
            print(f"  {k:20s} {why}")
        return 0

    ability = postedit.get(args.ability)
    if ability is None:
        reason = postedit.unsupported_reason(args.ability)
        if reason:
            raise SystemExit(f"{args.ability} 当前不支持：{reason}")
        raise SystemExit(f"未登记的能力：{args.ability}（--list 看清单）")

    # 档位留空时取能力表的第一项 —— 它与 service 的默认值同源，不会分叉
    resolution = args.resolution or (ability.resolutions[0]
                                     if ability.resolutions else ("2k" if ability.shape == "prohd" else ""))
    source = _source(args, ability)
    submit_id = str(uuid.uuid4())

    # ---------------------------------------------------------------- dry-run
    body = postedit.build_submit_body(
        ability=ability, source=source, submit_id=submit_id,
        aid=PLACEHOLDER_AID, resolution=resolution,
        workspace_id=settings.postedit_workspace, prompt=args.prompt)
    print("== 草稿形态自检（未发任何请求）==")
    print(json.dumps(postedit.draft_summary(body["draft_content"]),
                     ensure_ascii=False, indent=2))
    print("\n== 提交体外层 ==")
    print(json.dumps({k: ("<草稿见上>" if k == "draft_content" else v)
                      for k, v in body.items()}, ensure_ascii=False, indent=2))
    print(f"\n注：aid={PLACEHOLDER_AID} 是占位，真实提交会用账号的 aid。")

    if not args.go:
        print("\n[dry-run] 未提交。真实提交请加 --go --yes-i-pay（会计费）。")
        return 0
    if not args.yes_i_pay:
        raise SystemExit("--go 还缺 --yes-i-pay：拒绝在未确认扣费的情况下提交")

    # ---------------------------------------------------------------- 真实提交
    db.init()
    accounts = [a for a in repo.list_accounts() if a["enabled"]]
    if args.account:
        accounts = [a for a in accounts if a["id"] == args.account]
    if not accounts:
        raise SystemExit("没有可用账号")
    account = accounts[0]
    rt = pool_mod.pool.runtime(account["id"])
    submit_id = str(uuid.uuid4())
    print(f"\n提交到账号 #{account['id']} … submit_id={submit_id}")
    rt.client.submit_postedit(
        ability=ability, source=source, submit_id=submit_id,
        resolution=resolution, count=1, prompt=args.prompt,
        workspace_id=settings.postedit_workspace)

    print("已提交，开始轮询（最长 180s）")
    deadline = time.time() + 180
    while time.time() < deadline:
        st = rt.client.fetch_many([submit_id]).get(submit_id) or {}
        print("  ", st.get("status_name"), st.get("progress") or "")
        if st.get("ok"):
            print(json.dumps(st.get("images"), ensure_ascii=False, indent=2))
            return 0
        if st.get("failed"):
            print("失败：", st.get("fail_message"))
            return 1
        time.sleep(5)
    print("轮询超时（任务可能仍在跑，去作品页确认，别重发）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
