import logging

from sqlmodel import Session, select

from application import tasks as tasks_module
from core.db import TaskLog, engine
from platforms.chatgpt.register import RegistrationEngine


def test_append_task_event_persists_masked_message_and_detail():
    event = tasks_module.append_task_event(
        "task-redact",
        "email=u@example.com password=Secret123!",
        detail={"access_token": "access-live", "state": "failed"},
    )

    assert "u@example.com" not in event["message"]
    assert "Secret123!" not in event["message"]
    assert event["detail"]["access_token"] == "[REDACTED]"
    assert event["detail"]["state"] == "failed"


def test_task_logger_masks_stdout(monkeypatch, capsys):
    monkeypatch.setattr(tasks_module, "append_task_event", lambda *args, **kwargs: None)
    logger = tasks_module.TaskLogger("task-redact")

    logger.log("email=u@example.com token=live-token", detail={"password": "Secret123!"})
    captured = capsys.readouterr().out

    assert "u@example.com" not in captured
    assert "live-token" not in captured
    assert "Secret123!" not in captured


def test_save_task_log_masks_email_error_and_detail():
    tasks_module._save_task_log(
        "chatgpt",
        "u@example.com",
        "failed",
        error="token=live-token",
        detail={"password": "Secret123!"},
    )

    with Session(engine) as session:
        row = session.exec(select(TaskLog)).one()

    assert "u@example.com" not in row.email
    assert "live-token" not in row.error
    assert "Secret123!" not in row.detail_json


def test_registration_engine_log_masks_all_sinks(caplog):
    callback_messages = []
    registration = RegistrationEngine.__new__(RegistrationEngine)
    registration.logs = []
    registration.callback_logger = callback_messages.append
    registration.task_uuid = None

    with caplog.at_level(logging.INFO, logger="platforms.chatgpt.register"):
        registration._log("email=u@example.com password=Secret123! token=live-token")

    assert "u@example.com" not in registration.logs[0]
    assert "Secret123!" not in registration.logs[0]
    assert "live-token" not in registration.logs[0]
    assert callback_messages == [registration.logs[0].split("] ", 1)[1]]
    assert all(secret not in caplog.text for secret in ("u@example.com", "Secret123!", "live-token"))
