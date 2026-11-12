# Agent Handoff and Direct Notifications

**Goal:** Plane lets an external main agent hand a work item to an external
worker once. The worker receives subsequent task events and refreshes its own
task-scoped execution access without further main-agent calls. Plane does not
host agent prompts, choose models, or manage agent sessions.

**Completion evidence:** API regression tests and a local HTTP integration test
prove project notification, atomic handoff, worker-only delivery of answers,
comments, completed uploads and specification changes, scoped catch-up,
autonomous refresh, stale-revision fencing, delivery recovery and revocation.
The updated `plane-local` stack remains available for manual testing.

## Design

- Keep the existing project event log as the durable source. Add task-scoped
  consumption and signed HTTPS callback subscriptions, not a second source of
  truth. Event bodies contain IDs and revisions, never credentials or file data.
- A handed-off run is a persistent assignment. Its assignment key can only read
  its task context/events, manage its own notification subscriptions and obtain
  short-lived execution credentials. It cannot delegate, approve, merge or act
  on another task. Existing parent revocation, membership and project-policy
  checks still apply. Assignment authority ends on cancellation/reassignment or
  confirmed completion, not while waiting for a human answer.
- Short-lived execution access remains revision-bound. Material task edits
  preserve assignment and notifications, invalidate execution keys/approvals,
  and require the worker to explicitly acknowledge the current specification
  when refreshing access. Old evidence cannot authorize a new-revision merge.
- Callback delivery is persisted with the event transaction and dispatched by
  the existing Celery infrastructure after commit. Failed deliveries remain
  inspectable/replayable; a scheduled sweep recovers interrupted dispatch.
  Repeated delivery is expected and receivers deduplicate stable delivery IDs.
- Use CloudEvents JSON and Standard Webhooks HMAC signatures. Reuse the existing
  webhook transport policy (30-second request timeout, 600-second retry base,
  five retries) rather than introducing new operational limits. Protect callback
  egress against private addresses, redirects and DNS rebinding; private local
  test receivers require an explicit operator allowlist.
- A runtime needs only an HTTPS callback receiver or a scoped event-feed
  consumer. Prompts and session-resume behavior remain outside Plane.

## Work

### Task 1: Persistent Handoff and Worker Refresh

**Files:** coordination models/migration, API token purpose, execution/access,
coordination URLs/views, contract mutation, API rate accounting and tests.

- [x] Add handoff and narrowly scoped assignment authentication.
- [x] Add revision-checked worker credential refresh, including after idle expiry.
- [x] Preserve handed-off ownership on spec edits; fence stale execution writes.
- [x] Test isolation, concurrent/duplicate handoff, renewal, revision edits and
      cancellation/completion/parent revocation.

### Task 2: Reliable Direct Delivery

**Files:** notification models/migration, coordination notification APIs/service,
Celery tasks/sweep, event emission and tests.

- [x] Add project/main-agent and assignment/worker subscriptions and scoped replay.
- [x] Persist deliveries transactionally, sign callbacks and recover failures.
- [x] Validate callback destinations and pin the validated destination at send time.
- [x] Test signatures, duplicate delivery, rollback, recovery and cross-task denial.

### Task 3: Complete Task Event Coverage

**Files:** task/comment/attachment lifecycle hooks, coordination event helpers and
contract/API tests covering both UI and v1 writes.

- [x] Emit feedback events for comments and confirmed uploads, with actor IDs.
- [x] Emit field/revision changes consistently across native task write paths.
- [x] Include granted child-task changes without exposing unrelated project work.

### Task 4: Integration and Local Deployment

**Files:** API documentation, local integration fixture/runner, private local-stack
deployment configuration and validation artifacts.

- [x] Run focused regressions, then the affected coordination/security suites.
- [x] Build/migrate/restart only the named local stack, retaining its data.
- [x] Demonstrate main-agent delivery and handoff, then direct worker callbacks,
      access refresh and spec-update recovery with no further main-agent calls.
- [x] Leave documented URLs, delivery evidence and test limitations for the user.

## Boundaries

No backwards-compatibility layer, external AI-provider dependency, production
deployment, PR, push or merge is part of this request. Preserve current uncommitted
answer-editor changes, the standalone welcome page and unrelated security report.

## Verification

- Initial handoff tests failed on missing routes, then all six passed.
- Feedback regressions failed on missing comment/property events, then passed.
- Final API/model/transport regression run: 401 passed, the schema test skipped
  because schema generation is disabled in the test settings. Explicitly reran
  schema validation with `ENABLE_DRF_SPECTACULAR=1`: one passed (nonblocking schema
  metadata warnings remain). Includes concurrent refresh, all three material edit
  types, native state cancellation, parent revocation and completion fencing.
- Python lint, changed-browser-test lint and `git diff --check` pass.
- Applied migrations 0133 and 0134 and updated API/Celery services on `plane-local`.
- Real browser/HTTP integration on AHT-2 passed: UI answer acknowledgement,
  ordinary comment delivery, PNG/text uploads with byte-hash verification,
  explicit revision refresh and assignment continuity. Main-agent API calls
  remained 15 before/after all feedback. No page errors.
- Receiver outage retained a pending event after its first failed attempt.
  Worker-key replay delivered it on attempt two with the same delivery UUID,
  with no main-agent calls. Restored the receiver afterward.
- Live Celery inspection confirms delivery/recovery and audit/activity tasks
  are registered. The download integration found and fixed a raw-object-key
  response; a regression now requires the existing storage signing service.

### Review Follow-up

- Fixed cancellation overwriting specification edits made in the same request.
- Fixed nullable state updates returning 500; cancellation/reopening now uses
  the state actually saved by the task model.
- Routed browser module assignment/removal through the shared transactional
  service so the assigned worker receives the same events as API clients.
- Callback transport now tries other validated DNS addresses on transport
  failure, retaining address pinning, TLS hostname verification and no redirects.
- Added ten regression cases. Final API/model/transport suite: 411 passed,
  one schema test skipped by test settings. Python/browser lint and diff checks pass.
- Rebuilt the local backend and verified deployed source hashes. Browser handoff,
  answers, comments, module changes, uploads and spec refresh passed with main-agent
  calls unchanged at 17 after handoff. No browser or receiver errors.

### Additional Review Cycles

- Fixed missing worker notifications for advance merge authorization, module
  deletion, late editor-upload binding and attachment restoration.
- Fixed the comment composer binding images to the task ID instead of the new
  comment ID, and unbound task-description uploads rejected by the event hook.
- Preserved project-cover uploads and scoped task-asset binding to the target
  project or an unbound workspace asset. Added regression coverage for both.
- Final review found no further actionable issues in the handoff/notification
  scope. API/model/transport suite: 425 passed, one schema test skipped by settings.
  Six frontend unit tests, changed-file lint/format checks and diff checks pass.
- Updated API/web images and verified deployed backend hashes. The browser test
  posted a real inline screenshot, verified worker-downloaded bytes, and delivered
  merge authorization changes. Main-agent calls stayed at 21 after handoff, with
  no page or receiver errors. Earlier UI/demo/report work remains untouched.

## Local Test Setup

- App: `http://127.0.0.1:18130`
- Project: `http://127.0.0.1:18130/mue/projects/2ad884e1-6488-48d9-8b12-280ba5f2c5ee/issues/`
- Verified task: `http://127.0.0.1:18130/mue/browse/AHT-2/`
- Receiver status: `http://127.0.0.1:18131/status`
- Evidence: `/private/tmp/plane-local-stack/verification/agent-handoff/`

Create work in **Agent Handoff Test** to exercise automatic handoff and direct
feedback receipts. The loopback-only receiver is a deterministic external
runtime fixture, not an LLM provider. Real prompts/session execution remain the
responsibility of the chosen external runtime. No production service was changed,
no PR/merge was performed, and existing MUE work was retained.
