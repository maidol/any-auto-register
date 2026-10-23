# Compose 生产迁移

这两个脚本用于将当前 Compose 项目打包到新主机：

- `backup_migration.sh`：在源主机短暂停机，生成权限为 `0600` 的 `.tar.gz` 包。
- `restore_migration.sh`：从已解压的包校验内容、还原项目并构建/启动 Compose 服务。

包中包含项目目录内的源码、Compose/Docker 构建文件、隐藏配置（例如 `.env`）、未跟踪的自定义文件和 `data/`。会排除 Git 元数据、虚拟环境、依赖缓存、日志及 Python 字节码。SQLite 数据库会在服务停止后做 `PRAGMA integrity_check`；备份清单会在还原前校验文件哈希。

## 源主机：备份

把最新的两个脚本放到生产项目的 `tools/` 目录，然后从项目根目录执行：

```bash
cd /path/to/any-auto-register
bash tools/backup_migration.sh /path/outside/the/project
```

脚本要求 Compose 配置中包含 `app` 服务且所有声明的服务都在运行；必须恰有一个运行中的 `app` 容器。所有容器数据挂载必须是项目目录内的 bind mount。遇到命名卷、项目外挂载或服务未启动时会停止并报错，避免生成不完整包。备份结束后会尝试恢复备份前运行的服务；如果服务无法重启，已生成的归档仍会保留并报错提示。

归档可能包含账号数据、令牌或凭据。脚本不会加密归档；请通过受信任的传输方式下载，并在迁移完成后安全删除不再需要的副本。

## 新主机：还原

新主机需安装 Docker Compose、Python 3 和构建依赖可访问的网络。将归档上传后，先解压到一个仅当前用户可访问的临时目录，再运行包内的还原脚本：

```bash
mkdir -m 700 /path/to/private-staging
tar -xzf /path/to/any-auto-register-migration-<timestamp>.tar.gz -C /path/to/private-staging
bash /path/to/private-staging/restore.sh /path/to/new/any-auto-register
```

目标目录必须不存在或为空。还原会校验文件清单及主数据库，然后以 `docker compose up --build -d` 构建并启动全部服务；构建失败时项目文件会保留在目标目录供排查。还原脚本不会覆盖非空目录，也不会执行 `docker compose down` 或清理数据卷。请在新主机使用独立项目目录；若同一 Docker 主机已存在同名 Compose 项目，`up` 可能更新该项目的容器。

若源容器还存在旧路径 `/app/account_manager.db`，归档会把它保存为 `extra/legacy-app-account_manager.db`；还原时单独放到目标目录的 `data/migration-legacy/account_manager.db`，**不会自动覆盖或合并**当前 `data/account_manager.db`。启动后应核查这份旧库是否包含需要保留的配置，并在单独备份后再决定如何合并。

该版本不导出 Docker 镜像；目标主机需要能按归档中的源码重新构建镜像。数据挂载若不符合上述支持范围，脚本会拒绝备份，而不是静默跳过。
