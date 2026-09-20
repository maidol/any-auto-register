"""Task command API tests."""
from __future__ import annotations

from application.tasks import (
    TASK_STATUS_CANCEL_REQUESTED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCEEDED,
    _mutate_task,
    create_task,
)
def test_task_history_serializes_cancellable_status(client):
    task = create_task(
        task_type="register",
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    _mutate_task(task["id"], lambda model: setattr(model, "status", TASK_STATUS_RUNNING))

    response = client.get("/api/tasks")

    assert response.status_code == 200
    item = next(item for item in response.json()["items"] if item["id"] == task["id"])
    assert item["cancellable"] is True
    assert item["terminal"] is False

    _mutate_task(task["id"], lambda model: setattr(model, "status", TASK_STATUS_SUCCEEDED))
    response = client.get(f"/api/tasks/{task['id']}")

    assert response.status_code == 200
    assert response.json()["cancellable"] is False
    assert response.json()["terminal"] is True


def test_cancel_pending_task(client):
    task = create_task(
        task_type="register",
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )

    response = client.post(f"/api/tasks/{task['id']}/cancel")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelled"
    assert data["cancellable"] is False


def test_cancel_running_task_requests_cancellation(client):
    task = create_task(
        task_type="register",
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    _mutate_task(task["id"], lambda model: setattr(model, "status", TASK_STATUS_RUNNING))

    response = client.post(f"/api/tasks/{task['id']}/cancel")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == TASK_STATUS_CANCEL_REQUESTED
    assert data["cancellable"] is True
    assert data["finished_at"] is None


def test_cancel_running_task_does_not_make_it_terminal(client):
    task = create_task(
        task_type="register",
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    _mutate_task(task["id"], lambda model: setattr(model, "status", TASK_STATUS_RUNNING))

    response = client.post(f"/api/tasks/{task['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] not in {
        "cancelled",
        "succeeded",
        "failed",
        "interrupted",
    }


def test_cancel_succeeded_task_is_noop(client):
    task = create_task(
        task_type="register",
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    _mutate_task(
        task["id"],
        lambda model: (
            setattr(model, "status", TASK_STATUS_SUCCEEDED),
            setattr(model, "error", "already finished"),
        ),
    )

    response = client.post(f"/api/tasks/{task['id']}/cancel")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == TASK_STATUS_SUCCEEDED
    assert data["error"] == "already finished"
    assert data["cancellable"] is False


def test_cancel_unknown_task_returns_not_found(client):
    response = client.post("/api/tasks/task_missing/cancel")

    assert response.status_code == 404
