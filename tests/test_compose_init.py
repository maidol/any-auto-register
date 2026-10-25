"""docker-compose.yml 要给 app 服务开 init: true。

入口脚本最后 `exec uvicorn`，uvicorn 成了容器里的 1 号进程，但它不回收别人的子进程。
每次任务关掉 Camoufox，forkserver / Utility / Socket / RDD 这几个子进程会过继给 1 号进程，
没人回收就留成僵尸进程（生产实测：10 次浏览器启动后 40 个僵尸，父进程都是 uvicorn）。
init: true 让 docker 在容器里放 tini 当 1 号进程，由它回收。
"""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def test_app_service_runs_with_init():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    assert services["app"].get("init") is True
