# Direct Codex Runtime

Plane connects directly to native Codex app-server WebSockets. There is no custom
service to install on an agent machine. The connector runs beside Plane's API,
using the same backend image, database and environment:

```sh
python manage.py migrate
python manage.py run_codex --config /run/agent-infra/config.json
```

Run one connector per Plane database, supervised by your normal process/container
manager. A PostgreSQL advisory lock prevents competing queue consumers. Mount the
configuration repository read-only and provide the referenced secrets through its
private environment. Do not put raw tokens in the manifest or prompts.

## Agent Machines

Live validation used Codex CLI 0.169.0 on the Mac and 0.168.0 on SOSO, both with
the existing ChatGPT sign-in and Sol 6.1 / max. The Mac CLI was upgraded from
0.154.0 without replacing its sign-in or sessions. The upstream WebSocket transport
is explicitly [experimental and unsupported](https://developers.openai.com/codex/app-server).
Pin and validate the CLI version before upgrading hosts. Plane uses native
`thread/start`, `thread/resume`, paginated history, `turn/start`, streaming events,
dynamic tools and `thread/archive`. It does not use desktop-private APIs.

Run a separate app-server with the host's existing Codex sign-in and approved
permissions. For a loopback-only test:

```sh
codex login status
codex app-server --listen ws://127.0.0.1:18431
```

For a private Tailscale listener use native authentication, for example:

```sh
codex app-server --listen ws://TAILSCALE_IP:18431 \
  --ws-auth capability-token --ws-token-file /private/path/codex-ws-token
```

The token file contains an operator-generated private capability token; provide the
same value through the runtime's `token_env` variable on Plane. An SSH local
forward to a loopback listener also works without exposing a port. Bind only to
loopback or the private interface. Plain `ws` over Tailscale is protected by the
private network; use TLS termination for `wss`. Opening the Codex desktop app alone
does not start a reachable WebSocket listener.

Interactive approval/input requests are not automatically approved. Configure the
host's operator-approved unattended policy before enabling work. A model rejected
by the host's current sign-in must be corrected in that host/profile or the role's
`model` setting; Plane does not silently substitute a model.

## Configuration Repository

Start from [config.example.json](./config.example.json). Keep generic `AGENTS.md`,
role prompts, project guidance and common/project skills in that repository.
`instructions` paths resolve relative to the manifest and are explicitly combined
with the role guidance. The source checkout's own `AGENTS.md` is still loaded by
Codex. Install skills in Codex-recognized directories on each relevant host.
Repository access, SSH/GitHub credentials, build and deployment knowledge remain
on those hosts or in project guidance, not in Plane forms.

Create the project's main agent/key in **Project settings > Agent coordination**.
Register a worker identity with the existing `register_coordination_agent` command
and use its user ID in the manifest. Enable execution for the project. This is a
manual onboarding operation; adding a Plane project does not enroll a host.

The default model for every role is `gpt-6.1-sol` with `max` reasoning. A role can
explicitly override `model` or `config.model_reasoning_effort` in its placement.

Each project selects **one runtime and one owning machine**. Its overseer and all
task implementation/chat threads run there so they share the local project
situation. Different projects may select different machines. Role-specific host
or runtime overrides are rejected. Each role can select a working directory on
that same machine. A runtime identifies one app-server/profile/session store. Resolved
host, runtime and instructions are saved on each thread binding. Configuration
edits affect newly created bindings; existing sessions are never silently moved.
Preserve runtime identities and session stores across restarts. Migration to a
different machine requires an explicit session/workspace migration.

## Work and Conversation

New task events wake only the project overseer, which calls the Plane `dispatch`
tool. It claims the task through the existing API and creates independent
implementation and chat threads for that run. Subsequent answers, attachments,
comments and specification/review changes go directly to the task pair.

Open **Agent conversation** on a work item to chat. Messages live outside Activity
and comments. Attachments are ordinary work-item assets and remain linked by ID.
The browser receives persisted snapshots over SSE; reconnecting does not require
keeping a browser open. The chat thread can observe worker status and queue a
message with the `plane` tool. The worker consumes messages at checkpoints or in
its next idle turn. Ordinary chat never steers or interrupts its active turn.

The connector manages assignment and execution credentials per run. Both threads use the existing
task-scoped API through the tool; raw keys never enter the model context. A
specification change can be inspected with `read_spec` (including current task
details), read-only task API calls and attachment downloads using the existing
scoped assignment authority. This does not authorize writes or advance the run's
revision. Implementation must explicitly use `acknowledge_spec` before new
execution authority is issued; the chat thread cannot acknowledge on its behalf.
Human review and merge authorization continue to use the existing protected APIs.

Codex's original session, on its original machine, remains the conversation state.
Plane's transcript is a review archive, not a prompt replay. Reconnect reconciles
all history pages and stable client/item IDs. Ambiguous turn submissions are not
automatically retried. An unavailable/lost session is shown as such, never rebuilt
from the transcript. Queued feedback, delivered turns and worker acknowledgement
are separate states.

Closing/cancelling a run rejects further chat work and archives its task threads.
The project overseer remains available. Offline hosts retain pending archive work;
execution revocation does not wait for transcript delivery. Closed conversations
remain readable without a running Codex session.

## Verification

The dedicated local and SOSO projects each ran their entire thread set on their
own host. Real browser chat continued while a worker command remained active;
feedback and uploaded documents reached that worker without interruption or
another overseer turn. The SOSO worker downloaded the attachment through native
filesystem RPC, applied the feedback, acknowledged it, and shared verification
notes and a screenshot rendered in desktop/mobile chat. Browser reload and
connector restart reused the original sessions.

Cancelling both test runs immediately rejected their previously valid execution
keys (403), archived both task threads, kept the overseers idle, and preserved
all saved messages and attachments. These were isolated trials, not production
deployment or evidence of compatibility with every future CLI release.
