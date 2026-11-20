# 上游能力：后编辑（智能超清 / 超清 / 细节修复 / 扩图）

> 事实标注口径：**[实]** = 有真实抓包或实跑记录；**[据]** = 能力名/枚举有据但形态未取证；
> **[缺]** = 没有证据，禁止当事实写进实现。

## 1. 两套草稿形态

后编辑**不重新生成画面**，只在既有像素上增强或延展。但"后编辑"在即梦里有**两代结构**，
混用会直接 `ret=1000`：

| shape | 能力 | 关键字段 | 证据 |
|---|---|---|---|
| `prohd` | 智能超清（Paz） | `abilities.pro_hd` + `core_param.model` + `resolution_type`，**无** `gen_option` | **[实]** 2026-11-19 提交成功，实扣 19 积分 |
| `legacy` | 超清（normal_hd）/ 细节修复 | `abilities.<ability>.postedit_param` + `gen_option`，**无** `core_param` | **[实]** 超清：本号历史 12 条；细节修复：2026-09 实测 |

## 2. 生成类型枚举（`GenerateType`）

取自前端 bundle（模块 `yMd`，2026-11-19），与真实草稿互证：

| 值 | 名称 | 落到的工具 |
|---|---|---|
| 1 | Text2Image | 文生图 |
| 2 | SuperResolution | **细节修复** |
| 7 | InPaint | 局部重绘（需 mask） |
| 8 | OutPaint | **扩图** |
| 9 | InPaintRemove | 消除笔（需 mask） |
| 10 | Text2Video | 视频 |
| 12 | Blend | 图生图 |
| **13** | **SuperDefinition** | **普通超清（`normal_hd`）** |
| 14 | Matting | 抠图 |
| **35** | **ProHD**（枚举之外，实测得出） | **智能超清（`pro_hd`）** |

> 互证一：本号历史里 12 条超清草稿，`component.gen_type` 全是 13、`generate_type=normal_hd`。
> 互证二：2026-11-19 抓到的智能超清请求，`postedit_param.generate_type=35`、`generate_type=pro_hd`。
> 35 不在 bundle 的枚举里 —— **bundle 会滞后于服务端**，别拿它当全集。

另一套 `AbilityCode`（v2 草稿用）命名不同：`super_resolution` / `pro_hd_2k|4k|8k` /
`out_painting` / `cutout` / `smart_edit_v2`。**别把两套名字互相套用。**

## 3. 已登记的能力

| 对外模型 | ability | gen_type | 输入 | 证据 | 状态 |
|---|---|---|---|---|---|
| `jimeng-pro-hd`（智能超清） | `pro_hd` | 35 | 上传图 | **[实]** 成功，19 积分/次（2K/4K/8K 同价） | 可用 |
| `jimeng-hd`（超清/变清晰/超分） | `normal_hd` | 13 | 上传图 / 作品引用 | **[实]** 上传图 1024→2048 出图，**余额未变** | 可用 |
| `jimeng-detail-fix`（细节修复） | `super_resolution` | 2 | **仅**作品引用 | **[实]** item 引用成功（2048→1920），未扣费；带上传图会 `generate_failed` | 可用 |
| `jimeng-inpaint`（局部重绘） | `inpainting`（unified） | 7 | 上传图 + **mask** | **[实]** 端到端成功，1 积分/4 张 | 可用 |
| `jimeng-erase`（消除笔） | `inpainting_remove`（unified） | 9 | 上传图 + **mask** | **[实]** 端到端成功，1 积分/1 张 | 可用 |
| `jimeng-cutout`（抠图/去背景） | `saliency_seg` | — | 上传图 | **[实]** 免费、1.9s、返回透明底 PNG | 可用（同步） |
| `jimeng-outpaint`（扩图） | `outpainting`（unified） | 8 | 上传图 | **[实]** 16:9→1820×1024、3x→3072²（几何公式两处被产物尺寸反证） | 可用 |
| `jimeng-smart-edit`（智能编辑） | `smart_edit_v2`（unified_v2） | **101** | 上传图 + **prompt** | **[实]** 1.5k=1536（4 积分）、2k=2048（**6 积分**） | 可用 |

实验能力需显式打开：`JIMENG2API_POSTEDIT_EXPERIMENTAL=1`。
**当前没有实验能力**（全部已取证），开关保留给未来新增的能力。

证据等级：**[实]** = 有真实提交成功/失败的回执；**[抓]** = 从 UI 抓到的请求体，
与生产构造逐字段对齐，但未跑过真实提交。两者都不含"猜"——凡是猜的字段都没写进代码。

## 4. 智能超清：真实请求（2026-11-19，成功）

完整外层提交体存档在 [`docs/evidence/2026-11-19-pro-hd-generate.json`](evidence/2026-11-19-pro-hd-generate.json)。

```json
{
  "extend": {"workspace_id": 24813558116108},
  "submit_id": "f18307f8-…",
  "metrics_extra": "{…}",
  "draft_content": "{
    \"type\":\"draft\",\"min_version\":\"3.3.40\",
    \"min_features\":[\"AIGC_GenerateType_ProHD\",\"AIGC_PostEdit_UpscaleModel\"],
    \"version\":\"3.3.40\",
    \"component_list\":[{
      \"type\":\"image_base_component\",\"aigc_mode\":\"workbench\",
      \"generate_type\":\"pro_hd\",              ← 没有数字 gen_type
      \"abilities\":{\"pro_hd\":{
        \"core_param\":{\"model\":\"image_upscale_paz\",\"generate_type\":0},
        \"resolution_type\":\"2k\",
        \"postedit_param\":{\"generate_type\":35,\"origin_image\":{ …对象… }}
      }}
    }]
  }",
  "http_common_info": {"aid": 513695}
}
```

四个必须照抄的点：

1. **`origin_image` 是对象**，不是字符串：
   `{type:"image", source_from:"upload", platform_type:1, image_uri, width, height, format:"", uri}`
   —— 传 `"tos-…"` 字符串会 `ret=1000`（2026-11-19 实测，零计费）。
2. **`pro_hd` 有 `core_param`、没有 `gen_option`**。这一点与 legacy 相反。
3. **`extend` 是 `{"workspace_id": …}`**，不是 `{"root_model": …}`。
4. `min_features` 必须带 `AIGC_GenerateType_ProHD` + `AIGC_PostEdit_UpscaleModel`。

### 4.1 `workspace_id` 的来路

UI 点"生成"时会**新建一个工作区**（名字形如 `图片生成-2026-11-19-2142`），请求里带它的 id。

网关没有照抄这一步：`/mweb/v1/workspace/create` 的 body 未取证，猜一个 body 去建工作区
会在用户账号里留下垃圾。默认落到账号默认工作区（`0`），可用 `JIMENG2API_WORKSPACE_ID` 覆盖。

**`workspace_id=0` 已验证可用**（2026-11-19 网关端到端：HTTP 200、38.5 s、2048×2048）。
抓包里 UI 用的是新建工作区，但默认工作区同样被接受 —— 不需要为每次提交建工作区。

## 4.2 unified_generate 族：局部重绘 / 消除笔（2026-11-19）

```json
POST /mweb/v1/aigc_draft/generate
{
  "extend": {"workspace_id": …},
  "draft_content": {
    "component_list": [{
      "generate_type": "unified_generate",
      "abilities": {"unified_generate": {
        "core_param": {"prompt": "…", "generate_type": 0},
        "generate_type": 7,
        "unified_ability_list": [{
          "ability_code": "inpainting",
          "ability_param": {"resource_list": [
            {… "purpose": 11, "content": {"image": {"image_content": {…"name":"origin"}}}},
            {… "purpose": 12, "content": {"image": {"image_content": {…"name":"mask"}}}}
          ]}
        }]
      }}
    }]
  }
}
```

四个关键点：

1. **`purpose` 编码资源角色**：`11`=原图、`12`=蒙版；节点名也是 `origin` / `mask`。
2. **蒙版是普通 PNG**：白=重绘区域、黑=保持（实测真实蒙版：1,047,860 黑 + 716 白，
   716 正好是画笔那一笔）。
3. **`core_param.prompt` 是提示词位置** —— 写死空串会让上游「自由发挥」。
4. 外层 `extend` 用 `workspace_id`（同 pro_hd）。

局部重绘实测：1 积分 / 4 张 1024×1024；只改蒙版区域，其余像素保持。

消除笔同族，实测（2026-11-19）：**1 积分 / 1 张**。注意它与局部重绘**张数不同**
（1 vs 4）—— 两者 `gen_count` 都是 1，差异来自上游按能力分档。

**蒙版必须完全覆盖目标**，这是两次实测对比出来的，不是推测：

| 蒙版 | 结果 |
|---|---|
| `410-610 × 690-850`（红块是 `430-600 × 700-870`，**下边缘差 20px**） | 红块被削成扁矩形：**覆盖到的部分擦掉了，露在外面的留着** |
| `400-640 × 670-900`（完全包住） | 红块**彻底消失**，只剩蓝圆与背景 |

所以"消除笔没擦干净"这个现象先查蒙版边界，而不是怀疑能力本身。

### 4.2.1 消除笔 = 同一族，六处不同

消除笔（`InPaintRemove=9`）的草稿与局部重绘**完全同构**，53 个字段一一对应。
差异只有下面这些，全部来自抓包原文：

| 位置 | 局部重绘 | 消除笔 |
|---|---|---|
| `generate_type` | `7` | **`9`** |
| `ability_code` | `inpainting` | **`inpainting_remove`** |
| `core_param.prompt` | `""`（键存在） | **键缺失** |
| `ability_param.option` | `{"type":"","id":…}` | **键缺失** |
| `sceneOptions[0].scene` | `ImageInPaintRepaintButton` | `ImageInPaintEraserButton` |
| `sceneOptions[0].benefitCount` | `1` | **键缺失** |
| `metrics.toolItemId` / `tool_entry.modal_tool_key` | `23730762306060` / `image_inpaint` | `23722237505548` / `image_inpaint_remove` |

⚠️ **这三处"键缺失"是契约的一部分，不是省略**。写成 `prompt: ""`、`option: {}`、
`benefitCount: 0` 都会偏离抓包 —— 上游对未知/多余键的处理没有证据支持，
而 `tests/test_postedit.py::test_erase_key_presence_differs_from_inpaint`
就是钉这一点的。

**取证方式与代价**：这次不是靠真实提交，而是在页面里 hook
`XMLHttpRequest.prototype.send` 与 `window.fetch`，命中 `aigc_draft/generate`
就把 body 存下来并 `abort()` —— **零计费**、请求不出浏览器。

因此它的证据等级是 **[抓]** 而非 **[实]**：草稿是 UI 真实构造的，
但"上游是否接受这份草稿"尚未验证。局部重绘的成功不能直接外推 ——
两者的 `core_param` / `ability_param` 键集不同。

⚠️ **已知偶发**：提交阶段偶发 `division by zero`（本地异常，**不扣费、任务不创建**），
重启进程后常自愈；根因未定位，`coordinator` 已补 `log.exception` 便于下次取证。

## 4.2.0 作品引用从哪来（含一个字段名坑）

### 产物里直接给（2026-11-20 起）

任务查询的每个产物都带 `item_id` 与 `history_id`，拼起来就是引用：

```json
{"url": "https://…", "width": 1536, "height": 1536,
 "item_id": "7709905466289196350", "history_id": "51910303526668"}
```

```bash
# 拿上面的产物直接喂给「细节修复」
curl -s http://127.0.0.1:8300/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"细节修复","image":"item:7709905466289196350:51910303526668"}'
```

⚠️ **item 引用绑账号**。网关会在 `assets` 表里记下「这个 item 是谁产的」，
提交 `item:` 引用时**锁定那个账号**；该号不可用（禁用/冷却/在途满）就
**明确失败**，不会换号重试 —— 因为 `detail_fix` 用错号是 `generate_failed` **且计费**。

历史任务的产物可以用 `scripts/backfill_assets.py --apply` 补归属；
但加 `history_id` 之前的老产物只有 `item_id`（历史 ID 没存），这类引用要自己去 UI 补齐。


`jimeng-hd` / `jimeng-detail-fix` 要 `item:<item_id>:<history_id>`，这两个值来自
**提交回执**，不是 `get_history`：

| 来源 | 字段 |
|---|---|
| 提交回执 | `data.aigc_data.history_record_id`（= history_id）、`item_list[].item_id` |
| 轮询 `get_history_by_ids` | `data.<submit_id>.item_list[].item_id` |

⚠️ **`/mweb/v1/get_history` 的列表字段是 `records_list`，不是 `item_list`**。
读错字段会得到"永远空列表"，很容易误判成"账号没作品"。它的顶层是
`{has_more, next_offset, records_list, agent_conversation_session_map}`。

另一个坑：**`history_type` 用 0~5 都试过，本号全部返回 0 条**（`has_more:false`）——
所以 `records_list` 空不一定是参数错，也可能是该号真没有可列的生成记录。
别拿"能列出作品"当能力前提。

## 4.2.1 细节修复：item 引用成功且未扣费（2026-11-20）

用 `item:<item_id>:<history_id>` 提交，`success`，产物 **1920×1920**（输入是 2048×2048）。

**输入 2048 → 输出 1920** 说明它不是"放大"，而是"重修细节"，输出尺寸由它自己定
（不是简单的倍数关系）。别拿它当超分用。

余额 5774 → 5774 **未变**。所以它的计费行为要分开说：

| 路径 | 结果 | 计费 |
|---|---|---|
| 作品引用（正确用法） | success，1920×1920 | **未观察到扣费**（1 次样本） |
| 带上传图（错误用法） | `generate_failed` | 历史观测**会计费** |

也就是说它是一个"**用错要罚钱**"的能力 —— 这正是网关把它锁成 `input="item"`
并在入口拦掉上传图的原因。不要为了"方便"放开这个校验。

## 4.2.2 普通超清：`origin_image` 必须是**对象**（2026-11-20）

这是一条踩了两次的坑：

| 时间 | `postedit_param.origin_image` | 结果 |
|---|---|---|
| 2026-11-19 | `"tos-cn-i-…"`（**字符串**） | `ret=1000`，零计费拒绝 |
| 2026-11-20 | `{type:"image", image_uri, width, height, …}`（**对象**） | `ret=0`，出图 2048×2048，**余额不变** |

对象形态与 `pro_hd` 的 `origin_image` 是同一个构造（`_image_node`）：
`{type, id, source_from:"upload", platform_type:1, name:"", image_uri, width, height, format, uri}`。

提交返回的 `submit_id` 在 **`data.aigc_data.task.submit_id`**（不是顶层），
`status` 初始为 45，与其它能力同一套轮询。

⚠️ **"免费"的边界**：2 次提交（含 1 次完整轮询出图）余额都不变，所以**当前观测**
是不扣费；但样本只有 2 次、且不排除该号有免费额度。**不要把它当承诺**，
计费策略上游随时可改。相比之下 `detail_fix` 带上传图会 `generate_failed` **且计费** ——
同一族里两个能力的计费行为可以相反。

## 4.3 抠图：同步分割，不是生成任务

```json
POST /mweb/v1/saliency_seg
{"image_uri_list": ["tos-cn-i-…"], "mode": "canvas"}
→ {"ret":"0","data":[{"mask":{"uri":"tos-cn-i-…","url":"https://…png"}}]}
```

三个关键事实（2026-11-19 实测）：

1. **免费**：调用前后余额不变（上传与分割都不扣）；画布里的"生成"才收 1 积分。
2. **`mask.url` 就是成品**：RGBA PNG，**alpha 通道即蒙版** —— 主体保留原色、
   背景 alpha=0，直接可交付，不需要二次合成。
3. **没有 `submit_id`**：它是同步接口（实测 1.9s），进任务队列只会造一个查不到
   状态的壳。所以网关把它做成同步路径，不进协调器。

```bash
curl -s http://127.0.0.1:8300/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"抠图","image":"https://…/a.jpg"}'
# → 200 {"data":[{"url":"…png","format":"png"}],"credits":{"forecast":0,"charged":0}}
```

效果取决于输入图**有没有明确主体**：棋盘/风景这类没有单一主体的图，只会留下
最显著的那个对象；这是分割算法的语义，不是缺陷。

## 4.4 扩图：`option.out_painting` 的四边扩展（2026-11-19 抓包）

扩图**也是 `unified_generate`**（`generate_type: 8`、`ability_code: outpainting`），
与局部重绘同族，但草稿里两处形态不同：

```json
"ability_param": {
  "resource_list": [
    {… "resource_type": 1, "purpose": 1,
     "content": {"text": {"text_content": "扩展成森林背景"}}},   // 仅提示词非空时
    {… "resource_type": 2, "purpose": 11, "name": "origin"}
  ],
  "option": {"type": "", "id": "…", "out_painting": {
      "type": "", "id": "…",
      "bottom": 0, "left": 0.3888888888888889,
      "right": 0.388888888888889, "top": 0,
      "max_size": 1820, "image_ratio": 3
  }}
}
```

### 几何规则（实测反推，已公式化）

UI 的两套模式**互斥**：

| 模式 | 规则 | 抓包样本 |
|---|---|---|
| 具体比例 | **短边不动**，长边按目标比例扩展；倍率不参与 | 3:4 / 16:9 / 4:3 / 9:16 |
| 原比例 | 四边各扩 `(scale-1)/2`，`max_size` = 长边 × scale | 1.5x / 2x / 3x |

- 每边扩展量用**未取整**的理论尺寸算 —— 3:4 的 `top` 是精确的 `1/6`，
  而不是 `341/2/1024`（那种取整值抓不到一致结果）；
- 只有 `max_size` 取整；
- **3x 实测 3072×3072**（2026-11-20）：非整数倍也走同一个公式，`max_size = 原边长 × scale`；
- ⚠️ **`max_size` 是上限，不是精确输出**：800×600 扩 16:9，我们算 `max_size=1067`
  （`round(600×16/9)`），上游实际输出 **1066** —— 它按四边扩展量还原尺寸后取 floor。
  1024×1024 那次（1820.44 → 1820）两种取整恰好相等，所以这个差异此前没暴露。
  **客户端不要按 `max_size` 校验产物尺寸**，差 1 像素是正常现象。
- `image_ratio` 不是按钮顺序：**原比例=1、3:4=2、16:9=3、4:3=4、9:16=5**。
  只抓 16:9 会误以为它是"方向枚举"，9:16=5 直接否掉了这个假设。

### 另外两处键存在性

| 位置 | 局部重绘 | 扩图 |
|---|---|---|
| `metrics.hasRejectedAudit` | `0` | **键缺失** |
| `sceneOptions[0].benefitCount` | `1` | **键缺失** |

```bash
curl -s http://127.0.0.1:8300/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"扩图","image":"https://…/a.jpg","ratio":"16:9"}'
# 原比例 + 3x：
#   -d '{"model":"扩图","image":"…","scale":3}'
```

`ratio` 与 `scale` 二选一：选了具体比例还传 `scale`，会在 `degradations` 里
明确写「倍率不参与」，而不是静默忽略。

## 4.5 智能编辑：`unified_generate_v2`（2026-11-19 抓包）

它是**真·另一套结构**，不是 `unified_generate` 加个后缀：

| 位置 | unified_generate | unified_generate_v2 |
|---|---|---|
| 提示词 | `core_param.prompt` | **能力节点上的 `prompt`** |
| 资源节点 | `image_uri` + `name`/`format` | `image_uri` + **`image_url`**（带签名）+ 宽高 |
| 能力参数 | `ability_param`（resource_list + option） | resource_list 与 `extra` **平铺**在能力条目上 |
| 额外节点 | — | **`extra`**（画布快照）、`render_config`、`gen_option` |
| 外层 `extend` | 只有 `workspace_id` | **`root_model` + `workspace_id`** |
| gen_type | 7 / 8 / 9 | **101** |

```json
"unified_generate_v2": {
  "prompt": "把红色方块换成一只猫",
  "model": "high_aes_general_v55",
  "generate_type": 101,
  "unified_ability_list": [{
    "ability_code": "smart_edit_v2",
    "resource_list": [{"purpose": 11, "content": {"image": {
        "image_uri": "tos-…", "image_url": "https://…signature=…",
        "width": 1024, "height": 1024}}}],
    "extra": "<一个 JSON **字符串**，见下>"
  }],
  "render_config": {"resolution_type": "1.5k", "intelligent_ratio": true},
  "gen_option": {"gen_count": 1, "gen_model_type": 2}
}
```

`extra` 是**嵌套 JSON 字符串**（不是对象），里面是画布快照：

```json
{"schema":"dreamina.smart_edit.canvas","version":1,
 "restore_data":{"source_image":{…},"elements":[],
   "prompt_contents":[{"type":"text","text":"…"}],
   "tool_options":{"marker":{},"brush":{…},"arrow":{…},"text":{…},"eraser":{…}}},
 "submit":{"prompt":"…","plain_prompt":"…","output_resolution":"1.5k","submitted_image":{…}}}
```

三个必须知道的事实：

1. **`image_url` 必须有签名**：它和 `image_uri` 是两个字段，前者是带
   `x-expires` / `x-signature` 的 CDN 地址，会过期 → 每次提交前用
   `image_by_uri`（免费只读）重取，不缓存。
2. **`elements`**（画布标记）的元素结构来自前端 bundle 的校验函数
   （`chunks/2v.*.js` 的 `s(e)`），字段名是 **snake_case**：

   | 类型 | 必需字段 | 本仓 |
   |---|---|---|
   | 公共 | `id` / `z_index` / `created_at`（**秒**）/ `updated_at` / `type` | ✅ |
   | `brush` | `points[{x,y}]` + `style{color, opacity}` | ✅ 已开放 |
   | `arrow` | `start{x,y}` / `end{x,y}` + `style{color, variant:"arrow"\|"line"}` | ✅ 已开放 |
   | `marker` | `marker_index` / `mode:"point"\|"box"` / `bounds{x,y,w,h}` / `recognition{status,…}` | ❌ 需要识别状态机 |
   | `text` | `text` / `bounds` / `style{color, font_size}` | ❌ 需要排版度量 |

   坐标是 **0-1 归一化**（不是像素）：`tool_options.text.font_size` 是 `0.0356`、
   `eraser.size` 是 `0.0333`，像素坐标系下字号不可能是这个量级。

   ⚠️ **markers 不约束编辑范围**（2026-11-19 单变量对照实测）：

   | prompt | markers | 结果 |
   |---|---|---|
   | `把红色方块换成一只猫` | 有 | 蓝圆保留，猫落在红块位置 → **局部** |
   | `把标记的区域变成一只橘猫` | 有 | 整图重绘成一只猫 → **全局** |

   两次都带 markers，差别只有 prompt 措辞 ⇒ **"改哪块"由模型对 prompt 的理解决定，
   不是 markers 决定**。markers 是给模型看的视觉标注，**不是蒙版**。
   要精确控制区域，用 `jimeng-inpaint`（真蒙版、`purpose=12`）。
3. **`prompt` 是必填**：v2 用描述驱动，空 prompt 直接 400（不建任务、不计费）。

metrics 也是全族最长：除常规键外还有 `markTagCnt` / `markBoxCnt` /
`imageTextCnt` / `imageArrowCnt`（恒 0，但**键必须存在**），
`sceneOptions[0]` 里带 `modelReqKey` / `imageInputNum` / `resolutionType`，
`tool_entry.panel_key` 是 `dreamina_all_tools_category_featured`（精选，不是图片）。

```bash
# 整图编辑
curl -s http://127.0.0.1:8300/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"智能编辑","image":"https://…/a.jpg","prompt":"把红色方块换成一只猫"}'

# 带画布标记（0-1 归一化坐标；只是视觉标注，不约束范围）
curl -s http://127.0.0.1:8300/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"智能编辑","image":"…","prompt":"把红色方块换成一只猫",
       "markers":[{"type":"brush","points":[[0.42,0.70],[0.58,0.84]]}]}'
```

**输出档位**：`1k / 1.5k / 2k / 3k / 4k / 8k`（映射表来自 bundle），默认 **1.5k**。
不传或传非法值都会落到 1.5k，非法值会在 `degradations` 里标注。

**档位是计价的，而且不是线性的**（2026-11-20 实测，1024×1024 输入）：

| 档位 | 产物 | 实扣 |
|---|---|---|
| `1.5k` | 1536×1536 | 4 积分 |
| `2k` | 2048×2048 | **6 积分** |

所以 `resolution` 不是装饰字段 —— 它同时决定产物尺寸和扣费。

## 5. 当前没有"不支持"的能力

能力表 8 项**全部端到端实测通过**（详见 §3 表格）。2026-11-19 的三次真实提交：

| 能力 | 结果 | 扣费 | 产物 |
|---|---|---|---|
| 消除笔（mask 覆盖红块） | succeeded | 1 积分 | 1024×1024 ×1 |
| 扩图 `ratio=16:9` | succeeded | 1 积分 | **1820×1024** ×4（与 `max_size` 公式一致） |
| 智能编辑 `1.5k` | succeeded | 4 积分 | 1536×1536 ×1 |

扩图那一项最有说服力：**上游按我们算的 `max_size=1820` 生成了图** ——
几何公式不是"看起来对"，而是被产物尺寸反证了。

`[抓]` 这个等级现在只可能出现在**新增能力的取证阶段**，不是遗留状态。

## 6. 补一个能力只需三步（零猜测）

1. **抓一次真实请求**：浏览器 DevTools → Network → `aigc_draft/generate` → 复制 body
   （`draft_content` 是字符串，要再 `JSON.parse` 一次）；
2. **对齐 builder**：`app/upstream/postedit.py::ABILITIES` 加一条，选 `shape`，
   差异写进 `notes`；
3. **钉死形态**：把抓包存进 `docs/evidence/`，在 `tests/test_postedit.py` 加一条
   **逐字段对拍**（照 `test_prohd_draft_matches_captured_request` 的写法），
   再摘掉 `experimental`。

## 7. 探针

```bash
uv run python scripts/postedit_probe.py --list
uv run python scripts/postedit_probe.py --ability jimeng-pro-hd --uri tos-cn-i-…
uv run python scripts/postedit_probe.py --ability jimeng-pro-hd --uri … --go --yes-i-pay
```

`--go` 与 `--yes-i-pay` 刻意分开：合成一个参数会让手滑变成账单。

## 8. 接口形态

对外仍是 `POST /v1/images/generations`（单端点），`model` 选能力：

```bash
curl -s http://127.0.0.1:8300/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"jimeng-pro-hd","image":"https://…/a.jpg","resolution":"2k"}'
```

- **默认异步**：同步端点上不等待，直接 `202 + {task_id, poll_url}`；显式传
  `wait` 才阻塞（`postedit_sync_wait` 控制默认预算，0 = 不等）；
- `prompt` 可省略（后编辑不需要提示词）；
- `resolution` 只对 `jimeng-pro-hd` 有意义：`2k` / `4k` / `8k`，**三档同价 19 积分**，
  默认 `2k`；非法档位吸附到 `2k` 并写进 `degradations`；
- `n` 恒为 1；`size` 对除扩图外的能力无效，都写进 `degradations`；
- `mask`：**收下并落库**（`tasks.mask_uri`），但当前没有任何能力消费它；
  传了会在 `degradations` 里写「不消费 mask，该字段未下发上游」——
  静默丢弃会让调用方以为局部重绘生效了；
- `GET /v1/models` 的 `tools[]` 带 `ability` / `gen_type` / `shape` / `input` /
  `resolutions` / `experimental`。

## 9. 计费

后编辑按"次"计价，`get_common_config` 里没有单价表，所以 `estimate_credits()` 返回
`None`（面板显示"未报价"），协调器不预扣。真实扣费以 `credit_history` 的
`submit_id` 对账为准。

实测：智能超清 2K = **19 积分**（UI 直发 5790 → 5771；网关端到端 5771 → 5752）。

⚠️ 上游回执的 `forecast_generate_cost` 会高估：网关任务行里记的是 **118**，实扣 19。
面板上的"预估"只能当参考，对账以 `credit_history` 的 `amount` 为准。
