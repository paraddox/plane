# Agent-First Tasks

## Contract

Every normal task exposes requirements and definition-of-done fields from creation,
including empty values. Open tasks are discoverable by agents without a separate
configure or submit step. Agents may claim incomplete work to ask blocking questions;
missing acceptance criteria must still prevent review-ready/merge completion.
The creator and active human assignees are owners. Assignment of an execution agent
remains a separate run identity. Release is a normal project-local task property.
All creation, detail, side-peek, modal, full-screen peek and mobile surfaces must work.
Existing merge authorization, exact revision binding, credential fencing and required
reviewers must remain enforced. No PR or merge is authorized for the MUE-8 demo.

## Implementation

- [x] Backend: retain WorkItemContract as the single versioned specification store;
      expose requirements, definition_of_done, release_id and spec_revision on normal
      task serializers. Create the internal contract automatically for human-created
      tasks and backfill existing tasks. Agent-created children retain their inherited
      RunChild scope, rather than becoming independently dispatchable roots.
- [x] Backend: automatically queue open tasks even with incomplete specifications;
      include normal task description in execution snapshots. Preserve cancellation,
      draft, archive and terminal-state exclusions. Keep precision judgment in the agent,
      with blocking questions and stricter completion gates.
- [x] Backend: derive owner authority from creator and human assignees, remove owner
      selection, and prevent execution credentials or unrelated users from granting
      themselves ownership. Revalidate owner/reviewer membership at authorization time.
- [x] UI: shared native intent editor used by task creation and every detail/preview
      layout. No Configure task or Submit to agents controls. Release belongs with the
      normal task properties. Keep execution progress, questions and approvals available.
      Persist native intent and release in saved drafts and preserve them on publication.
- [x] Verification: focused native-task API tests, ownership/security and revision
      regressions, relevant existing backend tests, web unit/type/lint/build checks,
      browser checks for creation, all preview modes, direct detail and mobile.
- [x] Deployment: rebuild only affected local services and apply the migration while
      preserving local users, task content, uploads and credentials. Verify MUE-8 remains
      owned by Sorin and is discoverable without manually configuring it.

## Evidence

- API suite: 360 passed, one skipped. After draft support and shared validator
  changes, native/contract tests passed (22) and execution/native tests passed (88).
- Web unit tests: five passed. Type checking, production API/web builds and
  strict changed-file lint passed.
- Chrome: desktop specified creation, mobile incomplete creation, project-local
  release, list Edit freshness/save, direct detail, draft save/reopen, side peek,
  modal, full-screen peek and mobile preview passed with no page errors.
- Synthetic workflow: automatic claim, blocking question/answer/resolution,
  screenshot/document uploads, test evidence, owner and reviewer approvals passed.
  Held a release-write response to verify approval controls remain disabled until
  the new specification revision is available. No merge was requested.
- Light/dark sign-in, task and settings checks recorded zero hydration errors.
- Local migrations 0131/0132 applied after a private database backup. API checks,
  migration consistency, service health and Celery ping passed. Existing manager
  key authenticates and MUE-8 is ready with unchanged creator/content and no auto merge.

## Current State

Base branch: codex/fix-web-shell-hydration, committed UI fixes at 04059cf8d.
The native task implementation replaces the opt-in configuration/submission path.
Requirements and acceptance criteria autosave with explicit revision binding; all
preview layouts expose them and release is a normal task property. Drafts retain
the same fields without dispatching before publication. No compatibility mutation
routes are retained.
MUE-8 is 2153f6cd-6396-4fc3-bc64-ea1f3e1f6432 in project
992bbeb1-1552-4c5e-b3a3-b77b31c1fad3, workspace mue. Do not modify its text or approvals.
Local stack operations and private manager credentials live outside Git under
/private/tmp/plane-local-stack. Never print or commit credentials. The manager and
worker identities are already registered; do not create replacement keys unnecessarily.
