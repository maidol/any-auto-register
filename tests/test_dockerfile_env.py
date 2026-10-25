"""Dockerfile 运行阶段要设 PYTHONUNBUFFERED=1，print 才会立即进 docker logs。

不设时 stdout 接的是管道，print 先进缓冲区，攒满约 8KB 才写出，
启动日志（含「SOLVER_AUTOSTART 已关闭」那一行）在 docker logs 里长时间看不到。
放在 `COPY . .` 之后：改动只让最后几层重建，pip install、camoufox fetch 那几层的缓存不失效。
"""
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
LINES = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
ENV_LINE = "ENV PYTHONUNBUFFERED=1"


def test_runtime_stage_sets_pythonunbuffered():
    last_from = max(i for i, line in enumerate(LINES) if line.startswith("FROM "))
    assert ENV_LINE in LINES[last_from:]


def test_pythonunbuffered_comes_after_code_copy():
    assert ENV_LINE in LINES
    assert LINES.index(ENV_LINE) > LINES.index("COPY . .")
