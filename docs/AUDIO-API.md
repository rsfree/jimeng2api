# 语音合成北向契约（`POST /v1/audio/speech`）

> 实现依据：`audio-adapter` skill §3–§9。能力矩阵的每一条都对应 2026-11-25 的真实实测，
> 实测细节见 [`UPSTREAM-TTS.md`](UPSTREAM-TTS.md)。
> 本部署声明优先于 OpenAI 参考取值 —— 客户端按本表传参，不要假设 OpenAI 的取值一定可用。

## 1. 端点

```
POST /v1/audio/speech
Authorization: Bearer sk-...
Content-Type: application/json
```

响应是**原始音频字节**（不是 JSON、不是 Base64），`Content-Type` 与真实编码一致。

## 2. 请求字段

| 字段 | 类型 | 必填 | 本部署取值 |
|---|---|---|---|
| `model` | string | 是 | `seed-audio` / `seed_audio` / `tts-1` / `tts-1-hd` |
| `input` | string | 是 | ≤ **1000** 字符；不得为空/纯空白/纯标点 |
| `voice` | string | 是 | 别名 `huopo`、`lively-female`，或上游数字音色 ID（10–20 位） |
| `response_format` | string | 否 | `mp3`（默认）/ `wav` / `pcm` |
| `speed` | number | 否 | **0.5 – 2.0**，默认 1.0；越界 400，不钳位 |
| `sample_rate` | int | 否 | 8000 / 16000 / 24000（默认）/ 32000 / 44100 / 48000 |
| `instructions` | string | 否 | **不支持** —— 传了即 400 |
| `stream_format` | string | 否 | 只接受 `audio`（默认）；`sse` 等 400 |
| 其他任意字段 | — | — | **一律 400**，`param` 指向该字段名 |

## 3. 响应

| `response_format` | `Content-Type` | 说明 |
|---|---|---|
| `mp3` | `audio/mpeg` | 24000 Hz 单声道 |
| `wav` | `audio/wav` | RIFF，16 bit 单声道 |
| `pcm` | `application/octet-stream` | 裸 PCM，无容器 |

成功响应额外带 `Content-Disposition: inline; filename="speech.<ext>"`（扩展名来自白名单）。

## 4. 错误信封

```json
{"error": {"message": "...", "type": "...", "param": null, "code": null}}
```

| HTTP | `type` | 场景 | `param` |
|---|---|---|---|
| 400 | `invalid_request_error` | 字段缺失/越界/未知字段/畸形 JSON | 出错字段名 |
| 401 | `authentication_error` | 缺失或无效 Key（`code=invalid_api_key`） | `null` |
| 413 | `invalid_request_error` | 请求体 > 1 MiB（`code=request_too_large`） | `null` |
| 429 | `rate_limit_error` | 上游限流（`code=rate_limit_exceeded`） | `null` |
| 502 | `api_error` | 上游失败、账号池无可用账号 | `null` |
| 504 | `api_error` | 上游超时 | `null` |

**与 `/v1/images/*` 的差异**：图片族沿用既有的两键信封（`code` + `message`），
语音端点使用完整四键。这是刻意隔离 —— 图片族已在生产，改形状会破坏存量客户端。

## 5. 示例

```bash
# 默认 mp3
curl -s -X POST http://127.0.0.1:8300/v1/audio/speech \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"seed-audio","input":"你好，世界。","voice":"huopo"}' \
  -o speech.mp3

# 2 倍速 + 48k wav
curl -s -X POST http://127.0.0.1:8300/v1/audio/speech \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"seed-audio","input":"加速示例","voice":"huopo",
       "response_format":"wav","sample_rate":48000,"speed":2.0}' \
  -o speech.wav
```

## 6. 计费与性能

- **0 积分**：走上游试听通道，不产生扣费。
- **同步**：一次请求直返音频，无 `task_id`、无轮询。
- 实测单次 ≈ **2.2–2.5 s**（50 字），与并发无关；上游到 64 并发无降级。
- 上游支持到 1500 字，但 1500 附近不稳定（1520 失败 / 1540 成功），故本部署收窄到 1000。

## 7. 自描述

```
GET /v1/audio/capabilities
```

免鉴权返回本部署的取值面（模型、音色、编码、采样率、speed 区间、字数上限、
`pitch: fixed-0`、`instructions: false`、`billing: free`）。

## 8. 已知限制

| 项 | 状态 |
|---|---|
| `pitch` | **不可调**（上游只接受 0），不对外暴露 |
| `instructions`（风格指令） | 不支持 |
| 流式（SSE / chunked） | 不支持，一次性返回完整音频 |
| `ogg` / `aac` / `flac` / `opus` | 上游直接拒收，不承诺 |
| 超过 1000 字 | 返回 400，由调用方切段拼接 |
| 音色数量 | **193 个**（去重后），全部来自 feed，已抽样验证可出声 |

## 8.1 与 audio-adapter 规范的差异（明确声明）

| 项 | 状态 | 原因 |
|---|---|---|
| 404 / 405 的信封 | 只有 `code` / `message` 两键 | 走项目全局 handler，与已在生产的图片族共用；单独改会波及存量客户端。这两个状态只出现在路径/方法拼错时，不含业务语义 |
| `POST /v1/audio/transcriptions`（ASR） | **不实现** | 上游没有语音识别能力，硬做只能是空壳 |
| TTS 未知 JSON 扩展字段 | 一律 400（不做透传映射） | 上游 `tts_generate` 只接受固定 4 个字段，多传即报错；透传没有意义 |
| 流式（SSE / chunked） | 不支持 | 上游是同步一次性返回 |

除此之外，§3–§9 的其余条目均已实现并有测试覆盖（201 项）。

## 9. 音色表与采集

**音色没有独立列表接口，它们混在首页推荐流 `/mweb/v1/feed` 的响应里。**
识别特征：`common_attr.effect_type == 209`，其中 `common_attr.id` 是音色 ID，
`common_attr.title` 是音色名。一次响应可取 208 条，去重后 193 个。

交叉验证：feed 里「活泼女声」的 id 是 `7366933865698431515`，与 `tts_generate`
实际可用 ID 完全一致。

```
data/voices.json                # {"音色名": "数字ID"}，启动时合并进内置项
scripts/refresh_voices.py       # 刷新：读 feed → 落盘（一次 HTTP）
scripts/collect_voices.py       # 旧方案：点 UI 试听，已废弃（上游会限流成 2 项）
```

- 支持**中文音色名**直接作为 `voice` 传参：`{"voice":"紫薇"}` 实测 200。
- 也支持上游数字 ID 直传。
- 改完文件重启进程，或调用 `tts.reload_voices()`。

### 音色查询与元数据

```
GET /v1/audio/voices?gender=&age=&accent=&category=&style=&q=
```

| 维度 | 取值 |
|---|---|
| `gender` | male / female / child / unknown |
| `age` | child / teen / young / adult / senior |
| `accent` | standard / dongbei / tianjin / henan / shandong / guangxi / yue / chongqing / changsha / shanghai / taiwan / xian / beijing / hongkong / bayarea / japanese |
| `category` | general / dialect / screen_role / anime_role / scenario |
| `style` | 温柔 / 活泼 / 御姐 / 磁性 / 正统 / 萝莉 / 搞笑 / 悲伤 … |
| `q` | 名字关键字 |

响应带 `data[]`（每条含 `name`/`id` 与全部元数据）和 `facets`（各维度取值与数量，
可直接渲染筛选器）。元数据来自 `data/voice_meta.json`，由
`scripts/build_voice_meta.py` 生成（上游 feed 的 description/tags 全空，只能按名字推）。

完整清单见 [`VOICE-CATALOG.md`](VOICE-CATALOG.md)（按类型分组的表格）。

样例音色：紫薇、华妃、黛玉、容嬷嬷、四郎、猴哥、熊二、如来佛祖、天津小哥、东北老铁、
粤语男声、重庆小伙、动漫海绵、知性女声、温柔淑女、磁性男主播…（共 193 个）
