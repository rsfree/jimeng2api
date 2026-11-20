#!/usr/bin/env bash
# 部署到 ai-prod。发版（Release）不会自动部署，这是那一步的手动入口。
#
#   ./scripts/deploy.sh
#
# 做三件事：同步源码 → 重建镜像并重启 → 等健康并打印版本。
# 任何一步失败立即退出（set -e），不会留下"同步了但没重启"的半成品。
set -euo pipefail

HOST="${DEPLOY_HOST:-root@210.121.44.245}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/id_ed25519_auto_macstudio}"
DIR="${DEPLOY_DIR:-/opt/jimeng2api}"
COMPOSE="docker-compose.prod.yml"
LOCAL_PORT_URL="http://127.0.0.1:39076/healthz"

cd "$(dirname "$0")/.."
EXPECT="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' app/__init__.py | head -1)"
echo "==> 目标版本 $EXPECT → $HOST:$DIR"

# 1) 同步源码。data/ 与 .env 绝不覆盖：一个是在跑的库，一个是凭据。
echo "==> 同步源码"
COPYFILE_DISABLE=1 tar czf - \
  --exclude='.venv' --exclude='data' --exclude='.git' \
  --exclude='__pycache__' --exclude='.workbuddy' --exclude='.env' \
  --exclude='*.pyc' --exclude='.DS_Store' . \
  | ssh -i "$KEY" -o ConnectTimeout=20 "$HOST" "tar xzf - -C $DIR 2>/dev/null"

# 2) 重建并重启
echo "==> 重建镜像"
ssh -i "$KEY" -o ConnectTimeout=20 "$HOST" \
  "cd $DIR && docker compose -f $COMPOSE up -d --build 2>&1 | tail -4"

# 3) 等健康。容器有自己的 healthcheck，但这里等的是**应用真的能应答**
echo "==> 等待健康"
for i in $(seq 1 30); do
  body="$(ssh -i "$KEY" -o ConnectTimeout=10 "$HOST" \
          "curl -s --max-time 5 $LOCAL_PORT_URL" 2>/dev/null || true)"
  if printf '%s' "$body" | grep -q "\"version\":\"$EXPECT\""; then
    echo "==> 就绪：$body"
    ssh -i "$KEY" "$HOST" \
      "docker ps --filter name=jimeng2api --format '    镜像 {{.Image}}   {{.Status}}'" || true
    exit 0
  fi
  sleep 2
done

echo "!! 30 次探测内没拿到期望版本 $EXPECT，最后的响应：${body:-（空）}" >&2
echo "   排查：ssh -i $KEY $HOST 'docker logs --tail 60 jimeng2api'" >&2
exit 1
