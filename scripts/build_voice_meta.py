#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色元数据构建：给 `data/voices.json` 里的名字打上性别/年龄/口音/风格/场景标签。

    uv run python scripts/build_voice_meta.py            # 生成 data/voice_meta.json
    uv run python scripts/build_voice_meta.py --report   # 只看分类统计与未识别项

**为什么用规则而不是让上游给**：feed 里音色的 `description` / `tags` /
`category_id_list` 全是空的（实测），元数据只能从名字推。规则表在下面，
新增音色后重跑即可；识别不出的归到 `unknown` 并在 report 里列出，方便人工补。

分类维度：
  gender  male / female / child / unknown
  age     child / teen / young / adult / senior / unknown
  accent  standard / dongbei / tianjin / henan / shandong / guangxi / yue /
          chongqing / changsha / shanghai / taiwan / xian / beijing / hongkong /
          bayarea / japanese / unknown
  style   温柔 / 活泼 / 御姐 / 萝莉 / 磁性 / 霸气 / 搞怪 / 治愈 / 悲伤 / 知性 /
          撒娇 / 病娇 / 清冷 / 阳光 / 成熟 / 沉稳 / 正统
  category  general / dialect / screen_role / anime_role / scenario
  use_case  配音 / 朗读 / 解说 / 广告 / 绘本 / 播客 / 有声书
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "voices.json"
OUT = ROOT / "data" / "voice_meta.json"

# ---------------------------------------------------------------- 规则表

#: 影视 / 剧集角色（精确匹配优先）
SCREEN_ROLES = {
    "皇上", "华妃", "紫薇", "黛玉", "容嬷嬷", "四郎", "顾姐", "如来", "如来佛祖",
    "唐僧", "猴哥", "八戒Pro", "将门女将", "神厨老佛爷", "桃花庵主",
}

#: 动画 / IP 角色
ANIME_ROLES = {
    "熊二", "猪猪侠", "佩奇猪", "动漫海绵", "动漫小新", "蜡笔小新", "蜡笔小妮",
    "懒小羊", "小八酱", "派星星", "乌萨七", "九小月", "米老哥", "春日部姐姐",
    "憨萌福娃", "聪慧胖仔", "搞怪鸭子嗓",
}

#: 场景专用音色（名字里直接带用途）
SCENARIO = {
    "广告男声": "广告", "Vlog配音": "Vlog", "生动解说": "解说",
    "沉稳解说Pro": "解说", "精品有声书": "有声书", "儿童绘本": "绘本",
    "萌娃百科": "绘本", "深夜博客": "播客", "真人播客男": "播客",
    "真人播客女": "播客", "熊爸爸讲故事": "绘本", "情感语录": "语录",
    "清醒语录": "语录", "清晰语录": "语录", "雄厚配音": "配音",
}

#: 口音 / 地域
ACCENT = [
    ("dongbei", ("东北", "老铁")),
    ("tianjin", ("天津",)),
    ("henan", ("河南",)),
    ("shandong", ("山东",)),
    ("guangxi", ("广西", "老表")),
    ("yue", ("粤语", "广东")),
    ("chongqing", ("重庆",)),
    ("changsha", ("长沙", "妹坨")),
    ("shanghai", ("上海",)),
    ("taiwan", ("台湾", "台湾腔")),
    ("xian", ("西安", "掌柜")),
    ("beijing", ("京腔", "京味", "京腔小爷")),
    ("hongkong", ("TVB", "港普")),
    ("bayarea", ("湾区",)),
    ("japanese", ("樱花",)),
]

#: 女性特征词
FEMALE = ("女声", "女生", "女孩", "少女", "妹", "姐", "阿姨", "婆婆", "淑女",
          "学妹", "学姐", "女友", "女主", "奶奶", "妈妈", "姑娘", "婆", "女大",
          "飒姐", "熟女", "女将", "老佛爷", "小妮", "夹子音", "美眉", "公主",
          "庵主", "女")

#: 男性特征词
MALE = ("男声", "男生", "男孩", "哥", "叔", "爷", "小伙", "青年", "学长", "掌柜",
        "男大", "弟弟", "伯伯", "公子", "总裁", "帝王", "少侠", "胖仔", "男宝",
        "男主播", "男子", "皇上", "唐僧", "如来", "猴哥", "八戒", "熊二", "男士",
        "少年", "君子", "老表", "勇士", "爸爸", "老铁", "低音", "老者", "男")

#: 儿童特征词
CHILD = ("小男孩", "小女孩", "萌娃", "童声", "男孩", "女孩", "萌妹", "小奶音",
         "奶萌", "软萌", "萌娃百科", "福娃", "奶萌男宝")

#: 老年特征
SENIOR = ("爷爷", "奶奶", "婆婆", "老婆婆", "老者", "老爷子", "老佛爷", "伯伯",
          "阿姨", "大叔")

#: 风格
STYLE = [
    ("温柔", ("温柔", "温和", "温婉", "柔美", "软糯", "甜软", "甜美", "清甜", "暖")),
    ("活泼", ("活泼", "开朗", "阳光", "元气", "明媚", "灵动", "爽朗", "爽快", "直爽")),
    ("御姐", ("御姐", "知性", "成熟", "妩媚", "狐媚", "魅力", "霸气", "冷")),
    ("萝莉", ("萝莉", "娇弱", "撒娇", "病娇", "呆萌", "乖巧", "奶萌", "超萌")),
    ("磁性", ("磁性", "低音", "低音炮", "苏感", "厚实", "低沉", "沉稳", "稳重")),
    ("搞怪", ("搞怪", "做作", "逗趣", "调皮", "咆哮", "搞笑")),
    ("悲伤", ("悲伤", "孤傲", "清冷", "冷")),
    ("正统", ("正统", "端庄", "正气", "正义", "威严", "权威", "严肃", "严厉",
              "刚正", "硬朗", "老实", "憨厚")),
]


def classify(name: str) -> dict:
    # 1) 角色类优先
    if name in SCREEN_ROLES:
        category = "screen_role"
    elif name in ANIME_ROLES:
        category = "anime_role"
    elif name in SCENARIO or any(k in name for k in ("解说", "有声书", "绘本", "播客", "语录")):
        category = "scenario"
    else:
        category = "general"

    # 2) 口音
    accent = "standard"
    for tag, keys in ACCENT:
        if any(k in name for k in keys):
            accent = tag
            break
    if category in ("screen_role", "anime_role") and accent == "standard":
        pass
    if category == "screen_role" and name in ("紫薇", "黛玉", "华妃", "容嬷嬷"):
        accent = "standard"      # 剧集配音，普通话
    if category == "dialect":
        pass
    if accent != "standard":
        category = "dialect" if category == "general" else category

    # 3) 性别
    if any(k in name for k in CHILD) and not any(k in name for k in ("男", "女")):
        gender = "child"
    elif any(k in name for k in FEMALE):
        gender = "female"
    elif any(k in name for k in MALE):
        gender = "male"
    elif any(k in name for k in CHILD):
        gender = "child"
    else:
        gender = "unknown"

    # 角色类兜底：猴哥/唐僧/如来/皇上 是男，紫薇/黛玉/华妃/容嬷嬷 是女
    if gender == "unknown":
        if name in ("紫薇", "黛玉", "华妃", "容嬷嬷", "小八酱", "春日部姐姐", "蜡笔小妮",
                    "派星星", "乌萨七", "九小月", "懒小羊", "佩奇猪"):
            gender = "female"
        elif name in ("猴哥", "唐僧", "如来", "如来佛祖", "皇上", "四郎", "熊二",
                      "猪猪侠", "动漫小新", "蜡笔小新", "动漫海绵", "米老哥"):
            gender = "male"

    # 4) 年龄
    if gender == "child":
        age = "child"
    elif any(k in name for k in ("爷爷", "奶奶", "婆婆", "老者", "老爷子", "老佛爷", "伯伯")):
        age = "senior"
    elif any(k in name for k in ("少女", "学妹", "少年", "妹坨")):
        age = "teen"
    elif any(k in name for k in ("青年", "学长", "学姐", "男大", "女大", "小伙")):
        age = "young"
    elif any(k in name for k in ("大叔", "阿姨", "熟女", "御姐", "成熟", "总裁", "帝王")):
        age = "adult"
    else:
        # 名字里没有年龄线索的一律按成年处理 —— 音色库主体是成年音色，
        # 留 unknown 会让「按年龄筛选」形同虚设。
        age = "adult"

    # 5) 风格（可多选，最多 2 个）
    styles = [tag for tag, keys in STYLE if any(k in name for k in keys)][:2]
    if not styles:
        styles = ["通用"]

    return {
        "gender": gender,
        "age": age,
        "accent": accent,
        "style": styles,
        "category": category,
        "use_case": [SCENARIO[name]] if name in SCENARIO else (
            ["配音"] if category in ("screen_role", "anime_role") else ["通用"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    voices = json.loads(SRC.read_text(encoding="utf-8"))
    meta = {}
    for name, vid in voices.items():
        if name.isascii():          # 英文别名不重复登记
            continue
        m = classify(name)
        m["id"] = vid
        meta[name] = m

    if args.report:
        print(f"音色 {len(meta)} 个\n")
        for dim in ("gender", "age", "accent", "category"):
            c = Counter(m[dim] for m in meta.values())
            print(f"{dim:10s} " + "  ".join(f"{k}={v}" for k, v in c.most_common()))
        print("\n风格 Top:", Counter(s for m in meta.values() for s in m["style"]).most_common(10))
        unk = [n for n, m in meta.items() if m["gender"] == "unknown"]
        print(f"\ngender 未识别 {len(unk)}: {unk[:20]}")
        unk2 = [n for n, m in meta.items() if m["age"] == "unknown"]
        print(f"age 未识别 {len(unk2)}: {unk2[:15]}")
        return 0

    OUT.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {OUT}（{len(meta)} 个音色）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
