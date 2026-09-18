"""Task command API tests."""
from __future__ import annotations

from application.tasks import create_task


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


def test_cancel_unknown_task_returns_not_found(client):
    response = client.post("/api/tasks/task_missing/cancel")

    assert response.status_code == 404
