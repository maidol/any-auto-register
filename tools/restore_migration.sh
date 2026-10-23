#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
  printf 'Usage: %s target-directory\n' "$0" >&2
}

die() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
  usage
  exit 0
fi
if (( $# != 1 )); then
  usage
  exit 2
fi

PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET="$(python3 - "$1" <<'PY'
import pathlib
import sys
print(pathlib.Path(sys.argv[1]).expanduser().resolve())
PY
)"
[[ "$TARGET" != / ]] || die "refusing to restore to the filesystem root"
[[ -d "$PACKAGE_DIR/snapshot" && -f "$PACKAGE_DIR/MANIFEST.sha256" ]] || die "run this script from the extracted migration package"

python3 - "$PACKAGE_DIR" "$TARGET" <<'PY' || die "checksum verification failed"
import hashlib
import json
import pathlib
import sys

package = pathlib.Path(sys.argv[1]).resolve()
target = pathlib.Path(sys.argv[2]).resolve()
try:
    target.relative_to(package)
except ValueError:
    pass
else:
    raise SystemExit("target must be outside the migration package")
try:
    package.relative_to(target)
except ValueError:
    pass
else:
    raise SystemExit("target must not contain the migration package")

try:
    manifest = json.loads((package / "MANIFEST.sha256").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError
    expected = {}
    for item in manifest["files"]:
        relative = pathlib.PurePosixPath(item["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError
        if relative.parts[0] not in {"snapshot", "extra"} and str(relative) != "restore.sh":
            raise ValueError
        if str(relative) in expected:
            raise ValueError
        expected[str(relative)] = item
    actual = set()
    for base_name in ("snapshot", "extra"):
        base = package / base_name
        if base.exists():
            for path in base.rglob("*"):
                if path.is_file() or path.is_symlink():
                    actual.add(path.relative_to(package).as_posix())
    if (package / "restore.sh").is_file():
        actual.add("restore.sh")
    if actual != set(expected):
        raise ValueError
    for relative, item in expected.items():
        path = package / relative
        resolved = path.resolve()
        resolved.relative_to(package)
        if item.get("type") == "symlink":
            if not path.is_symlink():
                raise ValueError
            value = path.readlink().as_posix().encode()
        elif item.get("type") == "file":
            if path.is_symlink() or not path.is_file():
                raise ValueError
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            value = None
        else:
            raise ValueError
        actual_digest = hashlib.sha256(value).hexdigest() if value is not None else digest.hexdigest()
        if actual_digest != item.get("sha256"):
            raise ValueError
except Exception:
    raise SystemExit(1)
PY

[[ -f "$PACKAGE_DIR/snapshot/docker-compose.yml" || -f "$PACKAGE_DIR/snapshot/compose.yml" || -f "$PACKAGE_DIR/snapshot/compose.yaml" ]] || die "the package has no Compose file"
[[ -f "$PACKAGE_DIR/snapshot/data/account_manager.db" ]] || die "the package has no account database"
python3 - "$PACKAGE_DIR/snapshot/data/account_manager.db" <<'PY' || die "SQLite integrity check failed for the account database"
import sqlite3
import sys
try:
    with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if result and result[0] == "ok" else 1)
PY

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose is required"
fi

PARENT="$(dirname "$TARGET")"
mkdir -p -- "$PARENT"
if [[ -e "$TARGET" ]]; then
  [[ -d "$TARGET" && -z "$(find "$TARGET" -mindepth 1 -maxdepth 1 -print -quit)" ]] || die "target directory is not empty"
  rmdir -- "$TARGET" || die "could not prepare the empty target directory"
fi
INSTALL_STAGE="$(mktemp -d "${TARGET}.restore.XXXXXX")"
cp -a "$PACKAGE_DIR/snapshot/." "$INSTALL_STAGE/" || die "could not stage the project files"
if [[ -f "$PACKAGE_DIR/extra/legacy-app-account_manager.db" ]]; then
  mkdir -p "$INSTALL_STAGE/data/migration-legacy"
  cp -- "$PACKAGE_DIR/extra/legacy-app-account_manager.db" "$INSTALL_STAGE/data/migration-legacy/account_manager.db" || die "could not stage the legacy database"
  chmod 600 "$INSTALL_STAGE/data/migration-legacy/account_manager.db"
  printf 'Legacy container database preserved separately at data/migration-legacy/account_manager.db.\n'
fi
if [[ -d "$INSTALL_STAGE/data" ]]; then
  chmod 700 "$INSTALL_STAGE/data"
  [[ ! -f "$INSTALL_STAGE/data/account_manager.db" ]] || chmod 600 "$INSTALL_STAGE/data/account_manager.db"
fi
find "$INSTALL_STAGE" -maxdepth 1 -type f -name '.env*' -exec chmod 600 {} +
mv -T -- "$INSTALL_STAGE" "$TARGET" || die "could not place the restored project"
INSTALL_STAGE=""

cd "$TARGET"
"${COMPOSE[@]}" up --build -d >/dev/null 2>&1 || die "Compose build/start failed; inspect the target project with docker compose logs"
mapfile -t SERVICES < <("${COMPOSE[@]}" config --services 2>/dev/null) || die "could not read restored Compose services"
mapfile -t RUNNING < <("${COMPOSE[@]}" ps --services --filter status=running 2>/dev/null) || die "could not inspect restored services"
if (( ${#SERVICES[@]} == 0 || ${#RUNNING[@]} != ${#SERVICES[@]} )); then
  die "not all Compose services are running; inspect the target project with docker compose ps"
fi
printf 'Restore complete: %s\n' "$TARGET"
