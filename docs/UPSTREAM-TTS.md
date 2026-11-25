# 上游能力：同步 TTS（`/mweb/v1/tts_generate`）

> 事实口径：本文所有数字来自 **2026-11-25 真实调用**（账号 #1，maestro）。
> 标注 **[实]** = 实测通过；**[界]** = 实测出边界；**[未]** = 未验证。

## 1. 端点

```
POST /mweb/v1/tts_generate
Content-Type: application/json
Cookie: sessionid=…
```

请求体：

```json
{
  "text": "要合成的文本",
  "id_info": {"id": "<音色ID>", "item_platform": 4},
  "audio_config": {
    "format": "mp3",
    "sample_rate": 24000,
    "speech_rate": 0,
    "pitch_rate": 0
  }
}
```

响应：

```json
{"ret":"0","errmsg":"success","data":{"data":"<base64 音频>"}}
```

**同步**：无 submit_id、无轮询。返回的是 base64 音频，不是 URL。

## 2. 计费：**0 积分** [实]

余额 6190 → 6190，多次调用均无扣费记录。这是试听通道，不走计费。

对比：异步 `aigc_draft/generate`（`generate_type: generate_tts`）的 `extend` 里带
`amount: 1`，1 积分/次，且必须传数字 voiceId（`voice_name` 填中文名实测 `generate_failed`）。

## 3. 性能 [实]

单次 50 字文本：**P50 ≈ 2.2–2.6 s**，与并发无关。

## 4. 并发 [实]

| 并发 | 成功 | P50 | 备注 |
|---|---|---|---|
| 1 | 2/2 | 2573 ms | |
| 2 | 4/4 | 2442 ms | |
| 4 | 8/8 | 2160 ms | |
| 8 | 16/16 | 2175 ms | P95 4765 ms |
| 16 | 32/32 | 2250 ms | |
| 32 | 32/32 | 2383 ms | max 10.2 s |
| 64 | 64/64 | 2462 ms | max 5.6 s |

**64 并发 100% 成功，无限流迹象**。更高并发 [未] 测（避免触发风控）。

## 5. 文本长度 [界]

| 字数 | 结果 | 耗时 | 产物 |
|---|---|---|---|
| 1 | OK | 2.4 s | 13.9 KB / 0.7 s |
| 60 | OK | 2.4 s | 219 KB / 11 s |
| 250 | OK | 4.4 s | 887 KB / 44 s |
| 500 | OK | 6.2 s | 1.78 MB / 89 s |
| 1000 | OK | 11.0 s | 3.55 MB / 178 s |
| 1200 | OK | 12.7 s | 4.29 MB |
| 1500 | OK | 15.7 s | 5.35 MB |
| 1520 | **FAIL** | — | ret=3010 |
| 1540 | OK | — | 5.50 MB |
| 1560 | **FAIL** | — | ret=3010 |
| 1580 | OK | — | 5.66 MB |
| 1600 | **FAIL**（65 s 后） | — | ret=3010 |

**1500 字以上不稳定**：1520 失败但 1540 成功、1560 失败但 1580 成功 —— 不是硬性字数限制，
更像是服务端合成超时/资源竞争。**工程上建议单次 ≤ 1000 字**，超长自己切段拼接。

## 6. 枚举值 [界]

### `format`

| 值 | 结果 | 产物头 |
|---|---|---|
| `mp3` | OK | MP3 frame |
| `wav` | OK | RIFF/WAV，16 bit 单声道 |
| `pcm` | OK | 裸 PCM（无头） |
| `ogg` | **FAIL** `ret=1000 invalid parameter` | — |
| `aac` | **FAIL** `ret=1000 invalid parameter` | — |

### `sample_rate`

8000 / 16000 / 24000 / 32000 / 44100 / 48000 **全部生效**（用 WAV 头逐档验证，返回值与请求值精确相等）。

注意：MP3 输出是固定码率，**字节数不随采样率变化**，别用文件大小推断采样率 —— 用 WAV 或解码后验证。

### `speech_rate`

| 值 | 结果 | 相对时长 |
|---|---|---|
| -100 | **FAIL** invalid | — |
| -50 | OK | 198%（8.16 s / 4.11 s） |
| 0 | OK | 100%（4.11 s） |
| 50 | OK | 68%（2.77 s） |
| 100 | OK | 52%（2.12 s） |
| 200 | **FAIL** invalid | — |

**有效区间 -50 ~ 100**。

### `pitch_rate`

| 值 | 结果 |
|---|---|
| 0 | OK |
| -100 / -50 / 50 / 100 | **全部 FAIL** `invalid parameter` |

**只接受 0**，音高不可调。

### `id_info.item_platform`

| 值 | 结果 |
|---|---|
| 1 | OK |
| 4 | OK（UI 默认） |
| 0 | FAIL `ret=3010` |

## 7. 异常输入 [界]

| 输入 | 结果 |
|---|---|
| 空字符串 / 纯空格 / 纯标点 | **FAIL** `ret=1000 invalid parameter` |
| 英文 | OK 3.9 s |
| 中英混合 | OK 2.4 s |
| emoji | OK 1.6 s（emoji 的读法未核） |
| 纯数字 | OK 2.4 s |
| 多行（`\n`） | OK 2.7 s |
| 非法 voiceId（`999999999999`） | FAIL `ret=3010` |

## 8. 音色 ID 的获取

音色库每个卡片的「播放」按钮会发一次本接口请求，body 的 `id_info.id` 即该音色 ID。
逐个点试听即可批量采集。已知：

| 音色 | ID |
|---|---|
| 活泼女声 | `7366933865698431515` |

## 9. 接入建议

- 对 OpenAI 兼容：`POST /v1/audio/speech` → 本接口，`voice` 字段映射到 `id_info.id`，
  `response_format` 映射 `format`，`speed` 映射 `speech_rate`（注意只支持 -50~100）。
- 文本先按 ≤1000 字切段，避免 1500 字附近的不稳定区间。
- `pitch_rate` 固定 0，不要对外暴露。
