# Product and Agent Coordination

Plane is the coordination and authorization layer. The execution infrastructure
still checks out code, runs tools/tests, pushes branches, and opens pull requests.
Plane supplies versioned product intent, scoped machine access, questions,
evidence, human decisions, and an agent-initiated guarded merge.

The main agent claims a task and hands it off once. Its worker subscribes directly
to task notifications and refreshes its own execution access. Plane does not host
agent prompts or sessions. See [Agent Notifications](agent-notifications.md) for
the callback protocol, replay and runtime-independent integration example.

## Project Setup

1. Apply the database migrations, including PostgreSQL completion guards.
2. In project settings, open **Agent coordination** and enable execution. Optional
   required CI check names apply to the project; context URLs and releases are
   project-local too. Repository access and target branches are configured in the
   external agent infrastructure, not this page. The dispatcher supplies the
   repository and target branch when claiming a task.
3. In **Agent coordination → Registered Agents**, enter a main agent name and
   select **Create main agent**. A human project administrator gets a new
   project-scoped bot identity and its API key in a one-time dialog. The key is
   stored hashed; lists and reloads never return its secret. The key icon beside
   each main agent opens API-key management to issue additional keys or revoke
   individual ones. Revoking a key also removes access from workers delegated
   through that key; it does not revoke other keys for the same main agent.

   Operators may also register identities with the command below. The responsible
   owner must be an active human project administrator:

   ```sh
   python manage.py register_coordination_agent "$PROJECT_ID" \
     --email "$DISPATCHER_EMAIL" --name "$DISPATCHER_NAME" \
     --role dispatcher --owner "$HUMAN_ADMIN_ID"

   python manage.py register_coordination_agent "$PROJECT_ID" \
     --email "$WORKER_EMAIL" --name "$WORKER_NAME" \
     --role worker --owner "$HUMAN_ADMIN_ID"
   ```

   Dispatcher registration returns a bootstrap key once. Store it in the trusted
   orchestrator's secret store, not task text or an LLM prompt. Workers get stable
   bot identities but no long-lived bootstrap keys. Existing human accounts
   cannot be converted into bots; bots cannot use browser sessions.

4. Configure the GitHub App integration on the backend. Keep its private key out
   of Plane project settings, browser bundles, workers, and comments:

   ```text
   COORDINATION_GITHUB_APP_ID=<app client ID or app ID>
   COORDINATION_GITHUB_PRIVATE_KEY=<real multiline PEM, injected securely>
   COORDINATION_GITHUB_INSTALLATIONS={"PROJECT_UUID":{"owner/repository":INSTALLATION_ID}}
   COORDINATION_GITHUB_HTTP_TIMEOUT=<operator-selected positive seconds>
   ```

   Installation bindings are project-specific and operator-controlled. The App
   needs repository Contents write and Pull requests, Checks, and Commit statuses
   read permissions. Each operation obtains a repository-limited installation
   token. Do not configure the App as a branch-protection bypass actor. GitHub's
   required checks/reviews/rules still apply; Plane approval does not impersonate
   a native GitHub reviewer. Validate your repository rules with the installation
   before enabling production delivery. A rejection leaves work open for fixes.

   Guarded delivery uses merge commits with an explicit target branch and exact
   approved head SHA. Squash and rebase are intentionally not offered: the PR
   merge endpoint's mutable base cannot be pinned atomically. Completion verifies
   ancestry on the configured target, not merely a PR's `merged` flag.

5. Set a measured/operator-approved rate on the dispatcher key when needed:

   ```sh
   python manage.py set_api_key_rate "$DISPATCHER_CREDENTIAL_ID" --rate "$OPERATOR_RATE"
   ```

   Service dispatchers inherit the existing service-key rate policy. All delegated
   execution keys share their dispatcher's budget. Issuing or rotating keys does
   not reset that budget. The existing deployment rate policy and proxy protections
   remain in force; this feature opens no additional production port.

Set `ENABLE_DRF_SPECTACULAR=1` to publish the existing `/api/v1/schema/` and
documentation endpoints. The production Compose backend environment passes the
GitHub configuration; no production deployment is performed by this branch.

## Human Workflow

Every work item is agent-first. Requirements and definition of done are native
fields in task creation, direct detail, and all preview layouts:

- The creator and active human assignees are owners. Agent identity belongs to
  the execution run, not the human-assignee field.
- Requirements and stable definition-of-done criteria form a versioned spec.
  Release assignment is project-local. Existing priority and multiple modules
  remain available.
- Open tasks are discoverable immediately, even with empty requirements or
  acceptance criteria. Agents claim incomplete work to ask blocking questions;
  incomplete intent cannot pass review-ready or merge. Drafts do not dispatch.
- The owner may authorize automatic merge in advance for that spec revision.
  Otherwise, the owner or designated reviewers approve the implemented code.
- Every requested reviewer is a required gate, even with advance authorization
  or an earlier owner approval. Requested reviewers can collectively authorize
  the merge without another owner click. An owner's current changes-requested
  decision blocks merge.
- Answers are explicit responses to questions. An ordinary comment does not
  resolve a blocking question; the executing agent acknowledges resolution.
- Decisions bind to the run, spec revision, code revision, head SHA, and PR.
  Changing code requires fresh review. Changing material spec inputs cancels old
  execution grants and clears advance permission. A handed-off assignment and its
  notification route survive: its worker reads and explicitly acknowledges the
  new revision before obtaining fresh execution access. Returning to an earlier SHA
  does not reactivate an older code-review decision.
- A cancellation closes the run and clears advance permission. The owner/admin
  may reopen it through the normal task state property. Repeating cancellation of an old
  cancelled run cannot cancel its replacement.

The agent, not an autonomous merge service, submits the final merge command after
its tests, human gates, and required provider checks pass. Done is set only after
the approved head is confirmed on the configured repository target. The recorded
merge SHA is the observed target commit proving that ancestry; after recovery it
may include later target commits as well. Finalization closes the run and revokes
all its execution keys. Normal Issue PATCH, bulk writes, and reclassifying an
issue's state cannot bypass the PostgreSQL completion guards.

## Machine Protocol

Authenticate with `X-Api-Key`. Keys never grant human approval rights, including
human-owned personal keys. Registered dispatcher keys dispatch work; execution
keys perform only their granted task/run actions. Unlisted operations are denied.
Assignment keys only read assigned context/events, manage their own callback
subscriptions and refresh access for that same run. They cannot perform task
writes, delegate access, approve or merge.

All paths below are relative to:

```text
/api/v1/workspaces/{slug}/projects/{project_id}/coordination/
```

### Dispatcher

| Method | Path                                            | Purpose                                     |
| ------ | ----------------------------------------------- | ------------------------------------------- |
| GET    | `agents/`, `policy/`, `releases/`               | Discover permitted project context          |
| GET    | `candidates/`                                   | Open, unclaimed tasks                       |
| GET    | `events/?after={sequence}&per_page={page_size}` | Durable project event feed                  |
| GET    | `work-items/{issue_id}/contract/`               | Versioned product specification             |
| POST   | `work-items/{issue_id}/claim/`                  | Atomically claim one task                   |
| GET    | `runs/{run_id}/`                                | Run metadata, never a recoverable raw key   |
| POST   | `runs/{run_id}/credentials/`                    | Issue/rotate the worker's execution key     |
| POST   | `runs/{run_id}/handoff/`                        | Return a durable worker assignment key once |
| POST   | `runs/{run_id}/reconcile/`                      | Observe a pending merge; never initiate one |

Claim input:

```json
{
  "agent": "REGISTERED_WORKER_UUID",
  "worker_id": "infrastructure-worker-instance-id",
  "repository": "owner/repository",
  "base_branch": "main"
}
```

Claim returns `201` with run metadata. A live competing claim returns `409`.
Claims do not expire with time. A human must cancel and reopen a task to replace
its execution; waiting for feedback cannot silently transfer it to another worker.
A possibly merged run must first be reconciled, never reassigned.

Credential issuance returns `201` with `id`, `token`, and `expired_at`, with
`Cache-Control: no-store`. Give that distinct key only to the named execution.
`expired_at` is null: worker authority follows the open work item, not a timer.
Rotation revokes older run keys. `Idempotency-Key` is rejected for this endpoint
so plaintext credentials never enter the replay-response store. If the response
is lost, issue a replacement; the previous key is revoked.

No credential renewal or execution lease is required. Heartbeats report liveness
only. After handoff, the separate assignment key calls `runs/{run_id}/refresh/`
with `expected_revision` to acknowledge the current specification and obtain or
rotate an execution key, without main-agent calls. Unchanged specifications need
no periodic refresh. Closed or cancelled tasks revoke all their run keys;
reopening does not revive those keys.
Disabled execution, removed
membership/identity access, parent-key revocation/expiry, cancellation, stale
specs, and confirmed completion fence execution keys. Authentication reads the
primary database even when business reads use a replica.

### Event Consumption

Events persist in the same transaction as each coordination change, with stable
UUIDs and commit-ordered project sequence numbers. Responses contain `events`,
`next_cursor`, and `has_more`; the page size follows the existing API pagination
policy. Persist the cursor only after processing the page. Retry failed reads,
deduplicate event IDs, and drain pages while `has_more` is true. After a
disconnect, continue from the last durable cursor and reconcile `candidates/`
and known runs. Re-reading events never grants another live claim. Workers use
`runs/{run_id}/events/` for their root and granted children only; they cannot read
the project-wide feed. Signed callbacks and durable per-subscription delivery
records are also available; see [Agent Notifications](agent-notifications.md).

### Assigned Execution

| Method | Path                                                     | Purpose                                                         |
| ------ | -------------------------------------------------------- | --------------------------------------------------------------- |
| GET    | `work-items/{issue_id}/overview/`                        | Spec, runs, questions, children, evidence, reviews, attempts    |
| POST   | `runs/{run_id}/heartbeat/`                               | Report liveness without self-renewal                            |
| POST   | `work-items/{issue_id}/questions/`                       | Ask a blocking or informational question                        |
| POST   | `work-items/{issue_id}/questions/{question_id}/resolve/` | Acknowledge an explicit human answer                            |
| POST   | `work-items/{issue_id}/children/`                        | Create reduced-scope work with server-inherited human ownership |
| PATCH  | `work-items/{issue_id}/modules/`                         | Add/remove multiple module memberships                          |
| POST   | `runs/{run_id}/head/`                                    | Report the PR and current exact head                            |
| POST   | `runs/{run_id}/evidence/`                                | Append revision-bound test/artifact evidence                    |
| POST   | `runs/{run_id}/review-ready/`                            | Report development/testing ready for human review               |
| POST   | `runs/{run_id}/merge/`                                   | Same agent initiates/retries the fenced target merge            |
| POST   | `runs/{run_id}/reconcile/`                               | Observe confirmation without another write                      |

The existing v1 work-item endpoints permit scoped reads/updates, append-only
comments, relations between granted targets, and owned artifact uploads.
Execution credentials cannot delete work items, including through a `deleted_at`
PATCH. Personal keys may read coordinated roots and children, but mutations to
either require the assigned execution credential or the human session workflow.
Personal-key mutations lock and recheck their task targets before writing so
concurrent contract creation cannot bypass this restriction.
Project context is read-only. Bulk mutations, credential delegation, reviewer
decisions, actor/owner rewriting, arbitrary task access and root reparenting are
not execution capabilities. Creating a child extends targets server-side; task
IDs in arbitrary headers or bodies never grant access. Children inherit owner,
release, and modules unless explicit same-project modules are supplied. Required
children and their acceptance criteria gate the parent's completion.

Representative inputs:

```json
{ "text": "Which behavior should the empty state use?", "blocking": true }
```

Explicit answers are snapshots, independent of edits to their ordinary comment
echoes. Resolve a question with the `answer_comment` ID from its current overview;
a changed answer returns 409 rather than acknowledging content the agent has not
read:

```json
{ "answer_comment": "CURRENT_ANSWER_COMMENT_UUID" }
```

```json
{
  "name": "Implement the API portion",
  "requirements": "The reduced API requirement",
  "definition_of_done": [{ "description": "The API contract test passes" }],
  "required": true
}
```

```json
{ "added": ["MODULE_UUID"], "removed": [] }
```

```json
{ "head_sha": "EXACT_COMMIT_SHA", "pull_request": 123 }
```

```json
{
  "head_sha": "EXACT_COMMIT_SHA",
  "kind": "test",
  "outcome": "passed",
  "summary": "Acceptance test results and command output reference",
  "criterion_ids": ["CRITERION_UUID"],
  "url": "https://ci.example/results"
}
```

Screenshot/document evidence can instead include an `asset` UUID. First create a
presigned upload through the existing work-item attachments API, transfer the
file, and PATCH upload confirmation. Only completed, non-deleted uploads owned by
that agent **and that run** on a granted issue can become evidence. Use
`kind: screenshot` or `document`, and `outcome: info`. Artifact upload credentials
also do not support response-cache idempotency. The normal private artifact
download path is reused by the human UI.

The latest test report for every required criterion must be passing for the
current head. Reporting a new head does not certify tests; report new evidence.
The server fetches required GitHub checks independently rather than trusting an
agent's assertion that CI passed.

Merge input must name the exact current intent:

```json
{ "expected_revision": 1, "code_revision": 1, "head_sha": "EXACT_COMMIT_SHA" }
```

The server checks ownership/delegation, spec/code versions, human authorization,
required reviews, questions, children, evidence, the claimed repository/base, PR
metadata, and current required CI checks. It persists a merge fence before the
remote write. The provider operation names the configured base branch and exact
head SHA; a PR retargeting race cannot redirect it to a different branch.

`200` means the target branch is confirmed to contain the approved head, and
execution access has ended. `202` means an outcome is uncertain and the run stays
Merging. The same still-authorized agent may repeat the **same** merge command
for the **same** fenced attempt; retries cannot change its spec, head, PR, or
target. A rejection of a retry never discards an earlier unknown operation.
Admitting a concurrent retry durably marks uncertainty before its remote write;
a later rejection of the original call cannot reopen or reassign that run.
`reconcile/` is observation-only and is also available to the owning dispatcher
or human owner/admin. It never independently merges anything. Neither merge nor
reconcile supports `Idempotency-Key`: their persisted attempt is the recovery
contract, and a response-cache transaction must not roll back a remote side effect.

Known first-attempt rejection leaves the run open for fixes and fresh review.
An unknown outcome cannot be cancelled, reassigned, or edited into another head
on the assumption that it failed. Preserve the fence until target confirmation;
operator investigation is needed if permissions/provider availability prevent
that observation. No arbitrary retry count or "assume failed after N seconds"
rule is applied.

## Session-Only Actions

Human controls use the same project path under `/api/` instead of `/api/v1/`,
with normal cookie authentication and CSRF protection:

- Native task `POST /api/workspaces/{slug}/projects/{project_id}/issues/`
  accepts `requirements`, `definition_of_done` and project-local `release_id`.
  Native task `PATCH .../issues/{issue_id}/` edits these fields with
  `expected_revision`. Reads include `spec_revision`. There is no separate
  configuration or submission endpoint. Intent edits invalidate previous runs
  and advance approvals; description is included in the execution snapshot.
- `PUT work-items/{issue_id}/auto-merge/`: owner only, with `enabled` and
  `expected_revision`.
- `PUT work-items/{issue_id}/reviewers/`: owner only, with `reviewers` UUID list
  and `expected_revision`.
- `POST work-items/{issue_id}/decisions/`: owner/designated reviewer, with
  `run`, `expected_revision`, `code_revision`, `head_sha`, `decision`
  (`approve` or `changes_requested`) and optional `comment`.
- `POST work-items/{issue_id}/questions/{question_id}/answer/`: owner/intended
  respondent, with `text`.
- `POST runs/{run_id}/cancel/`: owner/admin, unless a merge is in flight.
- Release/policy administration requires a human project administrator.

Some equivalent v1 routes expose metadata but reject human-only mutations. API
keys, including personal keys owned by the human, cannot supply these decisions.

## Errors, Audit, and Trust

Validation errors return `400` with field errors or a `detail` message.
Invalid/revoked machine credentials and denied grants return `403` under the
existing authentication contract. Missing/inactive resources return `404`.
Stale versions, competing claims and unfinished workflow gates return `409`.
The existing shared rate policy returns `429`; unavailable rate/provider services
fail closed with `503`. Read current run/overview state after conflicts rather
than guessing state from a successful old replay response.

Coordination records and API audit metadata preserve stable agent, dispatcher
credential, run, attempt, worker, actor and revision identity. Raw machine keys,
request/response bodies and authorization header values are excluded from audit
logs. Revocation does not erase the audit trail. The execution infrastructure
must treat requirements, comments and external documents as untrusted task data,
not authority to expand credentials or bypass approvals. Git/repository tool
access and isolated code execution remain infrastructure responsibilities.

## Verification Boundary

The branch includes real-auth PostgreSQL/Valkey contract and concurrency tests,
HTTP GitHub App/merge fixtures, completion-guard migration checks, and a browser
workflow in `apps/web/tests/coordination.browser.mjs`. The browser workflow uses
an isolated S3 emulator for actual presigned screenshot/document transfers and
private downloads, and checks desktop/mobile controls and rendered screenshots.
No production work item, key, provider installation, or merge is used for tests.
Live GitHub installation/repository-rule behavior still requires operator setup
and staging validation with those credentials. App-shell hydration warnings were
observed separately from successful coordination interactions.

For isolated backend verification, use the normal backend test dependencies,
disposable PostgreSQL and Valkey, and test settings. Pass `--migrations` explicitly:
the repository's pytest defaults otherwise disable the completion-guard triggers.

```sh
cd apps/api
pytest --migrations plane/tests/contract/api \
  plane/tests/contract/app/test_api_token.py \
  plane/tests/contract/app/test_authentication.py \
  plane/tests/unit/authentication/test_magic_code.py \
  plane/tests/unit/middleware/test_db_routing.py
```

The browser seed is deliberately restricted to a loopback PostgreSQL database
named `plane_coordination`. With the local API and seed sharing the same
`SECRET_KEY`, apply migrations and run:

```sh
cd apps/api
COORDINATION_FIXTURE=/private/tmp/plane-coordination-fixture.json \
  python manage.py shell -c \
  'from plane.tests.fixtures.seed_coordination_browser import seed; seed()'
```

This creates synthetic accounts, two projects, and a fresh task. It writes private fixture/login
files, never prints raw credentials, and resets the synthetic owner's password;
previous fixture sessions become invalid. Start the web app at `127.0.0.1:3000`
with `VITE_API_BASE_URL=http://127.0.0.1:8000` and the API at `127.0.0.1:8000`.
Configure a local S3-compatible endpoint and `uploads` bucket for artifact checks.
Then, from the repository root with Playwright available:

```sh
COORDINATION_FIXTURE=/private/tmp/plane-coordination-fixture.json \
COORDINATION_BROWSER_OUTPUT=/private/tmp/plane-coordination-browser \
  node apps/web/tests/coordination.browser.mjs
```

The same fixture supports a focused settings regression: it makes unsaved edits,
navigates between cached projects without reloading the SPA, and checks that
policy values and release drafts reset before saving the destination project.

```sh
COORDINATION_FIXTURE=/private/tmp/plane-coordination-fixture.json \
COORDINATION_BROWSER_OUTPUT=/private/tmp/plane-coordination-policy-browser \
  node apps/web/tests/coordination-policy.browser.mjs
```

`PLAYWRIGHT_MODULE` may point to an existing Playwright installation; the browser
test does not add a production dependency or use a personal browser profile.

Provider contracts: [branch merge](https://docs.github.com/en/rest/branches/branches#merge-a-branch),
[commit comparison](https://docs.github.com/en/rest/commits/commits#compare-two-commits),
[GitHub App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app),
and [branch protections](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
