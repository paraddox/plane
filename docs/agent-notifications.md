# Main-Agent Handoff and Direct Worker Notifications

Plane is the task, authorization and event service, not an agent runtime. A main
agent and its workers can run on different hosts or use different providers.
Their prompts, session persistence and execution tools remain outside Plane.

All paths below follow
`/api/v1/workspaces/{slug}/projects/{project_id}/coordination/` and authenticate
with `X-Api-Key`. Register a stable dispatcher and worker bot identity as described
in [Agent Coordination](agent-coordination.md). A runtime can reuse its registered
worker identity while supplying a distinct `worker_id` for every execution.

## Handoff

1. The main agent creates a project callback subscription. The default filter is
   `task.ready`, so ordinary feedback does not wake the main agent.
2. It claims an available task with the registered worker identity, external
   worker/session identifier and configured repository.
3. It calls `POST runs/{run_id}/handoff/` with `{}`. The response contains `id`,
   a plaintext assignment `token` returned only here, the `run`, and `event_cursor`.
4. The main agent starts its chosen runtime, securely supplies that assignment
   key and the task ID, then has no further normal task-management role.
5. The worker creates its own subscription with `after: event_cursor`, or consumes
   `GET runs/{run_id}/events/?after={cursor}`. Catch-up includes feedback posted
   between handoff and subscription registration, avoiding a startup gap.
6. The worker reads its task overview and calls `POST runs/{run_id}/refresh/`:

   ```json
   { "expected_revision": 1 }
   ```

   This returns `id`, a new execution `token`, and `expired_at`. The worker uses
   that short-lived token for task writes, questions, uploads, evidence and merge
   operations. Refresh rotates execution tokens without revoking the assignment
   key. It can restore an expired execution lease after a human wait; no main
   agent is needed. Heartbeats alone still do not renew authority.

Assignment keys remain revocable and task/run scoped. Cancellation, confirmed
merge, revoked parent access, disabled project execution or lost membership close
the worker's authority. Explicit cancellation/reassignment is required to replace
a handed-off worker; lease expiry alone cannot silently transfer its task.
Repeating handoff with the owning dispatcher rotates both assignment/execution
keys and disables old callbacks. Call it again only for deliberate key recovery.

Handoff, refresh and subscription creation reject `Idempotency-Key`; raw tokens
and callback secrets must never enter the idempotency replay store. Responses
carrying secrets use `Cache-Control: no-store`. Store them outside prompts,
comments and source control. A lost refresh response can be replaced by another
refresh. A lost callback secret is recovered by deleting/recreating that
subscription with the last saved event cursor.

## Direct Callbacks

Both main and worker create subscriptions through `POST subscriptions/`:

```json
{
  "url": "https://agents.example.com/plane/worker-session",
  "after": 42,
  "event_types": ["question.answered", "comment.created", "attachment.uploaded", "task.specification_changed"]
}
```

`after` and `event_types` are optional. A worker's subscription is bound by the
server to its own run and granted child tasks, not a caller-supplied task filter.
Omitting `after` starts at registration. Omitting the worker filter selects human
feedback, task changes, reviews and lifecycle events. An explicit empty filter
selects every event in the caller's authorized scope.

The creation response adds `signing_secret` once. `GET subscriptions/` and
`GET subscriptions/{id}/` return metadata only. `DELETE subscriptions/{id}/`
disables that route and cancels queued deliveries. A worker cannot inspect or
change another worker's route or the main agent's project route.

Callbacks use CloudEvents JSON (`Content-Type: application/cloudevents+json`):

```json
{
  "specversion": "1.0",
  "id": "EVENT_UUID",
  "type": "question.answered",
  "source": "/projects/PROJECT_UUID/coordination",
  "subject": "work-items/TASK_UUID",
  "time": "EVENT_TIME",
  "datacontenttype": "application/json",
  "data": {
    "project_id": "PROJECT_UUID",
    "issue_id": "TASK_UUID",
    "actor_id": "HUMAN_UUID",
    "spec_revision": 1,
    "sequence": 43,
    "question_id": "QUESTION_UUID",
    "run_id": "RUN_UUID"
  }
}
```

Event payloads contain references and change metadata, not API credentials, file
bytes or signed download links. The worker fetches current content through its
assigned API. `GET runs/{run_id}/assets/{asset_id}/` returns an authenticated
download URL for confirmed uploads on the root or granted children, including
inline comment files. Unuploaded or unrelated assets are not accessible.

Headers follow Standard Webhooks:

- `webhook-id`: stable delivery UUID, unchanged by retries and manual replay.
- `webhook-timestamp`: Unix time of this delivery attempt.
- `webhook-signature`: `v1,` followed by Base64 HMAC-SHA256 of
  `{webhook-id}.{webhook-timestamp}.{raw_body}`. The key is the Base64-decoded
  secret after removing its `whsec_` prefix.

The receiver verifies the signature and timestamp using its configured replay
tolerance, durably queues the notification, and returns 2xx. It then starts or
resumes its agent session. Acknowledging receipt is not a declaration that the
agent completed the work. Deduplicate delivery IDs and domain event IDs; retries
and catch-up can legitimately expose the same event again. Agent-authored
comments also generate events, so use `actor_id` to avoid self-trigger loops.
Serialize or coalesce callbacks into the assigned worker's work loop: refreshing
execution access rotates its prior execution token, not another worker's token.

## Feedback and Specification Changes

Direct worker events include explicit answers; comment creation, edits and
deletion; completed/deleted uploads; native task-field/specification changes;
module changes; granted-child feedback; review decisions; cancellation and merge.
Upload notifications occur after confirmation, not when a presigned upload is
merely issued. The same model/service hooks cover UI and external API writes.

Requirement, definition-of-done or description changes invalidate execution keys,
advance merge authorization and old-revision evidence/approvals, but retain the
assignment and callback. The worker reads the new specification and explicitly
refreshes against its current revision. Stale refreshes return 409. A refreshed
revision clears old PR/head bindings, requiring fresh implementation reporting
and applicable human approvals. Existing unresolved questions and required
child scope remain gates rather than silently disappearing on a spec edit.

## Delivery Recovery and Operations

Events and delivery rows commit with the business transaction. Celery dispatch
is attempted after commit. A broker outage leaves pending rows in the outbox;
the scheduled sweep recovers them. A delivery interrupted during sending becomes
eligible again after its transport lock expires. Delivery ordering is not
guaranteed; use project sequence numbers and scoped catch-up to reconcile state.

- `GET subscriptions/{id}/deliveries/`: paginated status, attempts, next attempt,
  HTTP status and sanitized errors. Response bodies are not stored.
- `POST subscriptions/{id}/deliveries/{delivery_id}/replay/`: queue another
  delivery cycle with the same delivery ID, including after exhausted retries.
- `GET runs/{run_id}/events/?after={sequence}`: durable task-scoped catch-up.
  Save the returned `next_cursor` only after durably handling the page and drain
  further pages while `has_more` is true.

The inherited webhook defaults are a 30-second HTTP timeout, 600-second retry
base and five retries after the initial attempt. Operators can configure
`COORDINATION_WEBHOOK_TIMEOUT`, `COORDINATION_WEBHOOK_RETRY_DELAY` and
`COORDINATION_WEBHOOK_MAX_RETRIES`. Failed rows remain visible and replayable.
The recovery sweep follows the configured retry delay. API-key rate accounting
continues sharing the parent dispatcher's budget, including assignment keys.

Production callbacks require public HTTPS endpoints. DNS is checked at
registration and delivery; the connection pins a validated IP while preserving
TLS hostname verification. Redirects are not followed. Private test receivers
require an explicit `WEBHOOK_ALLOWED_IPS` CIDR allowlist; do not broadly allow
private networks in production. Plane needs no additional inbound public port.

Protocol references: [CloudEvents](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md)
and [Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md).

## Local End-to-End Fixture

`apps/api/plane/tests/fixtures/agent_handoff_receiver.py` is an external,
deterministic protocol receiver, not a production orchestrator or an AI model.
Run it only on a private test network, with its published status port bound to
loopback. Its config and SQLite state contain credentials and must stay private.
The receiver requires explicit `--config`, `--state-dir`, `--host` and `--port`.

It records signed callbacks durably, performs main-agent claim/handoff once,
then has its worker identity acknowledge answers, read attachment contents and
post task receipts using only worker access. `/status` exposes IDs, event receipts
and main/worker API-call counters, never secrets. The main counter should remain
unchanged while you answer a question, add feedback/files or edit the task spec.
Use a dedicated test project; do not attach this deterministic receiver to real
production work. Your real runtime replaces its test behavior with your own
prompts, model calls and session-resume adapter.
