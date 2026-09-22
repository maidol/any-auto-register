#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE="app"
REPO_DIR="$(pwd)"
DATA_DIR="$REPO_DIR/data"
DB_FILE="$DATA_DIR/account_manager.db"
BACKUP_DIR="$DATA_DIR/backups"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

echo "==> Repository: $REPO_DIR"

command -v docker >/dev/null 2>&1 || {
    echo "错误：未找到 docker"
    exit 1
}

docker compose version >/dev/null 2>&1 || {
    echo "错误：未找到 docker compose"
    exit 1
}

mkdir -p "$DATA_DIR" "$BACKUP_DIR"

if [ -f "$DB_FILE" ]; then
    cp -p "$DB_FILE" "$BACKUP_DIR/account_manager.db.$TIMESTAMP.bak"
    echo "==> 已备份数据库：$BACKUP_DIR/account_manager.db.$TIMESTAMP.bak"
else
    echo "==> 未发现已有数据库，将创建新的数据库"
fi

echo "==> 检查 Compose 配置"
COMPOSE_CONFIG="$(mktemp)"
trap 'rm -f "$COMPOSE_CONFIG"' EXIT

docker compose config > "$COMPOSE_CONFIG"

grep -Fq 'ACCOUNT_MANAGER_DATABASE_URL: sqlite:////app/data/account_manager.db' "$COMPOSE_CONFIG" || {
    echo "错误：Compose 未使用 /app/data/account_manager.db"
    exit 1
}

grep -Fq 'target: /app/data' "$COMPOSE_CONFIG" || {
    echo "错误：Compose 未挂载 /app/data"
    exit 1
}

GIT_SHA="$(git rev-parse --short HEAD)"
echo "==> 构建镜像，提交：$GIT_SHA"

docker compose build \
    --pull \
    --build-arg "APP_VERSION=$GIT_SHA" \
    "$SERVICE"

echo "==> 重建并启动容器"
docker compose up -d --force-recreate "$SERVICE"

CONTAINER_ID="$(docker compose ps -q "$SERVICE")"

if [ -z "$CONTAINER_ID" ]; then
    echo "错误：未获取到容器 ID"
    docker compose ps
    exit 1
fi

echo "==> 等待容器启动"
READY=0

for i in $(seq 1 60); do
    STATUS="$(docker inspect -f '{{.State.Status}}' "$CONTAINER_ID" 2>/dev/null || true)"

    if [ "$STATUS" = "running" ]; then
        READY=1
        echo "容器已运行"
        break
    fi

    if [ "$STATUS" = "exited" ] || [ "$STATUS" = "dead" ]; then
        echo "错误：容器启动失败，状态：$STATUS"
        docker compose logs --tail=160 "$SERVICE"
        exit 1
    fi

    sleep 2
done

if [ "$READY" -ne 1 ]; then
    echo "错误：等待容器启动超时"
    docker compose logs --tail=160 "$SERVICE"
    exit 1
fi

echo "==> 检查持久化挂载"
docker inspect "$CONTAINER_ID" \
    -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'

MOUNT_SOURCE="$(docker inspect "$CONTAINER_ID" \
    -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}')"

if [ "$MOUNT_SOURCE" != "$DATA_DIR" ]; then
    echo "错误：/app/data 未挂载到 $DATA_DIR"
    echo "实际挂载源：$MOUNT_SOURCE"
    exit 1
fi

echo "==> 检查应用健康状态"
HEALTHY=0

for i in $(seq 1 30); do
    if docker compose exec -T "$SERVICE" \
        curl -fsS --max-time 5 http://127.0.0.1:8000/api/health; then
        echo
        HEALTHY=1
        break
    fi

    sleep 2
done

if [ "$HEALTHY" -ne 1 ]; then
    echo "错误：健康检查失败"
    docker compose logs --tail=160 "$SERVICE"
    exit 1
fi

echo "==> 宿主机数据库"
if [ -f "$DB_FILE" ]; then
    stat "$DB_FILE"
else
    echo "警告：宿主机暂未看到数据库文件"
fi

echo "==> 容器状态"
docker compose ps

echo "==> 构建、重建和健康检查完成"
