# 提示词增强（`POST /v1/prompts/enhance`）

> 实测日期 2026-11-25。免费、同步（内部轮询约 5–20 秒）。
> 上游：`/mweb/v1/prompt_enhancement/create` + `/query`。

## 1. 端点

```
POST /v1/prompts/enhance
Authorization: Bearer sk-...
Content-Type: application/json
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `prompt` | 是 | ≤2000 字符 |
| `model` | 否 | 默认 `jimeng-video`；只用于取增强上下文，不产生计费 |
| `ratio` | 否 | `16:9`（默认）/ `9:16` / `1:1` / `4:3` / `3:4` / `21:9` |
| `resolution` | 否 | `720p`（默认）/ `1080p` |
| `duration` | 否 | 4–15 秒，默认 5 |
| `context` | 否 | `T2V`（默认，已实测）/ `R2V`（参考生视频）/ `F2V`（帧生视频） |
| `target` | 否 | `video`（默认，返回完整导演脚本）/ `image`（裁剪成图片提示词） |

响应：

```json
{"original": "一只猫在草地上奔跑",
 "enhanced": "# 透传结果\n## 主体\n幼年橘猫…\n## 场景\n…\n## 风格\n…\n## 音乐\n…\n## 情节\n…",
 "pe_task_id": "video-pe-1bb9c200-…",
 "credits": 0}
```

## 2. 效果

输入「一只猫在草地上奔跑」，输出不是简单同义改写，而是**导演级脚本**：

```
## 主体   幼年橘猫，浅棕毛发带深棕条纹，腹部及四肢为白色，尾巴有环纹，黄色眼睛。
## 场景   阳光明媚的午后草地，生长有蒲公英和白色小花，远处有茂密树木，天空湛蓝。
## 风格   低角度手持Vlog拍摄风格，暖调明亮，活泼欢乐氛围。
## 音乐   节奏轻快，以尤克里里或吉他清脆拨弦为主的乐曲。
## 情节   低角度跟拍，镜头带轻微呼吸感，幼年橘猫从画面左侧跑入…用前爪拍了拍蒲公英，
         发出轻微啪嗒声…消失在远处草丛中。
```

含**镜头运动**（低角度跟拍、轻微呼吸感）和**声音设计**（啪嗒声、脚步声），
可直接喂给视频模型。

## 2.1 `target=image`：给图片用的裁剪模式

上游**只服务视频**，但输出里的视觉部分可以抽出来喂图片。`target=image` 会：

- 按 `## 标题` 分节，**白名单优先**保留 `主体/场景/风格/画面/构图/色彩/光线/氛围/细节/材质/背景`；
- 剔除 `音乐/情节/声音/台词/对白/音效/旁白/节奏/运镜/转场/时长/时间线`；
- 压成一段连续描述；没有 `##` 分节时**原样返回**（宁可少裁，不要误删）；
- 同时把完整视频脚本放在 `enhanced_video` 里，调用方可以自己取舍。

实测同一个输入：

```
input  「一只猫在草地上奔跑」
target=video → # 透传结果 / ## 主体 / ## 场景 / ## 风格 / ## 音乐 / ## 情节 …
target=image → 幼年橘猫，浅棕毛发带深棕条纹，腹部和下巴为白色。
               傍晚的绿色草地，生长有白色小野花，远处有树木，天空有云彩，夕阳洒下金色光芒。
               暖黄色调，温暖治愈，充满生机。
```

⚠️ 这是**本网关的裁剪封装**，不是上游的图片增强能力 —— 图片侧没有
`support_prompt_enhancement`。残留的「慢镜头」这类词偶尔会漏过，因为它在「风格」段里。

## 3. 上游协议（踩过的三个坑）

```json
POST /mweb/v1/prompt_enhancement/create
{"items": [{
  "pe_task_id": "video-pe-<uuid>",
  "content_generator_type": "video",
  "prompt_context": "T2V",
  "input_snapshot": {
    "pe_client_input": {"user_prompt": "…", "media_desc": ""},
    "model_req_key": "dreamina_seedance_40_mini",
    "generation_params": "{\"feature\":\"omniReference\",\"aspectRatio\":\"16:9\",…}",
    "prompt_segments": [{"type": "text", "text": "…"}],
    "rich_prompt_snapshot": "{\"feature\":\"omniReference\",\"contents\":[…]}"
  }
}]}
```

| 坑 | 说明 |
|---|---|
| **字段是 snake_case** | `pe_task_id` / `content_generator_type` / `input_snapshot`。JS 源码里是 camelCase（经过属性名转换），照抄源码会 `ret=1000` |
| **两个字段是 JSON 字符串** | `generation_params` 和 `rich_prompt_snapshot` 不是对象，要 `json.dumps` |
| **`prompt_context` 是 `"T2V"`** | 大写；`content_generator_type` 才是小写 `"video"` |

查询结果（第二步）：

```json
POST /mweb/v1/prompt_enhancement/query
{"offset":0,"limit":10,
 "list_filter":{"content_generator_type":"video",
   "status_list":["created","running","succeeded","failed","timeout"]},
 "include_input_snapshot":false,"include_deleted":false}
```

- 结果在 `data.tasks[]`（不是 `items`/`task_list`），按 `pe_task_id` 匹配；
- 取 `task.pe_result.optimized_prompt`；
- **`status_list` 必须精确匹配** —— 多传一个 `"cancelled"` 会让整个查询 `ret=1000`，
  表现为"永远查不到结果 / 超时"。

## 4. 定位方法（下次找类似接口可复用）

UI 上的入口是**视频面板**里 `aria-label="优化提示词"` 的按钮，**不在图片面板**。
一开始在三处找错方向：图片面板按钮、`prompt_enhancement/query`（它只是查历史任务列表）、
以及盲猜参数（12 组全部 1000）。

最终解法：在 175 个 JS chunk 里搜接口名，找到 `createTasks(e)` 的调用点，
从代码里读出字段结构，再回 UI 点一次真实按钮确认。

## 5. 已知边界

| 项 | 状态 |
|---|---|
| 计费 | **0 积分**（余额 4749 → 4749） |
| 耗时 | 实测 19.5 秒（含轮询） |
| `context` | 上游枚举三种：`T2V`（已实测）/ `R2V`（参考图，未测）/ `F2V`（首尾帧，未测） |
| 适用生成类型 | **只支持视频** —— 实测 `content_generator_type=image` / `audio` 均 `ret=1000` |
| 图片模型 | **不支持** —— 图片的 `get_common_config` 里没有 `support_prompt_enhancement`，这是视频特性 |
| 批量 | 上游 `items[]` 支持多条，北向目前一次一条 |
