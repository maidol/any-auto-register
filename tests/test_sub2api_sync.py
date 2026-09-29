"""Sub2API 手动 / 定时导入。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from requests import Timeout
from sqlmodel import Session

from application import sub2api_sync as s2a
from core.db import AccountModel, engine
from core.config_store import config_store
from domain.accounts import AccountCreateCommand, AccountExportSelection, AccountUpdateCommand
from infrastructure.accounts_repository import AccountsRepository
from infrastructure.config_repository import ConfigRepository


def _create(email: str, *, access_token: str = "at_x", refresh_token: str = "rt_x") -> int:
    record = AccountsRepository().create(
        AccountCreateCommand(
            platform="chatgpt",
            email=email,
            password="pw",
            credentials={"access_token": access_token, "refresh_token": refresh_token},
        )
    )
    return record.id


def _configure(**extra: str) -> None:
    config_store.set_many({
        "sub2api_url": "http://s2a.local/",
        "sub2api_admin_key": "admin-key",
        "sub2api_default_group_id": "12",
        **extra,
    })


class FakeClient:
    def __init__(self, results: list[tuple[bool, str]] | None = None):
        self.results = list(results or [])
        self.pushed: list[str] = []
        self.groups: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.account_ids: dict[str, set[int]] = {}
        self.account_markers: dict[int, str] = {}
        self.next_id = 100
        self.group_bind_failures = 0
        self.reject_next_import = False
        self.refresh_failures = 0
        self.model_sync_failures = 0
        self.ambiguous_identity = False
        self.create_on_failure = False
        self.created_after_timeout: set[str] = set()
        self.last_credentials: dict[str, str] = {}

    def list_matching_account_ids(self, item, *, identity=None, import_marker=None):
        search_identity = identity or item.email
        account_ids = set(self.account_ids.get(search_identity, set()))
        if import_marker:
            if self.ambiguous_identity:
                self.account_markers[self.next_id] = import_marker
                account_ids.add(self.next_id)
                self.next_id += 1
            account_ids = {account_id for account_id in account_ids if self.account_markers.get(account_id) == import_marker}
        return account_ids

    def import_account(self, item, group_id, import_marker=""):
        self.pushed.append(item.email)
        self.groups.append(group_id)
        if self.reject_next_import:
            self.reject_next_import = False
            raise s2a.Sub2ApiImportRejected("import permission rejected")
        fingerprint = repr(item.credentials or [])
        previous = self.last_credentials.get(item.email)
        changed_payload = previous is not None and previous != fingerprint
        result = self.results.pop(0) if self.results else (True, "")
        if result[0] and (item.email not in self.created_after_timeout or changed_payload):
            account_id = self.next_id
            self.account_ids.setdefault(item.email, set()).add(account_id)
            self.account_markers[account_id] = import_marker
            self.next_id += 1
        elif not result[0] and self.create_on_failure:
            account_id = self.next_id
            self.account_ids.setdefault(item.email, set()).add(account_id)
            self.account_markers[account_id] = import_marker
            self.next_id += 1
            self.created_after_timeout.add(item.email)
            self.create_on_failure = False
        self.last_credentials[item.email] = fingerprint
        self.calls.append(("import", item.email))
        return result

    def find_new_account_id(self, item, known_ids, *, identity=None, import_marker=None):
        new_ids = self.list_matching_account_ids(
            item, identity=identity, import_marker=import_marker
        ) - known_ids
        return next(iter(new_ids)) if len(new_ids) == 1 else None

    def bind_account_group(self, account_id, group_id):
        self.calls.append(("bind_group", f"{account_id}:{group_id}"))
        if self.group_bind_failures:
            self.group_bind_failures -= 1
            raise ValueError("group bind failed")

    def refresh_account(self, account_id):
        self.calls.append(("refresh", str(account_id)))
        if self.refresh_failures:
            self.refresh_failures -= 1
            raise ValueError("refresh failed")

    def sync_upstream_models(self, account_id):
        self.calls.append(("models", str(account_id)))
        if self.model_sync_failures:
            self.model_sync_failures -= 1
            raise ValueError("model sync failed")


def _service(client: FakeClient) -> s2a.Sub2ApiSyncService:
    return s2a.Sub2ApiSyncService(client_factory=lambda _url, _key: client)


def _response(status: int, body) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = str(body)
    return resp


# ── 客户端 ───────────────────────────────────────────────────


def test_client_posts_wrapped_payload_with_admin_key_and_idempotency_key():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    ok_body = {"code": 0, "message": "success", "data": {"account_created": 1, "account_failed": 0}}

    with patch("application.sub2api_sync.requests.post", return_value=_response(200, ok_body)) as post:
        assert s2a.Sub2ApiClient("http://s2a.local/", "admin-key").import_account(item, "12") == (True, "")

    url = post.call_args.args[0]
    kwargs = post.call_args.kwargs
    assert url == "http://s2a.local/api/v1/admin/accounts/data"
    assert kwargs["headers"]["x-api-key"] == "admin-key"
    assert kwargs["headers"]["Idempotency-Key"].startswith("aar-sub2api-")
    assert kwargs["json"]["skip_default_group_bind"] is True
    assert kwargs["json"]["group_ids"] == [12]
    assert kwargs["json"]["data"]["accounts"][0]["name"] == "a@test.com"
    assert kwargs["json"]["data"]["proxies"] == []


def test_client_includes_import_marker_in_account_extra():
    account_id = _create("marked@test.com")
    item = AccountsRepository().get(account_id)
    response = _response(200, {"code": 0, "data": {"account_created": 1}})

    with patch("application.sub2api_sync.requests.post", return_value=response) as post:
        s2a.Sub2ApiClient("http://s2a.local", "admin-key").import_account(item, "12", "marker-123")

    payload = post.call_args.kwargs["json"]["data"]["accounts"][0]
    assert payload["extra"][s2a.IMPORT_MARKER_FIELD] == "marker-123"


def test_client_binds_group_with_account_update_only():
    client = s2a.Sub2ApiClient("http://s2a.local/", "admin-key")
    response = _response(200, {"code": 0, "data": {}})

    with patch("application.sub2api_sync.requests.put", return_value=response) as put:
        client.bind_account_group(11, "12")

    put.assert_called_once_with(
        "http://s2a.local/api/v1/admin/accounts/11",
        json={"group_ids": [12]},
        headers={"x-api-key": "admin-key"},
        timeout=s2a.REQUEST_TIMEOUT_SECONDS,
    )


def test_client_idempotency_key_is_stable_for_same_account():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    data = s2a._make_sub2api_json(item)
    assert s2a._idempotency_key(item, data, "12") == s2a._idempotency_key(item, data, "12")
    assert s2a._idempotency_key(item, data, "12") != s2a._idempotency_key(item, data, "13")
    assert len(s2a._idempotency_key(item, data, "12")) <= 128


def test_idempotency_key_stays_stable_within_marked_import_attempt():
    account_id = _create("attempt-key@test.com")
    item = AccountsRepository().get(account_id)
    first_data = s2a._sub2api_payload(item, "marker-123")
    changed_data = {**first_data, "updated": True}

    first_key = s2a._idempotency_key(item, first_data, "12", "marker-123")

    assert first_key == s2a._idempotency_key(item, changed_data, "12", "marker-123")
    assert first_key != s2a._idempotency_key(item, changed_data, "13", "marker-123")


def test_client_treats_http200_without_created_account_as_failure():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    body = {
        "code": 0,
        "message": "success",
        "data": {"account_created": 0, "account_failed": 1, "errors": [{"kind": "account", "message": "bad credentials"}]},
    }

    with patch("application.sub2api_sync.requests.post", return_value=_response(200, body)):
        with pytest.raises(s2a.Sub2ApiImportRejected, match="bad credentials"):
            s2a.Sub2ApiClient("http://s2a.local", "k").import_account(item, "12")


def test_client_reports_http_error_message():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    with patch("application.sub2api_sync.requests.post", return_value=_response(401, {"code": 401, "message": "invalid admin api key"})):
        with pytest.raises(s2a.Sub2ApiImportRejected, match="401.*invalid admin api key"):
            s2a.Sub2ApiClient("http://s2a.local", "k").import_account(item, "12")


def test_client_preserves_uncertain_in_progress_idempotency_conflict():
    account_id = _create("in-progress@test.com")
    item = AccountsRepository().get(account_id)

    with patch(
        "application.sub2api_sync.requests.post",
        return_value=_response(409, {"code": 409, "message": "request is still processing"}),
    ):
        ok, message = s2a.Sub2ApiClient("http://s2a.local", "k").import_account(item, "12")

    assert ok is False
    assert "still processing" in message


def test_client_skips_account_without_tokens_without_request():
    account_id = _create("empty@test.com", access_token="", refresh_token="")
    item = AccountsRepository().get(account_id)
    with patch("application.sub2api_sync.requests.post") as post:
        with pytest.raises(s2a.Sub2ApiImportRejected, match="缺少 access_token"):
            s2a.Sub2ApiClient("http://s2a.local", "k").import_account(item, "12")
    post.assert_not_called()


def test_known_import_rejection_clears_pending_snapshot():
    _configure()
    account_id = _create("empty-import@test.com", access_token="", refresh_token="")
    client = s2a.Sub2ApiClient("http://s2a.local", "admin-key")
    service = s2a.Sub2ApiSyncService(client_factory=lambda _url, _key: client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])
    accounts_response = _response(200, {"code": 0, "data": {"items": [], "pages": 1}})

    with patch("application.sub2api_sync.requests.get", return_value=accounts_response), patch(
        "application.sub2api_sync.requests.post"
    ) as post:
        result = service.sync_selected(selection)

    overview = AccountsRepository().get(account_id).overview
    assert result["failed"] == 1
    assert result["errors"][0]["stage"] == "import"
    assert not overview.get(s2a.IMPORT_GROUP_ID_KEY)
    assert not overview.get(s2a.IMPORT_KNOWN_IDS_KEY)
    assert not overview.get(s2a.IMPORT_FINGERPRINT_KEY)
    assert not overview.get(s2a.IMPORT_IDENTITY_KEY)
    assert not overview.get(s2a.IMPORT_MARKER_KEY)
    assert not s2a.Sub2ApiSyncService._has_pending_sub2api_work(AccountsRepository().get(account_id))
    post.assert_not_called()


def test_definite_rejection_of_retry_preserves_prior_uncertain_snapshot():
    _configure()
    account_id = _create("rejected-retry@test.com")
    client = FakeClient([(False, "request timed out")])
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    client.reject_next_import = True
    AccountsRepository().update(
        account_id,
        AccountUpdateCommand(credentials={"access_token": "at_rotated", "refresh_token": "rt_rotated"}),
    )
    second = service.sync_selected(selection)

    overview = AccountsRepository().get(account_id).overview
    assert first["failed"] == 1
    assert second["failed"] == 1
    assert overview[s2a.IMPORT_MARKER_KEY]
    assert overview[s2a.IMPORT_IDENTITY_KEY] == "rejected-retry@test.com"
    assert s2a.Sub2ApiSyncService._has_pending_sub2api_work(AccountsRepository().get(account_id))


# ── 手动导入 ─────────────────────────────────────────────────


def test_manual_sync_marks_success_and_never_pushes_twice():
    _configure()
    account_id = _create("a@test.com")
    client = FakeClient()
    service = _service(client)
    selection = lambda: AccountExportSelection(platform="chatgpt", ids=[account_id])  # noqa: E731

    first = service.sync_selected(selection())
    second = service.sync_selected(selection())

    assert client.pushed == ["a@test.com"]
    assert first["created"] == 1 and first["skipped"] == 0
    assert second["created"] == 0 and second["skipped"] == 1
    assert AccountsRepository().get(account_id).overview.get(s2a.SYNCED_AT_KEY)


def test_manual_import_binds_group_and_runs_refresh_then_model_sync():
    _configure()
    account_id = _create("grouped@test.com")
    client = FakeClient()

    result = _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))

    overview = AccountsRepository().get(account_id).overview
    assert client.groups == ["12"]
    assert client.calls == [
        ("import", "grouped@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
    ]
    assert overview["sub2api_group_bound_at"]
    assert overview[s2a.SYNCED_AT_KEY]
    assert overview[s2a.ACCOUNT_ID_KEY] == "100"
    assert overview[s2a.REFRESHED_AT_KEY]
    assert overview[s2a.MODELS_SYNCED_AT_KEY]
    assert result["created"] == 1
    assert result["refresh_failed"] == 0
    assert result["model_sync_failed"] == 0


def test_manual_import_binds_group_before_refresh_and_model_sync():
    _configure()
    account_id = _create("bind-order@test.com")
    client = FakeClient()

    result = _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))

    overview = AccountsRepository().get(account_id).overview
    assert client.calls == [
        ("import", "bind-order@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
    ]
    assert overview["sub2api_group_id"] == "12"
    assert overview["sub2api_group_bound_at"]
    assert result["group_bind_failed"] == 0


def test_group_binding_failure_retries_without_reimport_or_repeating_completed_stages():
    _configure()
    account_id = _create("bind-retry@test.com")
    client = FakeClient()
    client.group_bind_failures = 1
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    first_record = AccountsRepository().get(account_id)
    first_overview = first_record.overview
    assert s2a.Sub2ApiSyncService._has_pending_sub2api_work(first_record)
    second = service.sync_selected(selection)

    overview = AccountsRepository().get(account_id).overview
    assert first["group_bind_failed"] == 1
    assert not first_overview.get("sub2api_group_bound_at")
    assert second["group_bind_failed"] == 0
    assert overview["sub2api_group_bound_at"]
    assert client.calls == [
        ("import", "bind-retry@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
        ("bind_group", "100:12"),
    ]


def test_manual_sync_rejects_missing_default_group_before_import():
    _configure(sub2api_default_group_id="")
    account_id = _create("ungrouped@test.com")
    client = FakeClient()

    with pytest.raises(ValueError, match="默认分组"):
        _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))

    assert client.calls == []


def test_postprocess_retry_does_not_import_account_twice():
    _configure()
    account_id = _create("retry@test.com")
    client = FakeClient()
    client.refresh_failures = 1
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    first_overview = AccountsRepository().get(account_id).overview
    second = service.sync_selected(selection)

    assert first["created"] == 1 and first["refresh_failed"] == 1
    assert second["created"] == 0 and second["skipped"] == 1
    assert client.pushed == ["retry@test.com"]
    assert client.calls == [
        ("import", "retry@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
        ("refresh", "100"),
    ]
    overview = AccountsRepository().get(account_id).overview
    assert first_overview[s2a.SYNCED_AT_KEY]
    assert not first_overview.get(s2a.REFRESHED_AT_KEY)
    assert first_overview[s2a.MODELS_SYNCED_AT_KEY]
    assert overview[s2a.REFRESHED_AT_KEY]


def test_idempotent_import_replay_resolves_account_from_original_snapshot():
    _configure()
    account_id = _create("timeout@test.com")
    client = FakeClient([(False, "request timed out"), (True, "")])
    client.create_on_failure = True
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    config_store.set("sub2api_default_group_id", "13")
    second = service.sync_selected(selection)

    overview = AccountsRepository().get(account_id).overview
    assert first["failed"] == 1
    assert second["created"] == 1 and second["postprocess_failed"] == 0
    assert overview[s2a.ACCOUNT_ID_KEY] == "100"
    assert client.groups == ["12"]
    assert len(client.account_ids["timeout@test.com"]) == 1
    assert client.calls == [
        ("import", "timeout@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
    ]


def test_import_retry_after_credential_change_reconciles_without_duplicate_post():
    _configure()
    account_id = _create("changed-token@test.com")
    client = FakeClient([(False, "request timed out")])
    client.create_on_failure = True
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    AccountsRepository().update(
        account_id,
        AccountUpdateCommand(credentials={"access_token": "at_changed", "refresh_token": "rt_changed"}),
    )
    second = service.sync_selected(selection)

    overview = AccountsRepository().get(account_id).overview
    assert first["failed"] == 1
    assert second["created"] == 1 and second["postprocess_failed"] == 0
    assert overview[s2a.ACCOUNT_ID_KEY] == "100"
    assert len(client.account_ids["changed-token@test.com"]) == 1
    assert client.calls == [
        ("import", "changed-token@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
    ]


def test_pending_import_with_changed_token_and_no_remote_record_blocks_repost():
    _configure()
    account_id = _create("no-remote-record@test.com")
    client = FakeClient([(False, "request failed"), (True, "")])
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    AccountsRepository().update(
        account_id,
        AccountUpdateCommand(credentials={"access_token": "at_rotated", "refresh_token": "rt_rotated"}),
    )
    second = service.sync_selected(selection)

    overview = AccountsRepository().get(account_id).overview
    assert first["failed"] == 1
    assert second["failed"] == 1
    assert second["errors"][0]["stage"] == "import_reconcile"
    assert overview[s2a.IMPORT_RECONCILE_BLOCKED_KEY]
    assert not overview.get(s2a.ACCOUNT_ID_KEY)
    assert client.pushed == ["no-remote-record@test.com"]


def test_changed_pending_payload_is_blocked_before_second_post():
    _configure()
    account_id = _create("stable-key-retry@test.com")
    client = s2a.Sub2ApiClient("http://s2a.local", "admin-key")
    service = s2a.Sub2ApiSyncService(client_factory=lambda _url, _key: client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])
    accounts_response = _response(200, {"code": 0, "data": {"items": [], "pages": 1}})
    post_responses = [Timeout("timed out")]

    with patch("application.sub2api_sync.requests.get", return_value=accounts_response), patch(
        "application.sub2api_sync.requests.post", side_effect=post_responses
    ) as post:
        first = service.sync_selected(selection)
        AccountsRepository().update(
            account_id,
            AccountUpdateCommand(credentials={"access_token": "at_rotated", "refresh_token": "rt_rotated"}),
        )
        second = service.sync_selected(selection)

    assert first["failed"] == 1 and second["failed"] == 1
    assert second["errors"][0]["stage"] == "import_reconcile"
    assert post.call_count == 1
    overview = AccountsRepository().get(account_id).overview
    assert overview[s2a.IMPORT_MARKER_KEY]
    assert overview[s2a.IMPORT_RECONCILE_BLOCKED_KEY]


def test_pending_import_with_multiple_new_remote_records_pauses_without_repost():
    _configure()
    account_id = _create("ambiguous-retry@test.com")
    client = FakeClient([(False, "request failed")])
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    AccountsRepository().update(
        account_id,
        AccountUpdateCommand(credentials={"access_token": "at_rotated", "refresh_token": "rt_rotated"}),
    )
    client.account_ids["ambiguous-retry@test.com"] = {700, 701}
    second = service.sync_selected(selection)

    assert first["failed"] == 1
    assert second["failed"] == 1
    assert second["errors"][0]["stage"] == "import_reconcile"
    assert client.pushed == ["ambiguous-retry@test.com"]


def test_pending_import_reconciles_using_original_email_after_local_email_changes():
    _configure()
    account_id = _create("original-email@test.com")
    client = FakeClient([(False, "request timed out")])
    client.create_on_failure = True
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    with Session(engine) as session:
        account = session.get(AccountModel, account_id)
        account.email = "rotated-email@test.com"
        session.add(account)
        session.commit()
    AccountsRepository().update(
        account_id,
        AccountUpdateCommand(credentials={"access_token": "at_rotated", "refresh_token": "rt_rotated"}),
    )
    second = service.sync_selected(selection)

    assert first["failed"] == 1
    assert second["created"] == 1
    assert client.pushed == ["original-email@test.com"]
    assert len(client.account_ids["original-email@test.com"]) == 1
    assert not client.account_ids.get("rotated-email@test.com")


def test_pending_import_does_not_adopt_unrelated_new_same_email_account():
    _configure()
    account_id = _create("unrelated-same-email@test.com")
    client = FakeClient([(False, "request timed out")])
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    client.account_ids["unrelated-same-email@test.com"] = {700}
    AccountsRepository().update(
        account_id,
        AccountUpdateCommand(credentials={"access_token": "at_rotated", "refresh_token": "rt_rotated"}),
    )
    second = service.sync_selected(selection)

    assert first["failed"] == 1
    assert second["failed"] == 1
    assert second["errors"][0]["stage"] == "import_reconcile"
    assert client.pushed == ["unrelated-same-email@test.com"]
    assert client.account_ids["unrelated-same-email@test.com"] == {700}


def test_postprocess_retry_never_targets_a_different_sub2api_instance():
    _configure()
    account_id = _create("instance-change@test.com")
    client = FakeClient()
    client.model_sync_failures = 1
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    config_store.set("sub2api_url", "http://other-s2a.local")
    second = service.sync_selected(selection)
    pending = AccountsRepository().get(account_id).overview
    config_store.set("sub2api_url", "http://s2a.local/")
    third = service.sync_selected(selection)

    assert first["model_sync_failed"] == 1
    assert second["postprocess_failed"] == 1
    assert not pending.get(s2a.MODELS_SYNCED_AT_KEY)
    assert third["model_sync_failed"] == 0
    assert client.calls == [
        ("import", "instance-change@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
        ("models", "100"),
    ]


def test_instance_url_snapshot_survives_config_change_during_client_creation():
    _configure()
    account_id = _create("instance-race@test.com")
    client = FakeClient()
    client.refresh_failures = 1
    factory_calls = 0

    def client_factory(base_url, admin_key):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            config_store.set("sub2api_url", "http://other-s2a.local")
        return client

    service = s2a.Sub2ApiSyncService(client_factory=client_factory)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    origin = AccountsRepository().get(account_id).overview[s2a.INSTANCE_URL_KEY]
    second = service.sync_selected(selection)

    assert first["refresh_failed"] == 1
    assert origin == "http://s2a.local"
    assert second["postprocess_failed"] == 1
    assert client.calls == [
        ("import", "instance-race@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
    ]


def test_model_sync_retry_does_not_repeat_refresh_or_import():
    _configure()
    account_id = _create("models-retry@test.com")
    client = FakeClient()
    client.model_sync_failures = 1
    service = _service(client)
    selection = AccountExportSelection(platform="chatgpt", ids=[account_id])

    first = service.sync_selected(selection)
    second = service.sync_selected(selection)

    assert first["model_sync_failed"] == 1
    assert second["model_sync_failed"] == 0
    assert client.pushed == ["models-retry@test.com"]
    assert client.calls == [
        ("import", "models-retry@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
        ("models", "100"),
    ]


def test_ambiguous_import_identity_records_success_without_postprocessing():
    _configure()
    account_id = _create("ambiguous@test.com")
    client = FakeClient()
    client.ambiguous_identity = True

    result = _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))

    imported = AccountsRepository().get(account_id)
    overview = imported.overview
    assert imported.lifecycle_status == "invalid"
    assert overview[s2a.SYNCED_AT_KEY]
    assert not overview.get(s2a.ACCOUNT_ID_KEY)
    assert result["created"] == 1
    assert result["postprocess_failed"] == 1
    assert client.calls == [("import", "ambiguous@test.com")]


def test_manual_sync_failure_is_not_marked_and_retries_next_time():
    _configure()
    account_id = _create("a@test.com")
    client = FakeClient([(False, "boom")])
    service = _service(client)

    first = service.sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))
    second = service.sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))

    assert first["failed"] == 1 and first["errors"][0]["message"] == "boom"
    assert second["created"] == 1
    assert client.pushed == ["a@test.com", "a@test.com"]


def test_manual_sync_rejects_empty_selection_instead_of_pushing_everything():
    _configure()
    _create("a@test.com")
    _create("b@test.com")
    client = FakeClient()

    with pytest.raises(ValueError, match="勾选"):
        _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=[]))
    assert client.pushed == []


def test_manual_sync_requires_config():
    account_id = _create("a@test.com")
    client = FakeClient()
    with pytest.raises(ValueError, match="Sub2API"):
        _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))
    assert client.pushed == []


def test_manual_sync_stops_after_consecutive_failures():
    _configure()
    ids = [_create(f"u{i}@test.com") for i in range(5)]
    client = FakeClient([(False, "down")] * 5)

    result = _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=ids))

    assert result["aborted"] is True
    assert result["failed"] == s2a.MAX_CONSECUTIVE_FAILURES
    assert len(client.pushed) == s2a.MAX_CONSECUTIVE_FAILURES


def test_sync_endpoint_rejects_empty_selection(client):
    _configure()
    resp = client.post("/api/accounts/sync/sub2api", json={"platform": "chatgpt", "ids": []})
    assert resp.status_code == 400


def test_sync_endpoint_rejects_missing_default_group(client):
    _configure(sub2api_default_group_id="")
    account_id = _create("missing-group@test.com")
    response = client.post(
        "/api/accounts/sync/sub2api",
        json={"platform": "chatgpt", "ids": [account_id]},
    )
    assert response.status_code == 400
    assert "默认分组" in response.json()["detail"]


# ── 定时导入 ─────────────────────────────────────────────────


def test_auto_sync_disabled_does_nothing():
    _configure()
    _create("a@test.com")
    client = FakeClient()
    assert _service(client).sync_new_accounts() is None
    assert client.pushed == []


def test_auto_sync_only_pushes_accounts_created_after_enable():
    _configure(sub2api_auto_sync="1")
    _create("old@test.com")
    client = FakeClient()
    service = _service(client)

    first = service.sync_new_accounts()
    assert first["total"] == 0
    assert config_store.get(s2a.AUTO_SYNC_SINCE_KEY)
    assert client.pushed == []

    _create("new@test.com")
    second = service.sync_new_accounts()
    third = service.sync_new_accounts()

    assert client.pushed == ["new@test.com"]
    assert second["created"] == 1
    assert third["total"] == 0 and third["created"] == 0


def test_auto_sync_retries_pending_model_sync_after_status_changes():
    _configure(sub2api_auto_sync="1")
    service = _service(FakeClient())
    service.sync_new_accounts()
    account_id = _create("status-change@test.com")
    client = FakeClient()
    client.model_sync_failures = 1
    service = _service(client)

    first = service.sync_new_accounts()
    AccountsRepository().update(account_id, AccountUpdateCommand(lifecycle_status="invalid"))
    second = service.sync_new_accounts()

    assert first["created"] == 1 and first["model_sync_failed"] == 1
    assert second["skipped"] == 1 and second["model_sync_failed"] == 0
    assert client.calls == [
        ("import", "status-change@test.com"),
        ("bind_group", "100:12"),
        ("refresh", "100"),
        ("models", "100"),
        ("models", "100"),
    ]


def test_auto_sync_rejects_missing_default_group_without_importing():
    _configure(sub2api_auto_sync="1", sub2api_default_group_id="")
    config_store.set(s2a.AUTO_SYNC_SINCE_KEY, "2026-09-01T00:00:00+00:00")
    _create("scheduled@test.com")
    client = FakeClient()

    with pytest.raises(ValueError, match="默认分组"):
        _service(client).sync_new_accounts()

    assert client.calls == []


def test_auto_sync_disable_clears_start_point():
    _configure(sub2api_auto_sync="1")
    service = _service(FakeClient())
    service.sync_new_accounts()
    assert config_store.get(s2a.AUTO_SYNC_SINCE_KEY)

    config_store.set("sub2api_auto_sync", "0")
    assert service.sync_new_accounts() is None
    assert config_store.get(s2a.AUTO_SYNC_SINCE_KEY) == ""


def test_auto_sync_tick_respects_interval():
    _configure(sub2api_auto_sync="1", sub2api_sync_interval_minutes="10")
    calls = []

    class FakeService:
        def sync_new_accounts(self):
            calls.append(1)
            if not config_store.get(s2a.AUTO_SYNC_SINCE_KEY):
                config_store.set(s2a.AUTO_SYNC_SINCE_KEY, "2026-01-01T00:00:00+00:00")
            return None

    runner = s2a.Sub2ApiAutoSync(FakeService())
    runner.tick(1000.0)          # 刚开启：立刻记起点
    runner.tick(1060.0)          # 间隔没到
    runner.tick(1000.0 + 600)    # 到了
    assert len(calls) == 2


def test_client_lists_sub2api_groups_with_admin_auth():
    client = s2a.Sub2ApiClient("http://s2a.local/", "admin-key")
    groups = [{"id": 12, "name": "Default"}]
    response = _response(200, {"code": 0, "data": groups})

    with patch("application.sub2api_sync.requests.get", return_value=response) as get:
        assert client.list_groups() == groups

    get.assert_called_once_with(
        "http://s2a.local/api/v1/admin/groups/all",
        headers={"x-api-key": "admin-key"},
        timeout=s2a.REQUEST_TIMEOUT_SECONDS,
    )


def test_client_rejects_invalid_group_list_response():
    client = s2a.Sub2ApiClient("http://s2a.local", "admin-key")
    with patch("application.sub2api_sync.requests.get", return_value=_response(200, {"code": 1, "data": []})):
        with pytest.raises(s2a.Sub2ApiClientError, match="分组"):
            client.list_groups()


def test_client_filters_account_ids_by_original_identity_and_import_marker():
    account_id = _create("marked-search@test.com")
    item = AccountsRepository().get(account_id)
    body = {
        "code": 0,
        "data": {
            "items": [
                {"id": 11, "name": "marked-search@test.com", "extra": {s2a.IMPORT_MARKER_FIELD: "marker-123"}},
                {"id": 12, "name": "marked-search@test.com", "extra": {s2a.IMPORT_MARKER_FIELD: "other-marker"}},
                {"id": 13, "name": "marked-search@test.com", "extra": {}},
            ],
            "total": 3,
            "page": 1,
            "page_size": 100,
            "pages": 1,
        },
    }
    client = s2a.Sub2ApiClient("http://s2a.local", "admin-key")

    with patch("application.sub2api_sync.requests.get", return_value=_response(200, body)):
        assert client.list_matching_account_ids(item, identity="marked-search@test.com", import_marker="marker-123") == {11}


def test_client_finds_new_account_id_by_exact_identity():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    page_body = {
        "code": 0,
        "data": {
            "items": [{"id": 11, "name": "a@test.com"}],
            "total": 1,
            "page": 1,
            "page_size": 100,
            "pages": 1,
        },
    }
    client = s2a.Sub2ApiClient("http://s2a.local", "admin-key")

    with patch("application.sub2api_sync.requests.get", return_value=_response(200, page_body)) as get:
        assert client.find_new_account_id(item, set()) == 11

    get.assert_called_once_with(
        "http://s2a.local/api/v1/admin/accounts",
        params={"page": 1, "page_size": 100, "search": "a@test.com"},
        headers={"x-api-key": "admin-key"},
        timeout=s2a.REQUEST_TIMEOUT_SECONDS,
    )


def test_client_does_not_choose_ambiguous_new_account_id():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    page_body = {
        "code": 0,
        "data": {
            "items": [{"id": 11, "name": "a@test.com"}, {"id": 12, "name": "a@test.com"}],
            "total": 2,
            "page": 1,
            "page_size": 100,
            "pages": 1,
        },
    }
    client = s2a.Sub2ApiClient("http://s2a.local", "admin-key")

    with patch("application.sub2api_sync.requests.get", return_value=_response(200, page_body)):
        assert client.find_new_account_id(item, set()) is None


def test_client_refreshes_account_and_syncs_upstream_models():
    client = s2a.Sub2ApiClient("http://s2a.local", "admin-key")
    success = _response(200, {"code": 0, "data": {"models": ["model-a"]}})

    with patch("application.sub2api_sync.requests.post", return_value=success) as post:
        client.refresh_account(11)
        client.sync_upstream_models(11)

    assert [call.args[0] for call in post.call_args_list] == [
        "http://s2a.local/api/v1/admin/accounts/11/refresh",
        "http://s2a.local/api/v1/admin/accounts/11/models/sync-upstream",
    ]
    for call in post.call_args_list:
        assert call.kwargs["headers"] == {"x-api-key": "admin-key"}
        assert call.kwargs["timeout"] == s2a.REQUEST_TIMEOUT_SECONDS


def test_config_repository_persists_sub2api_default_group_id():
    repository = ConfigRepository()

    updated = repository.update_flat({"sub2api_default_group_id": "12"})

    assert "sub2api_default_group_id" in updated
    assert repository.get_flat()["sub2api_default_group_id"] == "12"


def test_config_api_clears_default_group_when_sub2api_url_changes(client):
    _configure()

    response = client.put(
        "/api/config",
        json={"data": {"sub2api_url": "http://new-s2a.local", "sub2api_default_group_id": "99"}},
    )

    assert response.status_code == 200
    assert client.get("/api/config").json()["sub2api_default_group_id"] == ""


def test_config_api_keeps_group_when_sub2api_url_is_unchanged(client):
    _configure()

    response = client.put(
        "/api/config",
        json={"data": {"sub2api_url": "http://s2a.local", "sub2api_default_group_id": "99"}},
    )

    assert response.status_code == 200
    assert client.get("/api/config").json()["sub2api_default_group_id"] == "99"


def test_sub2api_group_list_endpoint_returns_groups(client):
    groups = [{"id": 12, "name": "Default"}]
    with patch("api.accounts.sub2api_sync_service.list_groups", return_value=groups):
        response = client.get("/api/accounts/sub2api/groups")

    assert response.status_code == 200
    assert response.json() == groups


def test_sub2api_group_list_endpoint_reports_missing_config(client):
    with patch("api.accounts.sub2api_sync_service.list_groups", side_effect=ValueError("未配置 Sub2API")):
        response = client.get("/api/accounts/sub2api/groups")

    assert response.status_code == 400
    assert "Sub2API" in response.json()["detail"]


def test_sub2api_group_list_endpoint_reports_upstream_failure(client):
    error = s2a.Sub2ApiClientError("Sub2API unavailable")
    with patch("api.accounts.sub2api_sync_service.list_groups", side_effect=error):
        response = client.get("/api/accounts/sub2api/groups")

    assert response.status_code == 502
    assert response.json()["detail"] == "Sub2API unavailable"
