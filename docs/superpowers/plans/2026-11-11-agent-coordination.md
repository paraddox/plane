# Product and Agent Coordination Implementation Plan

Base: main at 22af73e7450c752cd153ccb455d8cba889af1c55 (PR #2 merged).
Working branch: codex/agent-coordination.

## Goal and Completion Evidence

Plane coordinates a product-owned work item through agent execution, questions,
decomposition, evidence, human authorization, and a confirmed repository merge.
The same task agent initiates the merge and loses task-run access only after
confirmed completion. Prove both advance-authorized and human-reviewed paths,
including failure recovery and authorization denials, with isolated PostgreSQL,
Valkey, provider-contract tests, migrations, schema checks, and browser checks.
No production deployment or merge of this branch is authorized.

## Agreed Product Contract

- Releases belong to individual Plane projects; work items cannot reference a
  different project's release. Priority and many-to-many modules are retained.
- The accountable human owner defaults to the creator and is separate from agent
  assignment. Requirements and definition-of-done criteria form a versioned spec.
- A complete task can be submitted to agents; incomplete drafts do not dispatch.
- An owner may authorize auto-merge in advance. Otherwise an owner or designated
  human reviewer authorizes merging after implementation. Every requested review
  is a required gate; owner approval does not silently waive outstanding reviews.
- An owner can approve before reviewers. Once remaining reviews and checks pass,
  the assigned agent may merge without another owner click. Code-review decisions
  bind to the reviewed head SHA; material spec changes invalidate authorization.
  Advance authorization applies only to its approved spec, not arbitrary changes.
- The same agent handles implementation, CI waits, fixes, renewed review, and
  merge initiation. A guarded API checks permission at merge time; there is no
  independently deciding merge agent or background auto-merge workflow.
- Done requires confirmed merge, not an agent's claim that testing is complete.
  Failed checks/conflicts leave the task open. Finalization records the merge,
  closes the run, and revokes all execution credentials for that run.
- Stable non-human service identities are separate from temporary workers/runs.
  Trusted server policy issues narrow run credentials; an LLM cannot manufacture
  credentials or grant itself privileges. Human approval is never delegated.
- Each worker has its own credential and authenticated provenance. Credential
  renewal requires a live authorized run; reassigned/stale workers are fenced.
  Named keys alone do not establish identity or task isolation.
- Existing session CSRF protection and unrelated user changes remain intact.

## Architecture and Interfaces

Reuse Issue, IssueComment, FileAsset, ModuleIssue, State, User.is_bot, APIToken,
and existing activity/notification UI. Add coordination state in a bounded
`plane.coordination` service package, with project-owned persisted records under
`plane.db.models`. Session endpoints live under `/api/.../coordination/`; machine
endpoints live under `/api/v1/.../coordination/` and retain API-key policy.

Implemented records: ProjectRelease, ProjectExecutionPolicy, WorkItemContract,
AgentProjectAccess, AgentRun, RunTarget, WorkItemQuestion, RunChild, RunEvidence,
ReviewRequirement/ReviewDecision, MergeAttempt, and durable CoordinationEvent
sequences. Execution credentials extend APIToken. Reuse
existing artifacts and comments rather than duplicating their storage engines.

The project execution policy names allowed repositories/base branches, context
references, trusted dispatcher identities, and operator-selected execution lease
and credential policies. It contains no public credentials. GitHub is the first
provider because this repository already uses it; provider secrets remain
server-side. Live installation credentials are an operator prerequisite, not
values to invent or commit. Provider contract tests use an isolated HTTP fixture.

Candidate mutations are explicit actions: submit, claim, renew, cancel,
ask/answer/resolve, decompose, report-evidence/head, request-review, decide-review,
authorize/revoke-auto-merge, merge, and reconcile-merge. Ordinary Issue PATCH,
bulk writes, or state changes must not bypass these guards. Task IDs, worker IDs,
grant ancestry, and allowed child targets are bound server-side, not trusted from
arbitrary headers. A read-only context operation supplies only permitted context.

## Work

### Task 1: Product Contract and Project Releases

Owner: main. Files: db models/migration, coordination contract services and
serializers, session/v1 routes, tests, shared web types.

- [x] Add project-owned releases and versioned work-item contracts: owner,
      requirements, stable definition-of-done criteria, target release, readiness,
      and advance-authorization metadata.
- [x] Add explicit validated submit/edit actions; reject cross-project releases,
      invalid human owners, incomplete submission, and machine-written approvals.
- [x] Preserve and validate multi-module membership; expose consistent machine
      add/remove semantics rather than the old move-oriented endpoint.
- [x] Verify migration consistency and real-key/session permission contracts.
      Evidence: 11 focused contract cases and the 227-test API/token regression run
      passed on disposable PostgreSQL/Valkey; schema validated in the regression.

### Task 2: Agent Identity, Exclusive Runs, and Scoped Credentials

Depends on Task 1. Files: APIToken/grant model, canonical authentication/rate
policy, coordination execution services/endpoints, operator management command,
contract and concurrent PostgreSQL tests.

- [x] Provision stable bot identities and dispatcher authority through trusted
      operator controls; do not open general credential management to API keys.
- [x] Atomically claim submitted work, issue distinct run/worker grants, renew
      while authorized, and fence cancellation, expiry, reassignment, and spec drift.
- [x] Enforce action and target restrictions across every reachable API route,
      including bulk, relations, attachments and created child tasks. Shared agent
      rate policy must not reset merely because another credential is issued.
- [x] Preserve actor, delegation, run/attempt, request, and revision metadata in
      audit records without recording secrets. Revocation preserves the audit trail.

### Task 3: Reliable Dispatch, Clarification, Decomposition, and Evidence

Depends on Tasks 1-2. Files: coordination events/questions/evidence services,
existing comment/module/attachment adapters, worker publication and tests.

- [x] Persist events with the business mutation and stable event identity. Use
      cursor-based pull delivery and catch-up rather than adding a webhook sender:
      consumers persist a cursor only after processing, retry failed HTTP reads, and
      atomic claims fence duplicate execution. This avoids a second delivery engine,
      outbound URL trust, and webhook secrets while meeting reliable dispatch.
- [x] Bind explicit blocking questions and answers to comments and intended human
      respondents; snapshot authoritative answers independently of editable comment
      echoes. Resume only after the agent acknowledges the current answer ID.
- [x] Allow scoped children/dependencies with owner, release, modules, acceptance
      criteria and run provenance. Required unfinished children block completion.
- [x] Bind uploaded artifacts, test/check results, screenshots, documents, and PR
      links to the run, spec revision, code revision and acceptance criteria.

### Task 4: Human Reviews and Same-Agent Merge

Depends on Tasks 1-3. Files: coordination review/merge services, GitHub adapter,
session decision endpoints, machine merge endpoints, all affected write guards.

- [x] Record human-only review requests/decisions and owner advance permission;
      reject bot/key attempts to impersonate human authorization.
- [x] Invalidate stale decisions after changed code/spec and block merge for
      unresolved questions, required children, required reviews or failed CI.
- [x] Let only the authorized assigned execution request merge for its configured
      repository/PR/head/base. Obtain current provider checks and enforce the expected
      head SHA and repository protections. Use the branch merge API with an explicit
      target and immutable approved head, not a mutable PR base. Workers never
      receive broad merge secrets.
- [x] Reconcile uncertain provider outcomes idempotently; confirm approved-head
      ancestry on the configured target before Done and credential revocation.
      The same agent can retry the fenced write without a second successful merge
      or duplicate completion event. Preserve failure details and allow fixes/review
      after definite rejection. Prevent deletion of an unresolved merge fence.

### Task 5: Human UI and Agent Contract

Depends on the relevant backend actions above. Files: existing issue detail and
creation surfaces, project settings/releases, shared services/types, docs.

- [x] Add an integrated task coordination panel for requirements/criteria, owner,
      release, auto-merge, required reviewers, questions, run progress and evidence.
      Reuse existing comments, attachments, modules and subtasks where applicable.
- [x] Add project release/execution-policy administration and agent/run visibility.
      Make permission, saving, empty, stale-data and failure states explicit.
- [x] Publish accurate OpenAPI and agent examples for the complete lifecycle,
      scoped delegation, recovery, human decisions, merge and revoked credentials.
- [x] Verify affected types/lint and functional desktop/mobile browser workflows;
      run the needed local servers and leave usable URLs for the user.

### Task 6: End-to-End Verification and Handoff

- [x] Exercise real authentication in isolated services: simple advance-approved
      task, reviewed task, owner-first/reviewer-later, reviewer-authorized task,
      clarification/resume, decomposition, artifacts, CI failure/fix/reapproval,
      stale SHA/spec, claim races, cancellation, missed events, uncertain merge,
      confirmed completion and post-completion denial.
- [x] Run applicable API/auth regressions, migration upgrade checks, schema
      validation, affected frontend types/lint, and browser layout/interaction checks.
- [x] Review final changes against this contract, resolve concrete findings, and
      report local evidence separately from unverified production/provider deployment.

## Final Evidence

- 431 backend API/authentication/security/coordination tests passed with actual
  PostgreSQL migrations and Valkey, including the real-HTTP GitHub fixture and
  concurrent claim/credential/revocation checks. Schema validation is included.
- Main migration 0122 upgraded through 0130 with existing tasks and hashed keys
  preserved and actual authenticated API access retained. Existing explicit
  answers backfilled correctly; reversing to 0127 and reapplying retained data.
  `makemigrations --check --dry-run` reports no changes.
- Web production build, web type checking, affected shared-package builds,
  scoped lint/format checks, 2 frontend helper tests, and 12 CSRF service tests
  passed. Production Compose configuration validates without deploying.
- The repeatable browser fixture and Playwright flow passed project policy and
  release setup, spec/advance authorization, required reviewer selection, claim,
  human answer acknowledgement, evidence, owner/reviewer decisions, and settled
  desktop/mobile layouts. Actual presigned screenshot/document uploads and
  private downloads used a local S3 emulator; rendered screenshots were inspected.
- Final review regressions prove canonical answers survive ordinary comment edits,
  stale answer IDs cannot resolve questions, completed task aggregates can be
  deleted, and unresolved merge history cannot be erased by deletion.
- Local UI: http://127.0.0.1:3000. API: http://127.0.0.1:8000. The disposable
  fixture's synthetic login is in `/private/tmp/plane-coordination-login.txt`.
  The disposable database was migrated and reseeded after Docker recovery during
  the settings-navigation regression verification. The private fixture/login
  files now describe that restored demo.

Limits: GitHub write behavior is verified with HTTP fixtures, not a live App
installation; operator-specific repository protections require staging validation
before production enablement. Existing app-shell hydration warnings appeared in
browser runs, while coordination interactions passed without other page errors.
At the initial implementation handoff, no production deployment, commit, push,
or new-branch PR/merge had been performed.

## PR Review

Published as draft PR #3: https://github.com/paraddox/plane/pull/3.
Review compares the full branch against main at 22af73e7450c752cd153ccb455d8cba889af1c55.

Two recovery findings were reproduced with failing regressions and fixed:

- An earlier definite rejection could erase a concurrent retry's uncertainty
  fence. Retry admission now persists uncertainty before the remote write, and
  rejection cannot downgrade it. Tests cover rejection before and after the
  overlapping retry's result; cancellation remains blocked.
- An expired lease left a pending merge impossible for its assigned agent to
  recover. Only the owning, still-authorized dispatcher can restore the same
  fenced run's lease and rotate its credential. Expired active runs remain
  non-renewable and must follow the ordinary reclaim path.

The focused execution/merge/provider/end-to-end suite passed 61 tests after these
fixes; the complete API/auth/security regression suite then passed all 359 tests,
and web type checking passed. No verified review findings remain unresolved.
Initial commit hooks also passed after two
existing shadowed callback parameters in the affected issue detail file were
renamed. The repository's hosted PR build/lint workflows target `preview`, not
`main`; PR #3 currently has no hosted checks, so local results are not presented
as hosted CI success. Live provider staging and the existing app-shell warning
limitations above remain unchanged. This PR is not merged or deployed.

### Independent Review Follow-Ups

Four further findings were implemented and covered by API/PostgreSQL regressions:

- Attachment downloads bind the asset to the granted issue and project before
  storage signing. Root/child reads remain available for valid attachments.
- Stale reconciliation preserves failed attempts and cannot overwrite a newer
  pending attempt; subsequent authorized retries still complete.
- Personal-key external-ID upserts resolve coordinated targets before mutation
  or replay, and recheck under the issue lock after authentication.
- Required-child states are locked during admission and protected by migration
  0130 during pending merges, including group changes and soft deletion.

The last seven regression cases were initially blocked by an unavailable local
Docker engine. After the user-approved Docker Desktop restart and restoration
of the isolated PostgreSQL/Valkey stack, all seven passed. The complete relevant
suite then passed 372 tests with actual migrations. A fresh upgrade from main
through 0130 preserved tasks, keys, answers and authenticated API access; reversing
to 0127 and reapplying succeeded. Migration drift checking also passed. No code
changes were needed following restored-runtime verification. The earlier runtime
validation limitation on commit 2b0f1a6ea is therefore cleared.

### Additional Authorization Guards

The next three review findings were reproduced in 13 failing API/PostgreSQL
regression cases, fixed, and verified:

- Execution PATCH rejects `deleted_at` for both roots and children. Valid
  updates still work and rejected deletion does not invalidate the live run.
- Personal-key write restrictions include RunChild records, covering PATCH,
  DELETE, bulk updates, external-ID upserts, and module assignment. Reads and
  valid execution completion remain available.
- The shared mutation guard now locks personal-key task targets in stable UUID
  order and rechecks coordination access before invoking the handler. Contract
  creation takes the same issue lock. PATCH, DELETE, and bulk races are tested
  both with and without idempotency, including unchanged bulk rows on denial.

All 13 regressions passed after the fixes; the full relevant API/auth/security
suite passed 385 tests with actual migrations. Scoped Ruff lint/format and diff
checks passed. No model or migration changes were needed for these findings.

### Attachment, Body Target, and Settings Isolation

The latest three findings were reproduced before their fixes:

- Attachment detail lookups now bind the asset to the URL's issue as well as its
  workspace and project. An ordinary issue URL cannot confirm or delete a
  coordinated root/child attachment by substituting its asset ID.
- Personal-key cycle and relation writes include every body-selected task in
  the shared authorization and locked recheck. Mixed ordinary/protected targets
  are denied without partial writes; ordinary-only operations still succeed.
- Project settings remount by workspace/project identity. Unsaved policy values
  and release drafts cannot carry across cached SPA navigation into another
  project's save request.

All 12 new backend regression cases failed before the fixes and passed after
them, including post-authentication contract creation. The full relevant suite
passed 397 tests with actual PostgreSQL migrations and Valkey. The focused
two-project browser regression reproduced the original state leak, then passed
with desktop/mobile screenshots inspected and no unexpected page errors beyond
10 known app-shell hydration warnings. Web type checking, 2 frontend helper
tests, scoped Ruff/OxLint/format checks, and diff checks passed. No model or
migration changes were needed.

### Canonical Gates and Cycle Transfers

A fresh independent read-only review found two further issues. Both were
reproduced through actual API requests before their fixes:

- The soft-deletion worker no longer treats `RESTRICT` relationships as
  cascades. Deleting a required child or a question/answer comment retains the
  canonical coordination record, so unfinished children and unacknowledged
  blocking questions cannot disappear from merge gates. Human answers and
  exact-answer acknowledgement still work after ordinary comment deletion.
- Personal-key cycle transfers resolve the same incomplete task selection used
  by the transfer utility, lock and recheck it, and pass that frozen selection
  into the mutation. Later arrivals cannot expand the authorized set, including
  when the source was initially empty. Denials leave memberships and progress
  snapshots unchanged. Normal session transfers and ordinary personal-key
  transfers remain available; completed/cancelled members stay in the source.

Nine regression cases failed against the prior code and passed after the fixes.
An additional session-transfer compatibility case passed. The full relevant
API/auth/security suite passed 407 tests with actual PostgreSQL migrations and
Valkey; scoped Ruff lint/format and diff checks passed. No frontend, model, or
migration changes were needed. Existing browser/build evidence and live-provider
validation limits are unchanged.

### Guest Visibility, Generic Assets, and Retention

The next independent read-only review found three issues, all reproduced with
18 failing database-backed regressions before the fixes:

- Restricted project guests can read only coordination tasks they created and
  those tasks' runs. Event filtering precedes pagination and omits hidden-task and
  project-wide events. Session and personal-key reads obey the same restriction;
  guest-created tasks, full-visibility guests, and regular members remain supported.
- Generic asset updates resolve the attached task before authorization and
  recheck after locking the task and asset binding. Workspace-scoped and
  unrestricted personal keys cannot modify coordinated root/child assets.
  Ordinary/unbound uploads remain writable. Concurrent rebinding is rejected
  without changing the upload status or undoing the other transaction's binding.
- Retention purging retains the normal bulk path, then isolates restricted rows
  when needed. Required children and canonical question/answer comments remain
  retained, while unrelated eligible rows in the same model and later cleanup
  phases are still purged. The configured retention duration is unchanged.

All 18 original regressions and two additional independently committed asset-
rebinding race cases passed. The complete relevant API/auth/security suite passed
427 tests with actual PostgreSQL migrations and Valkey. Scoped Ruff lint/format
and diff checks passed. No frontend, model, or migration changes were needed;
previous frontend evidence and live-provider validation limits are unchanged.

### Continued Review: Relation Targets

The next review pass identified a cross-project relation locking gap. Personal
relation writes now lock every affected work item in the request workspace,
regardless of project, before rechecking coordination restrictions. Incomplete
or foreign-workspace target sets are rejected before any relation is inserted.
Valid same-workspace cross-project relations remain supported.

Three new database-backed cases failed before the fix and passed afterward:
forward/reverse cross-project locks and mixed-workspace rejection without partial
writes. A separate archived-child locking claim was disproved: `Issue.objects`
uses `SoftDeletionManager`, not the archive-filtering `issue_objects` manager.
The existing state-lock probe now covers archived and unarchived required
children; both pass without changing merge production code.

The full relevant API/auth/security suite passed 431 tests with real migrations
and Valkey. Scoped Ruff lint/format and diff checks passed. No frontend, model,
or migration changes were needed. Further review continues against this updated
branch until a completed pass finds no actionable issues.

## Execution Notes

PR #2 was merged before this work started. Preserve the user's untracked
security_best_practices_report.md. Use only task-owned disposable test services;
do not touch production data, existing worktrees, or unrelated containers.
No arbitrary lease duration, token lifetime, retry budget, upload limit, or
throughput target is introduced: use applicable existing policy or explicit
operator configuration, and fail closed when a required policy is missing.
