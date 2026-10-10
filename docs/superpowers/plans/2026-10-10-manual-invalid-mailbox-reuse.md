# 手动无效邮箱复用 Implementation Plan

> **Execution:** Use superpowers:executing-plans to implement this plan task-by-task in the current session. Do not delegate to agents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow an opt-in ChatGPT mailbox registration task to reuse manually added invalid account emails after the selected mailbox provider authoritatively verifies or creates the exact mailbox address.

**Architecture:** Store `account_origin: "manual"` in the existing account overview JSON and expose it through the existing account record API. Add a default-off registration option, query only explicitly manual + invalid + password-bearing candidates, and route candidates through an optional exact-address provider capability delegated only to the task's selected primary provider. Preserve the existing failed-account reuse path and fail closed whenever provider address existence or exact-address creation is uncertain.

**Tech Stack:** React/TypeScript, FastAPI/Pydantic, Python, SQLModel account overview graph, pytest, existing frontend build/test tooling.

**Design:** `docs/superpowers/specs/2026-10-10-manual-invalid-mailbox-reuse-design.md`

---

## File map

- Modify `frontend/src/pages/Accounts.tsx`: tag manual creates, show a source badge, and add/send the default-off reuse toggle.
- Modify `README.md` and `README_en.md`: document the opt-in, ChatGPT-only scope, and CF Worker address constraints.
- Modify `core/base_mailbox.py`: optional exact-address mailbox capability and selected-provider delegation through `FallbackMailbox`; implement CF Worker `/admin/address` exact-match lookup plus `/admin/new_address` specified-address creation as detailed in C1.
- Modify `core/db.py`: query eligible manual invalid records and account for their use without altering their invalid lifecycle/validity markers.
- Modify `application/tasks.py`: gate the extra candidate source and use the address capability in the existing ChatGPT cycle flow.
- Modify `tests/test_api_accounts.py`, `tests/test_failed_account_reuse.py`, `tests/test_failed_account_reuse_executor.py`, and `tests/test_pinned_mailbox.py`; create `tests/test_manual_invalid_account_reuse.py` for focused candidate/task coverage.
- The account schema path is `api/accounts.py` → `domain/accounts.py` → `infrastructure/accounts_repository.py` → `core/account_graph.py`; prefer the existing overview dict contract, avoid SQL schema migration, and test round-trip preservation.

## Task 1: Persist and display manual-account provenance

**Files:**
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `README.md` and `README_en.md`
- Test: `tests/test_api_accounts.py`; frontend validation: `npm run build` in `frontend/`

- [x] **Step 1: Add account-creation provenance regression coverage**

Extend the account API/repository test that POSTs a new account so an explicit overview value `account_origin: "manual"` is returned unchanged through subsequent list/get. Add a paired case without that value and assert legacy/ordinary records are not silently labeled manual. The API path already accepts an `overview` dict; no new public top-level field is required.

- [x] **Step 2: Run the focused test and confirm it describes current behavior**

Run: `pytest -q tests/test_api_accounts.py`
Expected: existing account API tests pass. The frontend has no React component test harness; validate the request type and rendered code with the TypeScript/Vite build in Step 4. No backend schema failure is expected because overview is JSON-backed.

- [x] **Step 3: Tag manual-add requests and render a source badge**

In `AddModal.save`, include `overview: { account_origin: 'manual' }` in the POST body. In the account table row, render a compact “手动添加” badge only when the returned overview has `account_origin === 'manual'`; do not infer provenance from email, creation date, status, or credentials. Preserve the overview key when account detail updates are saved.

- [x] **Step 4: Run focused API tests and frontend checks**

Run: `pytest -q tests/test_api_accounts.py` and `(cd frontend && npm run build)`. This repository's `frontend/package.json` defines `build` as `tsc -b && vite build`.
Expected: API round-trip tests pass and TypeScript/build reports no errors; ordinary and old accounts remain unlabeled.

## Task 2: Define an exact-address mailbox capability

**Files:**
- Modify: `core/base_mailbox.py`
- Test: `tests/test_pinned_mailbox.py` and `tests/test_failed_account_reuse.py`
- Provider API validation: mock all CF Worker requests; per user instruction, do not call the real service. Mock coverage does not establish the deployed Worker version's response shape.

- [x] **Step 1: Write provider capability contract tests**

Add tests for: existing exact result; substring-only result not treated as existing; invalid local-part makes no request; authoritative absence followed by exact-address creation; rewritten address rejected; non-2xx create followed by a fresh existence query that finds the exact address; HTTP 401/500, non-JSON, missing `results`, and ambiguous pagination all fail closed. Add `FallbackMailbox` coverage that only the explicitly selected provider is called.

- [x] **Step 2: Confirm tests fail before implementation**

Run: `pytest -q tests/test_pinned_mailbox.py tests/test_failed_account_reuse.py`
Expected: FAIL because the explicit capability/delegation does not exist.

- [x] **Step 3: Add the optional capability and delegation**

Add a non-abstract `BaseMailbox.ensure_address(email: str) -> MailboxAccount` that raises a specific unsupported-capability error by default. Implement `FallbackMailbox.ensure_address(email, provider_key)` by selecting exactly the requested key, invoking only its capability, then injecting the provider metadata used by existing mailbox routing. Do not fall through to another provider. For a direct provider instance, call `ensure_address(email)` without a provider key.

Implement `CFWorkerMailbox.ensure_address` with the reviewed `/admin/address` and `/admin/new_address` contract:

1. Normalize `email.strip().lower()`, split into local/domain, and issue no requests unless the normalized domain equals `self.domain.lower()` and local fully matches `^[a-z0-9]+$`.
2. Query `GET {api}/admin/address` with params `query=email`, `limit=100`, `offset=0` and `_headers()`. Require a 2xx JSON response with a `results` list. Match only when a result's `name` lowercases exactly to the normalized email. Exact match returns the existing address. No exact match with `count > len(results)` is ambiguous and fails closed; otherwise it is absent.
3. On confirmed absence, POST `/admin/new_address` with `{"name": local, "domain": domain, "enablePrefix": false}`. Accept only 2xx and a returned `address` or `email` exactly matching the normalized email (case-insensitive). If the returned address differs, fail without retrying or deleting anything. On non-2xx, rerun the exact existence query once; a precise match resolves a concurrent create, otherwise fail.
4. Return `MailboxAccount(email=normalized, account_id=returned_jwt_or_empty, extra={"provider_resource": ...})` with metadata matching `CFWorkerMailbox.get_email()`; existing-address token is empty. Never log response bodies or tokens.

Do not infer mailbox existence from `/admin/mails`, which can return an empty list for an existing mailbox. Unit tests must mock every API response; no real environment create request is allowed.

- [x] **Step 4: Run provider and fallback tests**

Run: `pytest -q tests/test_pinned_mailbox.py tests/test_failed_account_reuse.py`
Expected: all explicit outcomes pass; fallback never invokes a non-selected provider; existing mailbox providers remain instantiable without implementing the optional method. The service adapter integration test is added to `tests/test_failed_account_reuse.py`, alongside current CF Worker address-read coverage.

## Task 3: Query and account for eligible manual-invalid rows

**Files:**
- Modify: `core/db.py`
- Test: create `tests/test_manual_invalid_account_reuse.py` for candidate query, use count, and task integration

- [x] **Step 1: Add query and counter regression tests**

Create manual-origin rows in test DB with overview combinations and assert the candidate query returns only rows with `account_origin == "manual"`, `validity_status == "invalid"`, ChatGPT platform, and non-empty password. Assert missing origin, `unknown`/`valid` status, failed-but-not-manual records, and blank passwords are excluded. Verify account status remains invalid after reserving/marking a manual candidate and use count is bounded by the existing `FAILED_ACCOUNT_REUSE_MAX` policy.

- [x] **Step 2: Run the focused tests and confirm they fail**

Run: `/data/projects/maidol/any-auto-register/.venv311/bin/pytest -q tests/test_manual_invalid_account_reuse.py`
Expected before implementation: FAIL because the query/counter functions are missing; verified. After implementation: 12 passed.

- [x] **Step 3: Implement candidate lookup and safe reuse accounting**

Add `list_reusable_manual_invalid_accounts(platform)` alongside `list_reusable_failed_accounts`, using loaded graph overview fields and stable oldest-first order. Exclude `lifecycle_status == "failed"` because those rows already belong to the existing failed-account path. Reuse `FAILED_ACCOUNT_REUSE_MAX` but store the separate count under `manual_reuse_attempts`; increment it with `core.account_graph.patch_account_graph(session, model, summary_updates={"manual_reuse_attempts": next_count})` so lifecycle, validity, and source are untouched. Do not call `mark_failed_account_reused` (its failed-lifecycle guard is inapplicable) or `save_account(Account(...))` (it can rewrite lifecycle). Keep reservation separate from durable count: only increment after `ensure_address` succeeds.

- [x] **Step 4: Run account graph and candidate tests**

Run: `pytest -q tests/test_manual_invalid_account_reuse.py tests/test_failed_account_reuse.py`
Expected: all new candidate and cap cases pass and existing failed-account semantics are unchanged.

## Task 4: Add the gated registration workflow and UI toggle

**Files:**
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `application/tasks.py`
- Test: `tests/test_manual_invalid_account_reuse.py` and existing registration-task/reuse tests

- [x] **Step 1: Add task behavior tests for the opt-in boundary**

Cover: toggle absent/false preserves current failed-account behavior and never queries manual candidates; true selects eligible manual records only after the existing failed candidate path returns none; explicit task email, non-ChatGPT platform, non-mailbox identity, provider lacking capability, provider error, and unconfirmed address state do not register the manual row; ensure-address failures leave lifecycle/validity/origin unchanged and never add rows to `list_reusable_failed_accounts`; rows with failed lifecycle are excluded from the manual query; successful `ensure_address` pins the exact returned `MailboxAccount` and stored account password, successful account save preserves `overview.account_origin`, and failed candidates are skipped without consuming their use count.

- [x] **Step 2: Run the focused tests and confirm they fail**

Run: `/data/projects/maidol/any-auto-register/.venv311/bin/pytest -q tests/test_manual_invalid_account_reuse.py`
Expected before implementation: opt-in integration cases fail because manual candidate selection is missing; verified. After implementation: 12 passed.

- [x] **Step 3: Implement the toggle and registration gate**

Add a `reuseManualInvalidEmails` state defaulting to `false` in `Accounts.RegisterModal`. Render a switch labeled “复用手动添加的无效邮箱” for the ChatGPT mailbox flow and send `extra.reuse_manual_invalid_emails` as a boolean. In `_execute_register_task`'s current ChatGPT cycle opening path, keep `_claim_failed_account` first and unchanged. For a manual candidate, perform these steps in order: (1) reserve `(platform, email)` using `_reused_failed_accounts` and its guard; (2) call `ensure_address`; (3) on failure release via `_release_failed_account`, skip without incrementing, and try the next row; (4) on success increment `manual_reuse_attempts`; (5) set `cycle["reused"]` and pin the exact returned `MailboxAccount` plus the row password so normal cycle cleanup releases the reservation. When `shared_mailbox` is `FallbackMailbox`, call `ensure_address(email, provider_key=extra["mail_provider"])`; for a single provider, call `ensure_address(email)`. Do not construct a replacement `MailboxAccount(email=...)`. On unsupported/error outcomes, log a sanitized skip reason; never log password, provider secrets, tokens or raw sensitive response content.

- [x] **Step 4: Run focused workflow and UI checks**

Run: `pytest -q tests/test_manual_invalid_account_reuse.py tests/test_failed_account_reuse.py tests/test_failed_account_reuse_executor.py` and `(cd frontend && npm run build)`. 
Expected: new option is default-off, opted-in behavior passes, existing failure reuse tests pass, TypeScript/build passes.

## Task 5: Review, regression verification, and handoff

**Files:** all files above

- [x] **Step 1: Run broader targeted regression coverage**

Run: `pytest -q tests/test_api_accounts.py tests/test_failed_account_reuse.py tests/test_failed_account_reuse_executor.py tests/test_manual_invalid_account_reuse.py tests/test_pinned_mailbox.py`.
Expected: all listed relevant tests pass. Run frontend build/type check once after all UI edits.

- [x] **Step 2: Review diff and safety properties**

Run `git diff --check`; inspect the diff for: manual origin only on manual AddModal creation, legacy origins remaining unknown, selected-provider-only operations, existing failed-account behavior unchanged, strict no-mailbox-existence inference from empty message lists, skip-without-consuming on adapter failure, and no sensitive material in logs.

- [x] **Step 3: Verify CF Worker failure-closed invariants**

Confirm tests cover invalid domain/local-part with no request, exact-match filtering of substring results, pagination ambiguity, malformed/error responses, rewritten create addresses, and create-conflict re-query. Confirm no code path uses `/admin/mails` emptiness as address absence and no error path falls back to another provider.

- [x] **Step 4: Hand off verification evidence**

Do not call the real CF Worker service; the user requested mock-only validation. Report explicitly that mocked contract tests do not verify the deployed Worker version's response shape. README user entries: `README.md#账号池--多平台账号统一管理` and `README_en.md#account-pool--unified-multi-platform-management`. Verification: `/data/projects/maidol/any-auto-register/.venv311/bin/pytest -q tests/test_manual_invalid_account_reuse.py tests/test_failed_account_reuse.py tests/test_failed_account_reuse_executor.py tests/test_pinned_mailbox.py tests/test_api_accounts.py` → 77 passed; `npm --prefix frontend run build` → passed. Completeness guard output:

```json
{
  "exit_code": 0,
  "blockers": [],
  "advisories": [],
  "new_names_count": 0,
  "declared": false
}
```

Report the live-probe deviation and test evidence to the reviewer. Do not commit, push, open a PR, or publish without the user's separate authorization.
