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

## 日常更新

```bash
# 本机改完后同步源码（排除本地数据、虚拟环境、编辑器状态与 .env）
cd /Users/macstudio/Desktop/wb/jimengcn
COPYFILE_DISABLE=1 tar czf - --exclude='.venv' --exclude='data' --exclude='.git' \
  --exclude='__pycache__' --exclude='.workbuddy' --exclude='.env' . \
  | ssh -i ~/.ssh/id_ed25519_auto_macstudio root@210.121.44.245 'tar xzf - -C /opt/jimeng2api'

ssh -i ~/.ssh/id_ed25519_auto_macstudio root@210.121.44.245 \
  'cd /opt/jimeng2api && docker compose -f docker-compose.prod.yml up -d --build'
```

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
