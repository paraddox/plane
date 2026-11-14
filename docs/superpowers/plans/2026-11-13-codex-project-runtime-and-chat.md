# Codex Project Runtime and Work-Item Chat: V1 Plan

Status: implemented and validated locally and on SOSO.

## Outcome

Humans manually connect each Plane project to a configured Codex runtime. Each
project has a persistent overseer thread. Each claimed work item has two separate
persistent threads: implementation and chat. These are independent Codex threads,
not a required parent/subagent tree. Individual threads may use subagents if their
own instructions call for them.

**Project affinity:** one project belongs to one agent machine/runtime. Its
overseer and every task's implementation/chat threads run on that same machine,
with access to local project files and working state. Never split a task pair or
a project's roles across machines. Multiple machines host different projects.
Project relocation is explicit, not a per-role placement setting.

The chat thread answers users without routinely interrupting implementation. The
active conversation lives in the Codex session on its owning machine. Plane saves
the complete user-facing transcript for later review, not as conversation state
to replay into Codex. The normal work-item Activity timeline does not display chat
messages. Closing the work item archives both task threads and leaves a reviewable
conversation on the ticket. The project overseer remains available for new work.

This is a new v1 design. No compatibility APIs, legacy transcript reconstruction,
new per-thread permission framework, or automatic fleet scheduler are required.
Use the operator-approved Codex permissions and existing Plane authorization.
Role prompts distinguish responsibilities; shared Codex permissions are acceptable.

## Research and Existing Code

- Research started with `codex-cli 0.154.0`. Live validation now uses 0.169.0 on
  the Mac and 0.168.0 on SOSO, with Sol 6.1 / max and existing ChatGPT sign-in.
  Separate projects have exercised independent thread creation/resume, paginated
  history, tool calls and streamed chat on both machines.
- The [official app-server documentation](https://developers.openai.com/codex/app-server)
  documents WebSocket transport (experimental and unsupported), thread lifecycle and streamed output. Pin the
  selected CLI release and generate client bindings from that release rather than
  assuming the desktop app's private integration is a public API.
- Knowing two thread IDs does not itself give independent threads a communication
  channel. The runtime must provide thread coordination tools backed by the owning
  app-server. Do not confuse native subagent messaging with arbitrary thread RPCs.
- `plane/coordination/execution.py`, `notifications.py` and `access.py` already
  provide claims, handoff, task-scoped access, durable signed feedback events,
  replay/catch-up and attachment access. Reuse these mechanisms.
- `AgentRun` currently binds one agent/worker identifier and permits one live run
  per contract. Keep one logical work-item run and add session bindings for its two
  threads; do not create competing runs just to represent the two conversations.
- `plane.asgi` and the production Uvicorn worker already support the HTTP serving
  model needed for an asynchronous chat event stream. There is no existing
  work-item agent-chat service or persistent conversation model.

## Runtime and Host Placement

Run only Codex's own `codex app-server --listen ws://ADDRESS:PORT` on each agent
machine. Plane runs the persistent connector and orchestration command in its own
backend deployment and connects directly to those WebSockets over the private
network. No custom agent-host gateway, adapter application or daemon is installed
on agent machines. The desktop app need not be open. Use native app-server
authentication for non-loopback listeners; do not expose listeners publicly.

Thread references are location-aware:

```text
ThreadRef = { host_id, runtime_id, thread_id }
TaskSession = { project_id, issue_id, run_id, implementation: ThreadRef, chat: ThreadRef }
ProjectSession = { project_id, overseer: ThreadRef }
```

`runtime_id` selects the local Codex process/profile and session store when a host
runs several projects. A bare thread ID must never select a machine implicitly.
Persist project affinity and task bindings so later configuration edits do not
redirect existing threads or create a split-host project. All roles use the
project's owning runtime. Role-specific paths or prompts do not change the host.

An illustrative project manifest, with placeholder IDs:

```json
{
  "workspace": "mue",
  "project_id": "PROJECT_UUID",
  "runtime": "soso-mue",
  "repository": "example/product",
  "base_branch": "main",
  "instructions": ["AGENTS.md"]
}
```

Host manifests supply app-server WebSocket URLs, local checkout paths, runtime profiles
and credential references. Repository access, build/deployment knowledge and
target branches stay in the infrastructure configuration or source repository,
not new Plane project forms. Project onboarding and placement remain manual.

Do not silently fail over an active Codex thread to another machine. Restarting
its owning runtime resumes its stored thread. Moving an active thread requires
an explicit migration of session and working-tree state. Backups for continuity
must include Codex session storage and uncommitted workspace state, not just Git.

## Configuration Repository

Use an external repository for configuration, prompts and skills. The connector
implementation and its tests live in Plane, not in a separate host application:

```text
agent-infra/
  AGENTS.md
  roles/
    overseer.md
    implementation.md
    chat.md
  .agents/skills/
    plane-workflow/SKILL.md
    review-and-deliver/SKILL.md
  projects/
    mue/
      AGENTS.md
      .agents/skills/
  hosts/
    workstation.md
    build-host.md
  config.json
  .env.example
```

Generic rules, project-specific guidance and the selected role form each thread's
instruction set. The runtime loads these layers explicitly and saves the resolved
instructions, model settings and location on each binding. It preserves Codex's
default harness and the source repository's own applicable guidance.

A configuration repository's `AGENTS.md` is not automatically discovered when
Codex works in an unrelated source checkout. Install/resolve the common and
project guidance into the runtime's Codex profile and pass role instructions
explicitly. See the [AGENTS.md discovery rules](https://developers.openai.com/codex/guides/agents-md).
Install selected shared/project skills into Codex-recognized locations; do not
assume arbitrary `skills/` directories are automatically active. See
[Codex skills](https://developers.openai.com/codex/skills).

Actual project keys, local environment files, SSH private keys and GitHub CLI
authentication stay outside version control. The committed example contains names
and references only. Do not automatically inherit this desktop chat's personal
configuration or credentials into the service.

## Thread Responsibilities and Communication

- Overseer: responds to new-task events, chooses the configured execution target,
  claims/hands off the work, and creates the two task threads through the runtime.
  Normal task feedback does not require another overseer turn.
- Implementation: reads task intent and attachments, asks questions, changes code,
  runs tests, reports progress, handles review feedback and performs the authorized
  merge. It alone is prompted to make implementation and delivery changes.
- Chat: handles user dialogue, reads implementation status and reported progress,
  requests clarification from the implementation thread, and forwards material
  decisions or feedback. It must distinguish observed results from assumptions.

Expose the `plane` dynamic tool with `worker_status`, `send_to_worker`,
`send_to_chat`, `inbox` and `acknowledge` actions. These are Plane tools, not
claims that those exact names are public Codex RPCs.
Each request resolves a full `ThreadRef` to the correct machine and app-server.
Use explicit sender/message IDs, replies and acknowledgements to prevent duplicate
handling and automatic reply loops.

Ordinary chat starts or continues only the chat thread. Inspect implementation
status/history without starting an implementation turn. Queue implementation-bound
messages while it is busy; the worker consumes them at meaningful checkpoints.
An idle worker can receive a follow-up turn on its existing thread. Do not call
`turn/steer` or `turn/interrupt` on implementation merely because a user sent chat.
An explicit human stop/cancel request is different and should stop execution.

Serialize turns per thread. Display queued/delivered/acknowledged feedback honestly;
accepting a message does not mean the implementation has applied it. Preserve
existing specification-revision and merge-approval behavior when requirements or
definition of done change. Casual chat is not an implicit merge approval.

Keep one runtime-owned credential manager per Plane run. Both threads may use the
same task authority through it. Current execution-key refresh invalidates the
previous run key, so independent threads must not repeatedly refresh and revoke
each other's access. Serialize refresh/rotation and update the shared task client.

## Plane Chat and Transcript

Add a conversation record linked to the work item and session/run bindings. Store
user-facing messages separately from `IssueComment` and work-item Activity.
Messages carry stable IDs, author identity, ordering, timestamps, completion/error
state, originating Codex turn/item IDs where applicable, and attachment references.

The task shows an `Agent conversation` action. Opening it shows the web chat and
its complete saved history without truncating older messages. It is initially closed.
The same action works from full detail, previews, Inbox embeds and mobile views.
Do not create one Activity entry per chat message or duplicate its text there.

While open, the UI resumes the mapped chat session on its owning host. Plane's
transcript is a review copy, not a source for reconstructing the model's state.
If that session is unavailable, show offline/unavailable and retain the review
history; do not silently invent a replacement context from the transcript.

Mirror user messages, public assistant output and attachment cards during normal
operation, rather than waiting until ticket closure to export everything. Persist
all user-visible dialogue even if Codex later compacts its working context.
Exclude private model reasoning, credentials and raw tool output from public chat.
Internal worker/chat coordination is not automatically a public conversation.

Use stable message and stream-event IDs for retry/reconnect deduplication.
Assistant output streams into one message, with completion reconciled against its
final Codex item. Keep incomplete/interrupted responses visibly marked rather than
pretending an interrupted stream completed successfully.

Attachments use existing work-item assets. A chat upload first uploads/confirms a
task attachment and then references its asset ID in the message. Worker screenshots
and documents follow the same path. Store asset references, not expiring download
URLs, as permanent transcript data.

Formal blocking questions retain their existing question/answer API and UI.
Ordinary chat does not create those records or issue comments, and never silently
acts as a formal answer or merge authorization.

## Transport and Persistence

```text
Browser -- HTTPS message POST + event stream --> Plane
Plane connector -- direct WebSocket RPC over Tailscale --> owning Codex app-server
Codex app-server -- replies/tool calls/notifications --> Plane durable records
Plane connector -- routed mailbox tools --> other addressed Codex threads
```

Keep browser connections on Plane's existing public HTTPS origin. Do not expose
raw app-server access to browsers. Add asynchronous SSE endpoints to the existing
ASGI application for messages, response deltas, delivery state and availability.
Reconnect with a full persisted snapshot, including after an API worker restart.

Reuse Plane's coordination events and scoped catch-up. Add durable commands for
chat rather than waking the overseer or implementation for every message.
Persist delivery checkpoints, thread bindings and transcript records in Plane;
Codex persists its own session state. Reconcile history after disconnect before
resubmitting ambiguous requests. Do not rely only on live notifications.

This path does not require inbound callbacks on agent machines. Existing generic
webhooks remain available to other integrations. Plane needs outbound connectivity
to each configured private app-server endpoint.

On closure, reject new chat work, revoke normal task execution access immediately,
finalize the saved transcript, and archive the two task threads. Archive finalization
must be idempotent and able to finish after execution credentials are revoked,
using a narrowly defined runtime archival operation. A transcript delivery outage
must not keep a merged ticket open or keep its execution credentials usable.
Show archive-pending state until reconciliation succeeds; retain the durable spool.
The closed work item's conversation is review-only and needs no running Codex model.

## Delivery Plan

### Task 1: Prove Independent Multi-Host Thread Control

Ownership: Plane connector and a dedicated test project.

- [x] Build a minimal client for the tested app-server protocol and persistent
      location-aware thread registry. Do not attach it to unrelated desktop sessions.
- [x] Create an overseer and two ordinary task threads using `thread/start`, not
      a subagent tree or a history fork. Give each its distinct resolved prompt.
- [x] Prove two-way routed messaging, status observation without a new worker
      turn, busy-worker queuing and idle-worker continuation.
- [x] Exercise two actual hosts with different projects: one entirely local and
      one entirely on SOSO. Verify each project's overseer/chat/implementation
      share its host, and restart resumes original IDs without role splitting.

Evidence: a worker command continues while the user chats; feedback reaches the
correct thread without an interrupted turn; host identity is never inferred from
a raw thread ID. Document version-specific API behavior before expanding the UI.

### Task 2: Configuration and Durable Plane Connector

Ownership: external `agent-infra` examples and Plane backend runtime/tests.

- [x] Validate project-affine manifests and explicitly load instruction layers;
      document native skill installation rather than inventing a skill loader.
- [x] Add manual manifest registration, connection state, event reception, queued
      project-affine host routing and persisted project/task session bindings.
- [x] Connect existing claim/handoff and feedback APIs to real Codex turns; keep
      the overseer idle during normal task handling.
- [x] Centralize run credential refresh so implementation and chat remain usable
      together without invalidating each other's task authority.
- [x] Resume native session stores/workspaces and persist delivery state across
      connector restarts. Operators own backups and process supervision.

Evidence: duplicated callbacks do not create duplicate claims/thread pairs; an
offline machine leaves visible queued work rather than creating another agent.

### Task 3: Conversation API, Archive and Session Bindings

Ownership: `apps/api/plane/db/models`, migrations, `plane/coordination`, API tests,
and shared API types/services.

- [x] Add task conversation/message records and two thread bindings to one run.
- [x] Add human message/history APIs, runtime transcript ingestion, asynchronous
      SSE/replay, delivery state and archive finalization.
- [x] Keep existing task grants and operator-approved shared Codex permissions;
      do not add a per-thread permission product or compatibility layer.
- [x] Connect task assets without duplicating chat in Activity; preserve the
      separate formal question/answer flow.
- [x] Cover close/revoke/archive ordering and idempotent recovery.

Evidence: ordered complete transcript after reconnect, no duplicate messages,
normal Activity unchanged by chat, and no need to replay Plane text to resume Codex.

### Task 4: Work-Item Conversation UI

Ownership: shared web conversation components, full/preview/Inbox integrations,
frontend unit and browser tests.

- [x] Add the closed-by-default conversation entry and responsive chat UI.
- [x] Render streaming replies, full history, author identity, queued/offline/error
      states, confirmed attachment cards and closed-ticket review mode.
- [x] Support reconnect and multiple authorized people viewing the same
      work-item conversation without mixing author identities or concurrent turns.

Evidence: the conversation works from every work-item presentation and mobile;
ordinary work-item history contains no copied dialogue.

### Task 5: Real MUE End-to-End Trial

Ownership: local stack, external agent hosts and regression/operational tests.

- [x] Manually register isolated projects and hosts; create real tasks in Plane.
- [x] Observe real overseer handoff, independent implementation/chat turns,
      feedback, uploaded document and screenshot. Existing question/answer and
      specification-change protections remain covered by contract regressions.
- [x] Chat while a worker command runs; confirm no routine interruption or
      extra overseer turns. Verify each project's complete thread set stays on its
      assigned machine, with recorded thread addresses.
- [x] Restart browser and connector; reconnect to the same stored chat.
- [x] After local success, run an isolated app-server on SOSO over SSH and verify
      a separate project's complete thread set runs there, including its overseer
      and chat thread. Reject manifests attempting to split a project across hosts.
- [x] Cover duplicate messages, disconnects, interrupted replies, uncertain
      submissions, acknowledgements and recovery with deterministic regressions.
- [x] Cancel the dedicated trial runs and confirm immediate execution revocation,
      eventual archival of both task threads, and complete review history in Plane.

Use the existing backend contract-test framework and web `pnpm --filter web test`
plus browser regressions. The live trial must use real Codex threads: the existing
deterministic callback fixture alone is not proof of this integration.

Recorded result: 231 backend coordination/Codex tests and 29 web tests passed.
The remote trial retained 23 messages and all three assets after cancellation;
its existing key returned 403, both SOSO task threads archived, and its overseer
remained idle. Desktop/mobile screenshots verified the returned image rendered.
The local trial retained 13 messages with the same revocation/archive behavior.
No demo changes were merged or opened as a PR.

## Scope Boundaries and Open Deployment Inputs

Implementation needs the actual host names/addresses, per-host paths, operator
Codex login/configuration and the first project's repository/credentials. These
are deployment inputs, not reasons to add a global auto-onboarding agent.

Automatic active-thread migration, fleet autoscaling, a redesigned permission
system and using Plane transcript text to recreate lost model sessions are outside
this v1. Normal backups, existing authorization and merge safeguards remain intact.
