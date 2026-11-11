# Agent-Ready API Implementation Plan

Goal: API-key agents can perform the identified business workflows with the same user permissions, effective per-key throughput policy, and safe retries. Deliver a tested PR and review it before handoff.

Base: main at 739bbd818. Working branch: codex/agent-ready-api.

## Contract

- Keep /api/v1 as the supported machine API. Session UI behavior and CSRF protection remain intact.
- Cover workspaces, projects, work items, pages, saved views, notifications, webhooks, and the existing estimate API. Login, credential management, and instance administration remain owner/session-controlled.
- Prioritize an explicit current contract over backwards compatibility. Keys have explicit nonempty read/write scopes; empty or invalid scopes fail closed. Optional workspace/project restrictions narrow the owning user's permissions. Migrations must still protect existing data.
- Keep the existing normal/service rate defaults; support explicit administrator-controlled per-key rates. Do not claim a higher VPS capacity without measurements.
- Reject disabled users, hash stored credentials, and prevent credentials from entering logs or ordinary token update responses.
- Idempotent mutations must check current authorization before replay, serialize concurrent retries, detect mismatched payloads, and roll back failed claims.
- Bulk work-item changes preserve permissions, validation, activity, and webhook behavior.
- Publish accurate schema and deployment guidance. Repository changes do not imply production Cloudflare settings or capacity have been verified.

## Work

- [x] Credentials: storage/migration, inactive-user rejection, owner-controlled key metadata, scoped access, redacted auditing.
- [x] Throughput: one effective atomic per-key policy across APIView/ViewSet, with correct response/backoff information.
- [x] Coverage: explicit API-key adapters around existing business views, discovery filtering, estimate registration and reachable upsert methods.
- [x] Write safety: durable idempotency, work-item conflict protection, useful bulk action, focused concurrent-request tests.
- [x] Integration: model registration, ordered safe migrations/backfill, production environment/schema configuration.
- [x] Documentation: agent authentication, permissions/scopes, supported workflows, retries, bulk use, rate control and Cloudflare/API deployment checks.
- [x] Verification: 260 isolated PostgreSQL/Valkey regression tests passed; the final review fix passed all 87 business tests including its new regression. Migration upgrade and consistency checks passed, OpenAPI validated, affected frontend types and lint passed.
- [x] Delivery: committed and pushed; draft PR #2 opened and reviewed; blocking findings resolved and verified. No production deployment or merge performed.

## Ownership

Credential worker owns key/auth/throttle/logging files and related tests. Coverage worker owns business adapters, API URL registration and schema hooks. Write-safety worker owns the idempotency model/module, work-item handlers and bulk modules. Main owns migration generation/backfill, deployment/docs, shared integration, verification and final review. Workers do not commit independently.

## Verification Environment

Use disposable local PostgreSQL and Valkey containers bound to random loopback ports, explicit test-only connection settings, and mocked external email/storage/webhook delivery. Do not use deployment databases or worker queues. Preserve the user's untracked security report and existing unrelated containers/worktrees.

## Review Decisions

- Reset historical rate overrides and service flags: both were owner-writable and cannot establish trusted operator policy.
- Recheck current roles, ownership, visibility, and retained soft-delete metadata before replay. If required metadata is purged, deny replay rather than disclose cached results.
- Reject Idempotency-Key on workspace deletion before mutation because deletion rewrites the workspace slug; document this exception in the schema and agent guide.
- Keep the existing deployment rate defaults until workload measurements justify changing them; operators can grant explicit per-key rates.
- Published-PR review caught project-view deletion replay accepting a workspace-admin fallback after loss of project-admin status. The replay now enforces the handler's exact project-admin-or-owner rule; the regression downgrades only the project role.

Review outcome: no remaining blocking findings within the implemented scope. GitHub reported no hosted checks for this PR; verification evidence is local. Production Cloudflare behavior and VPS capacity remain deployment checks.
