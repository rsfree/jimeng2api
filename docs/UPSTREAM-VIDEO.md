# 视频：结构、取值与实测（**已实现**）

> 状态：**已接入网关并端到端实测通过**（2026-11-20）。字段名来自前端
> `serializable()` 装饰器，取值来自 `/mweb/v1/video_generate/get_common_config`，
> 结果是真实提交跑出来的 —— 三段都不是猜的。

## 0. 实测结果（2026-11-20）

| 项 | 值 |
|---|---|
| 模型 | `dreamina_seedance_40_mini`（UI 默认，`default_model_idx=3`） |
| 参数 | 720p / 4000ms / 16:9 / `video_mode=2` / `fps=24` |
| 提交 | `ret=0`，`history_record_id=51869467820556` |
| 轮询 | `submitted` → 48 次（约 5 分钟）→ `success` |
| 产物 | **1280×720 mp4**，`HTTP 206` + `video/mp4`，文件头为有效 ISO MP4 |
| 扣费 | **24 积分**（4s；另一发 5s 扣 30 → **≈6 积分/秒**，720p mini） |

产物落点：`item_list[].video.transcoded_video[<档>].video_url`（同一段视频给
360p/720p/1080p 多档，取最高分辨率）。

### 图生视频（首帧）实测

同一套草稿，`video_gen_inputs[0]` 多一个 `first_frame_image`：

```json
{"image_uri": "tos-cn-i-…", "width": 1024, "height": 1024, "format": "png"}
```

**它是扁平对象**，不是图片侧那种完整 image node（那个带 `type`/`source_from`/
`platform_type`）—— 两种形态项目里都有，套错不会报错，只会静默失效。

| 项 | 值 |
|---|---|
| 提交 | `ret=0`，`1:1` / 720p / 4000ms |
| 轮询 | 16 次（约 96 秒）→ `success`，比文生视频快得多 |
| 产物 | 960×960 mp4 |
| 扣费 | **24 积分**（与文生视频同价） |
| **首帧生效** | 抽出视频第一帧与原图逐像素对比：构图、图形、位置一致（仅轻微缩放） |

所以 `first_frame_image` 是**真起点**，不是"参考图"。

`end_frame_image` 结构相同（扁平对象），网关已支持但**未单独实测**。

### Seedance 2.5 Flash 实测（2026-11-20）

同一套草稿，只换 `model_req_key` = `dreamina_seedance_45_flash`：

| 项 | 值 |
|---|---|
| 提交 | `ret=0`，720p / 4000ms / 16:9 |
| 轮询 | 18 次（约 108 秒）→ `success` |
| 产物 | 1280×720 mp4，抽帧确认画面正常（橘猫起身伸懒腰，两帧有动作变化） |
| 扣费 | **76 积分**（4s）→ **≈19 积分/秒** |

**价差是 3.2 倍**：同参数下 2.0 mini 只要 24 积分。UI 上"¥0.08/秒"是会员/团队卡
折算价，不是积分价 —— 别拿它做成本估算。

所以网关把单价做进模型表（`credits_per_second`），**不再用全局常量**：
未实测的模型给 `null`，面板显示"—"，而不是编一个数。

⚠️ `duration` 字段回显是 5 秒（我们传 4000ms）—— 上游似乎有 5 秒起点的取整，
config 的 `min_duration_ms: 4000` 与实际产物时长不完全一致。

## 1. 与图片完全不是一套

图片走 `aigc_draft/generate` + `component_list[].abilities.<能力>`；
视频走**同一个端点**但组件类型换成 `video_base_component`：

```python
# chunks/lib-agreement-1.*.js
t.DAVideoBaseComponent = u
  serializable("generate_type")   -> generateType      # 字符串枚举
  serializable("abilities")       -> DAVideoGenerateAbilities
  serializable("process_type")    -> processType       # 数字枚举
```

`DAVideoBaseComponent` **只有三个字段**，没有 `core_param`、没有 `unified_ability_list`。

## 2. 枚举（原文照抄）

```js
DAVideoGenerateType = {
  none: "",                         scenarioToolGenImage: "scenario_tool_gen_image",
  scenarioToolGenVideo: "scenario_tool_gen_video",
  lipSync: "lip_sync",              genVideo: "gen_video",
  mergeVideo: "merge_video",        subtitleErase: "subtitle_erase",
  videoAudioEffect: "video_audio_effect",
  videoTemplate: "video_template",  videoBgm: "video_bgm",  workflow: "work_flow",
}
DAVideoProcessType = { VideoGen: 1, SR: 2, InsertFrame: 3, LipSync: 4,
                       Extend: 5, LabSR: 6, MixAudioVideo: 7 }
DAVideoResolutionType = { "480p", "720p", "1080p" }
DAVideoMode = { v1Default: 0, preview: 1, _default: 2, iCLivePhoto: 3,
                avatar: 4, avatarLoopy: 5, avatarHq: 6, avatarHq480: 7 }
DAPriority = { Normal: 0, Relax: 1 }
```

## 3. 能力树（`abilities` 的九个键）

```
abilities
├── scenario          DAScenarioAbility
├── subtitle_erase    DASubtitleEraseAbility
├── lip_sync          DALipSyncAbility
├── gen_video         DAGenVideoAbility      ← 文/图生视频
├── video_audio_effect
├── video_bgm
├── merge_video
├── workflow
└── ai_camera
```

`gen_video` 四个字段：

```
text_to_video_params  DAText2VideoParams
scene                 string
video_task_extra      **字符串**（内含 JSON，同智能编辑的 extra）
video_ref_params      DAVideoRefParam
```

`text_to_video_params` 五个字段：

```
video_gen_inputs     DAVideoGenInput[]      ← 真正的入参
video_aspect_ratio   string                 ← 格式未取证（"16:9"？"16x9"？）
seed                 int64
model_req_key        string                 ← 取值未取证（UI 显示"Seedance 2.0 mini"）
priority             DAPriority
```

`video_gen_inputs[]` 的序列化名（16 个，来自装饰器）：

```
prompt / is_draft_mode / lens_motion_type / motion_speed / vid / ending_control
pre_task_id / audio_vid / video_mode / fps / duration_ms / camera_strength
template_id / origin_history_id / origin_agentic_history_id / resolution
```

构造函数里还有 `firstFrameImage` / `endFrameImage` / `i2vOpt` / `v2vOpt` /
`multiFrames` / `unifiedEditInput` / `longVideoInput` 等子节点字段，
序列化名未逐个提取（是 node 类型，装饰器写法不同）。

## 4. 外层 `extend` 也不同

`xh.*.js` 里提交体的 `extend` 是：

```js
extend: { mVideoCommerceInfo, templateId, rootModel, historyOption }
```

而 `rootModel` 由 `abilities.gen_video.text_to_video_params.model_req_key` 推出
（`_getRootModel`）—— 视频的 `root_model` **等于模型 key**，与智能编辑的
`high_aes_general_v55` 是同一套逻辑。

## 5. 当初缺的三样，已全部补齐

`model_req_key` 的取值来自 **`POST /mweb/v1/video_generate/get_common_config`** ——
这是视频**专用**的 config 端点（`/mweb/v1/get_common_config?scene=video` 无效，
参数被忽略、返回的还是图片模型）。它一次给全：

* `model_list[]`：19 个模型的 `model_name` + `model_req_key` + `default_model_idx`
* `video_duration_display_range`：`{min_duration_ms: 4000, max_duration_ms: 15000}`
* `video_resolution_display_list`：`720p` / `1080p`

模型表（2026-11-20）：

| idx | 显示名 | model_req_key |
|---|---|---|
| 0 | 即梦 Seedance 2.5 Flash | `dreamina_seedance_45_flash` |
| 1 | 即梦 Seedance 2.5（样片模式） | `dreamina_seedance_45_pro_draft` |
| 2 | 即梦 Seedance 2.5 | `dreamina_seedance_45_pro` |
| **3** | **即梦 Seedance 2.0 mini**（默认） | **`dreamina_seedance_40_mini`** |
| 4 | 即梦 Seedance 2.0 Fast VIP | `dreamina_seedance_40_vision` |
| 5 | 即梦 Seedance 2.0 VIP | `dreamina_seedance_40_pro_vision` |
| 6 | 即梦 Seedance 2.0 Fast | `dreamina_seedance_40` |
| 7 | 即梦 Seedance 2.0 | `dreamina_seedance_40_pro` |
| 10/11 | 即梦 Seedance 1.0 / Fast | `dreamina_ic_generate_video_model_vgfm_3.0_pro` / `_fast` |
| 12/13 | Wan 3.0 Prime / Wan 3.0 | `dreamina_wan_30_prime` / `dreamina_wan_30` |
| 14/15 | HappyHorse 1.1 / 1.0 | `dreamina_happyhorse_v1_1` / `_v1_0` |
| 16/17/18 | Vidu Q4 / Q3 / Q2 | `dreamina_vidu_q4` / `_q3` / `_q2` |

网关当前只开放**有明确显示名的四个**（mini / Fast / 2.0 / 2.5）；
其余（含两个显示名为空的重制版）语义未取证，先不列。

## 5.0 旧记录：还缺什么（已解决）

| 缺的东西 | 为什么逆不出来 |
|---|---|
| `model_req_key` 的取值 | `get_common_config.model_list` 只有 22 个**图片**模型；视频模型在另一个端点（见 §5.1） |
| `video_aspect_ratio` 的格式 | 只是字符串透传，bundle 里没有字面量 |
| `min_version` / `min_features` | 由 `FeaturesMinVersion.VideoGenerateAbility` 常量给出，值未提取 |
| 哪些字段**必需** | 序列化器会省略 undefined，所以"没这个字段"和"字段为空"在上游眼里不同 |
| 提交后怎么轮询 | 应该同 `get_history_by_ids`，未验证 |

**为什么没抓成**：视频页的 UI 自动化连续失败 —— 页面有持续倒计时横幅导致
`observation` 句柄秒级失效，`bsk drag/click` 反复报 `control context no longer
belongs`；`abort` 掉一个错误匹配的 GET 还会让 SPA 重载。
`/tmp` 里的 chunk 缓存也会被系统清理，所以最后改成在页面内用
`performance.getEntriesByType('resource')` + `fetch` 检索 bundle（这条路可靠）。

## 5.1 已验证：**结构本身是通的**（2026-11-20 探测）

用本文的字段表构造了一份文生视频草稿，把 `model_req_key` 填成明显非法的值提交：

```
ret=18084 当前视频生成模型不可用，请选择其他模型后重试。
```

**这条错误的价值**：上游没有抱怨 `video_base_component` / `gen_video` /
`text_to_video_params` / `video_gen_inputs` 的任何结构问题，**只说模型不对**。
也就是说三层嵌套的字段名、`process_type=1`、`video_mode=2`、`fps`、`duration_ms`、
`resolution="720p"` 这些都被接受了。

这种探测**不计费**：参数错误在提交期被拒，任务不创建。所以"试探模型 key"可以
零成本做，只有 key 正确时才会真扣费（30 积分/5s）。

已排除的 key（都不计费）：

| key | 结果 |
|---|---|
| `invalid_probe_xyz` | 18084 |
| `seedance_45` | 18084 |
| `seedance_3_0` | 18084 |

`seedance_45` / `seedance_3_0` 是 bundle 里的字面量，但**不是**合法的
`model_req_key` —— 它们可能是营销文案里的模型代号，或某个更长 key 的子串。
UI 显示的是"即梦 Seedance 2.0 mini"，所以真 key 里大概率带 `2_0`。

又试了 7 个（`seedance_2_0_mini` / `seedance_2_0` / `seedance20_mini` / `seedance_mini` /
`dreamina_seedance_2_0_mini` / `seedance_v2_0_mini` / `seedance_2_0_flash`）全部 18084。
盲猜走不通，只能拿真值。

**`creation_agent` 不是生成链路**（2026-11-20 探测）：只有
`/creation_agent/motion/template/list` 存在（返回 `{templates, has_more}`），
`/video/generate`、`/generate`、`/chat`、`/task/create`、`/config`、`/model/list`
**全部 404**。所以视频生成仍走 `aigc_draft/generate` + `video_base_component`（§1-4 那套），
Agent 页面只是把 UI 换成了对话式外观。

## 5.2 新版视频页走的是 `creation_agent`（不是 aigc_draft）

`bsk network` 抓到视频页请求 `/mweb/v1/creation_agent/motion/template/list`。
当前视频页是 **Agent 对话式布局**（`/ai-tool/generate?...ai_feature_name=video`），
与我逆的 `aigc_draft/generate` + `video_base_component` **不是同一条链路**。

两种可能：
1. 旧版表单链路仍在服务端保留（§5.1 的 18084 支持这个判断 —— 端点认这套草稿）；
2. 新版改走 `creation_agent` 的任务模型，结构未探。

网关要接视频，**优先走第 1 条**：契约已知、结构已验证，只差一个 key。

## 5.3 为什么 UI 自动化抓不到（2026-11-20 逐条试过）

这一节是"别再白试"的清单。视频页的**表单在跨域 iframe 里**，加上页面高频重渲染：

| 方法 | 结果 |
|---|---|
| 页面内 hook `XMLHttpRequest` | 拦不到 —— 请求从 iframe 发出，主文档的 hook 看不见 |
| `document.querySelectorAll` 读按钮 | 读不到 —— 跨域 iframe 隔离（`bsk sync` 能读，因为它走 accessibility tree 跨 frame） |
| `bsk fill` 输入提示词 | 必失败 `target changed while clearing` —— 富文本编辑器清空时重建 DOM |
| `bsk press` 逐字符 | 每次都要 observation，而 observation 秒级失效 |
| `bsk click @ref` | `element not visible (no click quads)`；换 tab 后 `target was removed from its document` |
| `bsk click --capture --image-x/y` | 需要 `screenshot --ref` 的 capture id，而目标 ref 正是"无几何"那个 |
| 隐藏倒计时横幅让观察稳定 | 隐藏了 6 个元素，仍失效（页面还有其它动画） |
| 后台 tab（`--no-focus`） | iframe 不渲染 ⇒ 所有元素无几何 |

**唯一可靠的一条**：`bsk debug start` 在网络层记录（跨 frame，含 `request_body`），
但它只能**记录已发生的请求**，不能替你触发点击。所以最后一步必须有人点一次。

## 6. 要接着做，最短路径

1. 在页面里 hook **只记录不拦截**（`window.__cap` 存 body，不 `abort`），
   手动点一次"生成"（会真扣费，UI 显示 30 积分/5s）—— 一次就够，
   拿到 `model_req_key` / `aspect_ratio` / `min_version` / 完整字段集。
2. 用本文的字段表构造草稿，与抓包逐字段对拍（`tests/test_postedit.py` 里
   已有三套对拍工具可复用）。
3. 走网关提交，与 canonical 视频契约对齐：`POST /contents/generations/tasks`
   只回 id → `GET /tasks/{id}` 查状态（规范见 skill `video-adapter`）。

⚠️ 视频是**计费最重**的一类（30 积分/5s 起，是智能超清的 1.6 倍），
任何验证都要先确认成本；构造错误的草稿在提交期被拒通常不计费，
但"提交成功但生成失败"是计费的。
