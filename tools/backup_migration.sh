#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
  printf 'Usage: %s [output-directory]\n' "$0" >&2
}

die() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
  usage
  exit 0
fi
if (( $# > 1 )); then
  usage
  exit 2
fi

PROJECT_DIR="$(pwd -P)"
[[ -f "$PROJECT_DIR/docker-compose.yml" || -f "$PROJECT_DIR/compose.yml" || -f "$PROJECT_DIR/compose.yaml" ]] || die "run this script from the Compose project directory"
[[ -f "$PROJECT_DIR/data/account_manager.db" ]] || die "data/account_manager.db was not found"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v tar >/dev/null 2>&1 || die "tar is required"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose is required"
fi

OUTPUT_DIR="${1:-$(dirname "$PROJECT_DIR")}"
mkdir -p -- "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
python3 - "$PROJECT_DIR" "$OUTPUT_DIR" <<'PY' || exit 1
import pathlib
import sys
project, output = map(pathlib.Path, sys.argv[1:])
try:
    pathlib.Path(output).relative_to(pathlib.Path(project))
except ValueError:
    raise SystemExit(0)
raise SystemExit("Error: output directory must be outside the Compose project")
PY

mapfile -t SERVICES < <("${COMPOSE[@]}" config --services 2>/dev/null) || die "could not read Compose services"
(( ${#SERVICES[@]} > 0 )) || die "Compose declares no services"
if ! printf '%s\n' "${SERVICES[@]}" | grep -qx app; then
  die "the current migration script expects an app service"
fi
RUNNING_SERVICES="$("${COMPOSE[@]}" ps --services --filter status=running 2>/dev/null)" || die "could not inspect running Compose services"
[[ -n "$RUNNING_SERVICES" ]] || die "no running Compose services were found"
DECLARED_SORTED="$(printf '%s\n' "${SERVICES[@]}" | LC_ALL=C sort)"
RUNNING_SORTED="$(printf '%s\n' "$RUNNING_SERVICES" | LC_ALL=C sort)"
[[ "$DECLARED_SORTED" == "$RUNNING_SORTED" ]] || die "all Compose services must be running for a complete backup"
mapfile -t APP_CONTAINERS < <("${COMPOSE[@]}" ps -q app 2>/dev/null) || die "could not inspect the app container"
(( ${#APP_CONTAINERS[@]} == 1 )) || die "exactly one running app container is required"
mapfile -t CONTAINERS < <("${COMPOSE[@]}" ps -q 2>/dev/null) || die "could not inspect Compose containers"
(( ${#CONTAINERS[@]} > 0 )) || die "no running Compose containers were found"

for container in "${CONTAINERS[@]}"; do
  mounts="$(docker inspect --format '{{json .Mounts}}' "$container" 2>/dev/null)" || die "could not inspect a container mount"
  if ! MOUNTS_JSON="$mounts" python3 - "$PROJECT_DIR" <<'PY'
import json
import os
import pathlib
import sys
root = pathlib.Path(sys.argv[1]).resolve()
try:
    mounts = json.loads(os.environ["MOUNTS_JSON"])
except Exception:
    raise SystemExit(1)
for mount in mounts or []:
    if mount.get("Type") != "bind":
        raise SystemExit(1)
    source = pathlib.Path(mount.get("Source", "")).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        raise SystemExit(1)
PY
  then
    die "a container has an external or non-bind data mount; refusing an incomplete backup"
  fi
done

APP_CONTAINER="${APP_CONTAINERS[0]}"
LEGACY_DB_PRESENT=0
if docker exec "$APP_CONTAINER" sh -c 'test -f /app/account_manager.db' >/dev/null 2>&1; then
  LEGACY_DB_PRESENT=1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$OUTPUT_DIR/any-auto-register-migration-$STAMP.tar.gz"
[[ ! -e "$ARCHIVE" ]] || die "backup archive already exists: $ARCHIVE"
TEMP_ARCHIVE="$(mktemp "$OUTPUT_DIR/.migration-backup.XXXXXX")"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/migration-stage.XXXXXX")"
STOPPED=0
FINALIZED=0

restore_services() {
  status=$?
  trap - EXIT
  if (( STOPPED )) && [[ -n "$RUNNING_SERVICES" ]]; then
    if ! "${COMPOSE[@]}" start $RUNNING_SERVICES >/dev/null 2>&1; then
      printf 'Error: backup finished, but the original Compose services did not restart\n' >&2
      status=1
    fi
  fi
  rm -rf -- "$STAGE"
  rm -f -- "$TEMP_ARCHIVE"
  if (( status != 0 && ! FINALIZED )); then
    rm -f -- "$ARCHIVE"
  fi
  exit "$status"
}
trap restore_services EXIT

STOPPED=1
"${COMPOSE[@]}" stop >/dev/null 2>&1 || die "could not stop Compose services cleanly"

check_sqlite() {
  python3 - "$1" <<'PY'
import sqlite3
import sys
try:
    with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if result and result[0] == "ok" else 1)
PY
}

check_sqlite "$PROJECT_DIR/data/account_manager.db" || die "SQLite integrity check failed for data/account_manager.db"
mkdir -p "$STAGE/extra"
if (( LEGACY_DB_PRESENT )); then
  docker cp "$APP_CONTAINER:/app/account_manager.db" "$STAGE/extra/legacy-app-account_manager.db" >/dev/null 2>&1 || die "could not copy the legacy container database"
  check_sqlite "$STAGE/extra/legacy-app-account_manager.db" || die "SQLite integrity check failed for the legacy container database"
fi
cp -- "$(dirname "${BASH_SOURCE[0]}")/restore_migration.sh" "$STAGE/restore.sh" || die "could not stage the restore script"

python3 - "$PROJECT_DIR" "$STAGE" <<'PY' || die "could not create the backup manifest"
import hashlib
import json
import os
import pathlib
import sys

project, stage = map(pathlib.Path, sys.argv[1:])
excluded = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "logs"}
records = []
def add(path: pathlib.Path, archive_path: str) -> None:
    if path.is_symlink():
        value = os.readlink(path).encode()
        kind = "symlink"
    elif path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        records.append({"path": archive_path, "sha256": digest.hexdigest(), "type": "file"})
        return
    else:
        return
    records.append({"path": archive_path, "sha256": hashlib.sha256(value).hexdigest(), "type": kind})

for parent, dirs, files in os.walk(project, followlinks=False):
    dirs[:] = sorted(d for d in dirs if d not in excluded and not d.startswith(".venv"))
    base = pathlib.Path(parent)
    for name in sorted(files):
        path = base / name
        relative = path.relative_to(project).as_posix()
        if name.endswith((".pyc", ".pyo")) or name.endswith(".log"):
            continue
        add(path, f"snapshot/{relative}")
for parent, dirs, files in os.walk(stage / "extra", followlinks=False):
    for name in sorted(files):
        path = pathlib.Path(parent) / name
        add(path, path.relative_to(stage).as_posix())
restore = stage / "restore.sh"
add(restore, "restore.sh")
manifest = {"format": 1, "files": sorted(records, key=lambda item: item["path"])}
(stage / "MANIFEST.sha256").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY

# Exclude generated and local-only files; keep every other project file, including untracked files and .env.
tar -czf "$TEMP_ARCHIVE" \
  --exclude='.git' --exclude='*/.git' \
  --exclude='*/.venv*' --exclude='*/node_modules' \
  --exclude='*/__pycache__' --exclude='*/.pytest_cache' --exclude='*/.mypy_cache' --exclude='*/.ruff_cache' \
  --exclude='*/logs' --exclude='*.pyc' --exclude='*.pyo' --exclude='*.log' \
  --transform='s,^[.]/,snapshot/,' -C "$PROJECT_DIR" . \
  -C "$STAGE" restore.sh MANIFEST.sha256 extra || die "could not create the backup archive"
chmod 600 "$TEMP_ARCHIVE"
mv -- "$TEMP_ARCHIVE" "$ARCHIVE" || die "could not finalize the backup archive"
FINALIZED=1
printf 'Backup created: %s\n' "$ARCHIVE"
printf 'Sensitive data may be included; keep this file private.\n'
