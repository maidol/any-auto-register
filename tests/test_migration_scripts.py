from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "tools" / "backup_migration.sh"
RESTORE_SCRIPT = ROOT / "tools" / "restore_migration.sh"


def _sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES ('preserved')")


def _write_manifest(package: Path) -> None:
    paths = [package / "restore.sh"]
    for base in (package / "snapshot", package / "extra"):
        if base.exists():
            paths.extend(path for path in base.rglob("*") if path.is_file())
    files = [
        {
            "path": path.relative_to(package).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "type": "file",
        }
        for path in sorted(paths)
    ]
    (package / "MANIFEST.sha256").write_text(json.dumps({"format": 1, "files": files}))


def _fake_docker(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "docker.log"
    log.write_text("")
    state = bin_dir / "docker-state"
    state.write_text("running\n")
    script = bin_dir / "docker"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\n"
        "if [[ \"$1\" == compose ]]; then shift; fi\n"
        "case \"$1\" in\n"
        "  version) exit 0 ;;\n"
        "  config) [[ \"${2:-}\" == --services ]] && printf '%s\\n' \"$COMPOSE_SERVICES\" ;;\n"
        "  ps)\n"
        "    if [[ \"$*\" == *'--services'* ]]; then\n"
        "      [[ $(cat \"$DOCKER_STATE\") == running ]] && printf '%s\\n' \"$RUNNING_SERVICES\"\n"
        "    elif [[ \"$*\" == *'-q'* ]]; then printf 'fake-container\\n'; fi ;;\n"
        "  stop) printf 'stopped\\n' > \"$DOCKER_STATE\" ;;\n"
        "  start|up) [[ ${FAIL_START:-0} != 1 ]] || exit 1; printf 'running\\n' > \"$DOCKER_STATE\" ;;\n"
        "  inspect) printf '%s\\n' \"$DOCKER_MOUNTS\" ;;\n"
        "  exec) [[ -f \"$LEGACY_DB\" ]] ;;\n"
        "  cp)\n"
        "    case \"$2\" in\n"
        "      *:/app/account_manager.db) cp \"$LEGACY_DB\" \"$3\" 2>/dev/null || exit 1 ;;\n"
        "      *) exit 1 ;;\n"
        "    esac ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
    return log


def _run_backup(
    project: Path,
    output: Path,
    docker_bin: Path,
    legacy_db: Path,
    *,
    services: str = "app",
    running_services: str = "app",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        PATH=f"{docker_bin}{os.pathsep}{env['PATH']}",
        DOCKER_LOG=str(docker_bin / "docker.log"),
        DOCKER_STATE=str(docker_bin / "docker-state"),
        DOCKER_MOUNTS=json.dumps([{"Type": "bind", "Source": str(project / "data"), "Destination": "/app/data"}]),
        LEGACY_DB=str(legacy_db),
        COMPOSE_SERVICES=services,
        RUNNING_SERVICES=running_services,
    )
    return subprocess.run(
        ["bash", str(BACKUP_SCRIPT), str(output)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_backup_archives_untracked_project_files_and_sqlite_data(tmp_path: Path) -> None:
    project = tmp_path / "production"
    (project / "tools").mkdir(parents=True)
    (project / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n")
    (project / "rebuild-my.sh").write_text("custom deployment helper\n")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("not part of migration\n")
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "cached.pyc").write_text("cache\n")
    _sqlite_db(project / "data" / "account_manager.db")
    legacy_db = tmp_path / "legacy.db"
    _sqlite_db(legacy_db)
    output = tmp_path / "downloads"
    output.mkdir()
    docker_bin = tmp_path / "bin"
    log = _fake_docker(docker_bin)

    result = _run_backup(project, output, docker_bin, legacy_db)

    assert result.returncode == 0, result.stderr
    archive = next(output.glob("*.tar.gz"))
    assert archive.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        assert "snapshot/rebuild-my.sh" in names
        assert "snapshot/data/account_manager.db" in names
        assert "extra/legacy-app-account_manager.db" in names
        assert not any(name.startswith("snapshot/.git/") for name in names)
        assert not any("__pycache__" in name for name in names)
        assert "restore.sh" in names
        manifest = bundle.extractfile("MANIFEST.sha256")
        assert manifest is not None
        entries = manifest.read().decode()
        assert hashlib.sha256(b"custom deployment helper\n").hexdigest() in entries
    assert "stop" in log.read_text()
    assert "start app" in log.read_text()


def test_backup_restores_running_services_when_sqlite_is_invalid(tmp_path: Path) -> None:
    project = tmp_path / "production"
    (project / "tools").mkdir(parents=True)
    (project / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n")
    (project / "data").mkdir()
    (project / "data" / "account_manager.db").write_text("not sqlite")
    output = tmp_path / "downloads"
    output.mkdir()
    docker_bin = tmp_path / "bin"
    log = _fake_docker(docker_bin)

    result = _run_backup(project, output, docker_bin, tmp_path / "missing.db")

    assert result.returncode != 0
    assert "SQLite integrity check failed" in result.stderr
    assert "start app" in log.read_text()
    assert not list(output.glob("*.tar.gz"))


def test_backup_refuses_when_a_compose_service_is_not_running(tmp_path: Path) -> None:
    project = tmp_path / "production"
    (project / "tools").mkdir(parents=True)
    (project / "docker-compose.yml").write_text(
        "services:\n  app:\n    build: .\n  worker:\n    image: worker\n"
    )
    _sqlite_db(project / "data" / "account_manager.db")
    output = tmp_path / "downloads"
    output.mkdir()
    docker_bin = tmp_path / "bin"
    log = _fake_docker(docker_bin)

    result = _run_backup(
        project,
        output,
        docker_bin,
        tmp_path / "missing.db",
        services="app\nworker",
        running_services="app",
    )

    assert result.returncode != 0
    assert "all Compose services must be running" in result.stderr
    assert "compose stop" not in log.read_text()


def test_restore_installs_verified_snapshot_and_starts_compose(tmp_path: Path) -> None:
    package = tmp_path / "package"
    (package / "snapshot" / "data").mkdir(parents=True)
    shutil.copy2(RESTORE_SCRIPT, package / "restore.sh")
    (package / "snapshot" / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n")
    (package / "snapshot" / "custom.sh").write_text("custom file\n")
    _sqlite_db(package / "snapshot" / "data" / "account_manager.db")
    _write_manifest(package)
    target = tmp_path / "restored"
    docker_bin = tmp_path / "bin"
    log = _fake_docker(docker_bin)
    env = os.environ.copy()
    env.update(
        PATH=f"{docker_bin}{os.pathsep}{env['PATH']}",
        DOCKER_LOG=str(log),
        DOCKER_STATE=str(docker_bin / "docker-state"),
        LEGACY_DB=str(tmp_path / "missing.db"),
        DOCKER_MOUNTS="[]",
        COMPOSE_SERVICES="app",
        RUNNING_SERVICES="app",
    )

    result = subprocess.run(
        ["bash", str(package / "restore.sh"), str(target)],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (target / "custom.sh").read_text() == "custom file\n"
    assert (target / "data" / "account_manager.db").stat().st_mode & 0o777 == 0o600
    assert "up --build -d" in log.read_text()
    assert result.stdout.startswith("Restore complete:")


def test_restore_rejects_non_empty_target_without_modifying_it(tmp_path: Path) -> None:
    package = tmp_path / "package"
    (package / "snapshot" / "data").mkdir(parents=True)
    shutil.copy2(RESTORE_SCRIPT, package / "restore.sh")
    (package / "snapshot" / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n")
    _sqlite_db(package / "snapshot" / "data" / "account_manager.db")
    _write_manifest(package)
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep")
    docker_bin = tmp_path / "bin"
    log = _fake_docker(docker_bin)
    env = os.environ.copy()
    env.update(PATH=f"{docker_bin}{os.pathsep}{env['PATH']}", DOCKER_LOG=str(log), DOCKER_STATE=str(docker_bin / "docker-state"))

    result = subprocess.run(
        ["bash", str(package / "restore.sh"), str(target)],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "target directory is not empty" in result.stderr
    assert marker.read_text() == "keep"
    assert "up --build -d" not in log.read_text()


def test_restore_verifies_manifest_before_starting_compose(tmp_path: Path) -> None:
    package = tmp_path / "package"
    (package / "snapshot").mkdir(parents=True)
    shutil.copy2(RESTORE_SCRIPT, package / "restore.sh")
    snapshot = package / "snapshot"
    (snapshot / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n")
    (snapshot / "changed.txt").write_text("tampered\n")
    _write_manifest(package)
    manifest = json.loads((package / "MANIFEST.sha256").read_text())
    next(item for item in manifest["files"] if item["path"] == "snapshot/changed.txt")["sha256"] = "0" * 64
    (package / "MANIFEST.sha256").write_text(json.dumps(manifest))
    target = tmp_path / "new-target"
    docker_bin = tmp_path / "bin"
    log = _fake_docker(docker_bin)
    env = os.environ.copy()
    env.update(PATH=f"{docker_bin}{os.pathsep}{env['PATH']}", DOCKER_LOG=str(log), DOCKER_STATE=str(docker_bin / "docker-state"))

    result = subprocess.run(
        ["bash", str(package / "restore.sh"), str(target)],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "checksum verification failed" in result.stderr
    assert "up --build -d" not in log.read_text()
    assert not target.exists()


def test_backup_keeps_completed_archive_if_services_fail_to_restart(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "production"
    (project / "tools").mkdir(parents=True)
    (project / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n")
    _sqlite_db(project / "data" / "account_manager.db")
    output = tmp_path / "downloads"
    output.mkdir()
    docker_bin = tmp_path / "bin"
    log = _fake_docker(docker_bin)
    monkeypatch.setenv("FAIL_START", "1")

    result = _run_backup(project, output, docker_bin, tmp_path / "missing.db")

    assert result.returncode != 0
    assert "did not restart" in result.stderr
    assert len(list(output.glob("*.tar.gz"))) == 1
    assert "compose stop" in log.read_text()
