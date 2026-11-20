# jimeng2api · ai-prod 部署

线上入口：**https://jimengcn.1task.cn** → nginx → `127.0.0.1:39076` → 容器 `:8300`

| 项 | 值 |
|---|---|
| 主机 | `root@210.121.44.245`（ai-prod，首尔） |
| 目录 | `/opt/jimeng2api`（源码 + `docker-compose.prod.yml` + `data/`） |
| 镜像 | `jimeng2api:0.1.0`（`WITH_LOGIN=1`，含 Playwright + Chromium） |
| 端口 | `127.0.0.1:39076`（只绑回环，公网一律走 nginx） |
| 域名 | `jimengcn.1task.cn`（A → 210.121.44.245，TTL 600） |
| 证书 | `/www/server/panel/vhost/cert/jimengcn.1task.cn/`（acme.sh + LE，EC-256，webroot `/www/wwwroot/acme`） |
| 管理员 Key | 按「`sk-` + 二级域名」约定生成，**只写在服务器 `/opt/jimeng2api/.env`**（权限 600，不入库、不进 README） |
| 面板 | `/panel/` 应用自带登录页，输入管理员 Key 即可；`/panel/api/` 用 admin_key 直连 |
| 运维面 | `/docs`、`/openapi.json`、`/stats` 在入口层 `return 404` |

> 本服务是**国内版**即梦（上游 `jimeng.jianying.com`，aid 513695）。
> 旧的 `jimeng.1task.cn` 是**国际版**，两者不是同一套上游，互不影响。
>
> 管理员 Key 只在库里没有 admin key 时由环境变量生效（见 `security.bootstrap`）。
> 要换 Key：停容器 → `mv data data.bak-<ts>` → 改服务器 `.env` 里的
> `JIMENG2API_ADMIN_KEY` → `up -d`。

## 首次部署踩到的两个硬坑

1. **`worker_class = "gthread"` 直接 500**
   FastAPI 是 ASGI 应用，gthread/sync 属 WSGI worker，会把 app 当 WSGI 调用 →
   `TypeError: FastAPI.__call__() missing 1 required positional argument: 'send'`，
   每个请求都 500。必须用 `uvicorn.workers.UvicornWorker`（workers 仍为 1）。
   同步 `def` 端点的并发由 Starlette 的 anyio 线程池提供，不靠 gunicorn threads。

2. **`playwright install --with-deps` 在 slim 上 exit 127**
   `python:3.13-slim` 缺 `libatomic1`，playwright 自带的 node 驱动起不来
   （`error while loading shared libraries: libatomic.so.1`），而 `--with-deps`
   本身要先跑 node ⇒ 鸡生蛋。必须在 pip install 之前手动 `apt-get install libatomic1`。

## 发版（自动化）

`.github/workflows/release.yml` 是**混合模型**，三种入口：

| 入口 | 行为 |
|---|---|
| push 到 `main` | 读最新 `vX.Y.Z` tag，**自增 patch** → 写回版本 → 打 tag → 建 Release |
| 人工打 tag `vX.Y.Z` | 按该 tag 发（不递增），用于你想自己定版本号时 |
| `workflow_dispatch` + `tag` | **补发**已存在的 tag（Release 被删/建失败时用） |

版本号写回**四处**（`scripts/bump_version.py`，每条正则断言恰好命中 1 次）：

```
app/__init__.py                    __version__        ← /healthz 报的就是它
docker-compose.yml                 镜像 tag（本地开发）
docker-compose.prod.yml            镜像 tag（生产实际执行）
deploy/ai-prod/docker-compose.yml  镜像 tag（部署样例）
```

```bash
uv run python scripts/bump_version.py --check     # 校验四处一致
```

**跳过发版**：commit message 里带 `[skip release]`（纯文档/配置归位这类不发版的改动）。
注意它匹配的是**整条 message** —— 描述这个开关的提交自己也会被跳过。

## 发版 ≠ 部署

Release 只做版本标记，**不动服务器**。部署仍按下面的「日常更新」手动执行。
所以 push 后线上版本会落后一个版本号，这是预期行为，不是发版失败。

（要改成发版即部署，需要在仓库里配一条 SSH 私钥 secret 并加 deploy job —— 目前没做，
因为部署会重建容器、30 秒不可用，不适合无条件跟着每次 push 跑。）

## 日常更新

```bash
./scripts/deploy.sh
```

一条命令做三件事：同步源码 → `up -d --build` → 轮询 `/healthz` 直到版本号与
`app/__init__.py` 一致，最后打印镜像与容器状态。**任何一步失败即退出**，
不会留下"同步了但没重启"或"重启了但版本没变"的半成品。

等价的手动分解（脚本出问题时用）：

```bash
cd /Users/macstudio/Desktop/wb/jimengcn
COPYFILE_DISABLE=1 tar czf - --exclude='.venv' --exclude='data' --exclude='.git' \
  --exclude='__pycache__' --exclude='.workbuddy' --exclude='.env' . \
  | ssh -i ~/.ssh/id_ed25519_auto_macstudio root@210.121.44.245 'tar xzf - -C /opt/jimeng2api'

ssh -i ~/.ssh/id_ed25519_auto_macstudio root@210.121.44.245 \
  'cd /opt/jimeng2api && docker compose -f docker-compose.prod.yml up -d --build'

curl -s https://jimengcn.1task.cn/healthz   # 确认 version 与本地一致
```

`data/` 与 `.env` **永不覆盖**：一个是在跑的 SQLite 库（含 `-wal`/`-shm`），
一个是管理员 Key。脚本的 exclude 列表是这条约束的唯一实现，改它要慎重。

> 全量重建（含 Chromium）约 6 分钟；只改 `app/` 时代码层缓存失效，Playwright 层命中缓存，约 40 秒。

## 验收矩阵

```bash
curl -o /dev/null -w '%{http_code}\n' https://jimengcn.1task.cn/healthz     # 200
curl -o /dev/null -w '%{http_code}\n' https://jimengcn.1task.cn/v1/models   # 200
curl -o /dev/null -w '%{http_code}\n' https://jimengcn.1task.cn/panel/      # 200 → 应用自带登录页
curl -o /dev/null -w '%{http_code}\n' https://jimengcn.1task.cn/docs        # 404
curl -o /dev/null -w '%{http_code}\n' http://jimengcn.1task.cn/             # 301
```

`/healthz` 不依赖账号池，可作存活断言；`/v1/models` 在 0 账号时返回静态快照
（`"live": false`），不是故障。
