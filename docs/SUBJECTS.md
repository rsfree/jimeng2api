# 主体（角色/商品）—— 只读列表

> 实测 2026-11-25。**只实现了查询**，创建/更新/删除未开放，原因见文末。

## 1. 端点

```
GET /v1/subjects?cursor=0&limit=20&source=mine
```

| 参数 | 说明 |
|---|---|
| `cursor` | 游标，默认 0（**不是 offset**） |
| `limit` | 1–50，默认 20 |
| `source` | 目前只支持 `mine` |

响应：

```json
{
  "object": "list",
  "subjects": [],
  "next_cursor": 0,
  "has_more": false
}
```

`subjects[]` 的字段**原样透传上游**（当前账号没有主体数据，元素结构未取证，
因此不做字段改名，避免按猜测的映射出错）。

## 2. 上游协议

```
POST /mweb/v1/dreamina_subject/get
{"cursor": 0, "limit": 20, "subject_source": "mine",
 "sort_field": 0, "sort_order": 0}      // 后两个可选
→ {"ret":"0","data":{"next_cursor", "has_more", "data_list"}}
```

**踩坑**：参数是 `cursor` 不是 `offset`。用 `offset` 会 `ret=1000`。
正确参数是在「资产和主体」页面点「主体」标签抓到的。

## 3. 上游还有哪些端点（未实现）

从 JS 里读到的完整清单：

| 端点 | 用途 | 状态 |
|---|---|---|
| `/dreamina_subject/get` | 列表 | ✅ 已实现 |
| `/dreamina_subject/create` | 创建主体 | ⛔ 未实现 |
| `/dreamina_subject/update` | 更新 | ⛔ 未实现 |
| `/dreamina_subject/delete` | 删除 | ⛔ 未实现 |
| `/dreamina_subject/generate_voice` | 给主体生成声音 | ⛔ 未实现 |
| `/dreamina_subject/get_gen_record_list` | 生成记录 | ⛔ 未实现 |
| `/dreamina_subject/update_gen_record` | 更新记录 | ⛔ 未实现 |

## 4. 为什么只做只读

`create` 需要上传参考图并走一套「主体生成」流程（可能计费、可能过审核、
可能受会员等级限制）。**没有真实素材无法验证**，而项目有一条硬规矩：
*清单里出现，就必须真能提交*。把没验证过的创建接口写进来，等于给调用方挖坑。

要开放创建，需要先走一遍 UI：上传一张图 → 建主体 → 抓 `create` 的请求体 →
确认计费 → 再实现。这一步还没有做。

## 5. 主体的用途

让同一角色/商品在多张生成图里保持一致。上游的做法是在提示词里用 `@主体名` 引用，
配合 `omniReference` 参数。本网关目前只提供「列出已有主体」，
引用方式需要调用方在 `prompt` 里自行写 `@名字`（上游是否认，未验证）。
