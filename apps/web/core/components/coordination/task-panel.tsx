/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import {
  Check,
  CheckCheck,
  ExternalLink,
  GitPullRequest,
  Plus,
  RefreshCw,
  Save,
  Send,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import useSWR from "swr";
import { API_BASE_URL } from "@plane/constants";
import { Button } from "@plane/propel/button";
import type { TAcceptanceCriterion, TContractStatus } from "@plane/types";
import { CoordinationService } from "@/services/coordination.service";
import { coordinationError } from "./helpers";

const service = new CoordinationService();
const field =
  "w-full min-w-0 rounded border border-subtle bg-surface-1 px-3 py-2 text-body-sm-regular disabled:opacity-60";
const statuses: Record<TContractStatus, string> = {
  draft: "Draft",
  ready: "Ready for agent",
  in_progress: "In progress",
  awaiting_input: "Awaiting human input",
  review: "Ready for review",
  merging: "Merging",
  merged: "Done",
  cancelled: "Cancelled",
};
const criterion = (): TAcceptanceCriterion => ({ id: crypto.randomUUID(), description: "" });

type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  actorId: string;
  creatorId: string;
  members: { id: string; name: string }[];
  canAdmin: boolean;
  canWrite: boolean;
};

export function TaskCoordinationPanel(props: Props) {
  const { workspaceSlug, projectId, issueId, actorId, creatorId, members, canAdmin, canWrite } = props;
  const {
    data,
    error: loadError,
    mutate,
    isLoading,
  } = useSWR(["task-coordination", workspaceSlug, projectId, issueId], () =>
    service.overview(workspaceSlug, projectId, issueId)
  );
  const { data: releases, error: releasesError } = useSWR(["coordination-releases", workspaceSlug, projectId], () =>
    service.releases(workspaceSlug, projectId)
  );
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [requirements, setRequirements] = useState("");
  const [criteria, setCriteria] = useState<TAcceptanceCriterion[]>([]);
  const [owner, setOwner] = useState(creatorId || actorId);
  const [release, setRelease] = useState("");
  const [revision, setRevision] = useState<number | undefined>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [reviewComment, setReviewComment] = useState("");
  const [advancePending, setAdvancePending] = useState<boolean | null>(null);
  const [reviewersPending, setReviewersPending] = useState<string[] | null>(null);
  const contract = data?.contract;
  const run = data?.runs[0];
  const isOwner = actorId === (contract?.owner ?? creatorId);
  const frozen = contract?.status === "merging" || contract?.status === "merged";
  const canControl = canWrite && (isOwner || canAdmin);
  const canEdit = canControl && !frozen;
  const canReview =
    canWrite &&
    !frozen &&
    run?.status === "active" &&
    !!run.head_sha &&
    (isOwner || data?.reviewers.some((row) => row.reviewer === actorId));
  const memberName = (id: string) => members.find((member) => member.id === id)?.name ?? id;
  const stale = contract && revision !== undefined && contract.spec_revision !== revision;

  useEffect(() => {
    if (dirty || !contract) return;
    setRequirements(contract.requirements);
    setCriteria(contract.definition_of_done.length ? contract.definition_of_done : [criterion()]);
    setOwner(contract.owner);
    setRelease(contract.release ?? "");
    setRevision(contract.spec_revision);
  }, [contract, dirty]);

  useEffect(() => {
    if (!dirty) return;
    const preventClose = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventClose);
    return () => window.removeEventListener("beforeunload", preventClose);
  }, [dirty]);

  async function action(operation: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await operation();
      await mutate();
    } catch (failure) {
      setError(coordinationError(failure));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    await service.saveContract(workspaceSlug, projectId, issueId, {
      requirements,
      definition_of_done: criteria.filter((item) => item.description.trim()),
      owner,
      release: release || null,
      expected_revision: revision,
    });
    setDirty(false);
    setEditing(false);
  }

  if (isLoading)
    return (
      <section className="border-y border-subtle py-5 text-body-sm-regular" aria-busy="true">
        Loading agent workflow...
      </section>
    );
  if (loadError)
    return (
      <section className="border-y border-subtle py-5">
        <p role="alert">{coordinationError(loadError)}</p>
        <Button variant="secondary" onClick={() => void mutate()}>
          Retry
        </Button>
      </section>
    );

  return (
    <section className="min-w-0 space-y-5 border-y border-subtle py-5 text-body-sm-regular" aria-label="Agent workflow">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <ShieldCheck className="size-4 shrink-0" />
          <h2 className="text-body-md-medium">Agent workflow</h2>
          <span className="text-tertiary">
            {contract ? `${statuses[contract.status]} / Spec ${contract.spec_revision}` : "Not configured"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {canEdit && !editing && (
            <Button
              variant="secondary"
              onClick={() => {
                setEditing(true);
                if (!criteria.length) setCriteria([criterion()]);
              }}
            >
              {contract ? "Edit specification" : "Configure task"}
            </Button>
          )}
          <button
            type="button"
            className="grid size-8 shrink-0 place-items-center rounded hover:bg-layer-2"
            title="Refresh workflow"
            aria-label="Refresh workflow"
            disabled={busy}
            onClick={() => void mutate()}
          >
            <RefreshCw className="size-4" />
          </button>
        </div>
      </header>
      {(error || releasesError) && (
        <p role="alert" className="break-words text-danger-primary">
          {error || coordinationError(releasesError)}
        </p>
      )}
      {stale && dirty && (
        <p role="alert" className="text-danger-primary">
          A newer specification is available. Your edits have not been saved.
        </p>
      )}
      {editing && canEdit ? (
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void action(save);
          }}
        >
          <fieldset disabled={busy} className="min-w-0 space-y-4">
            <label className="block space-y-1">
              <span>Requirements</span>
              <textarea
                className={field}
                rows={4}
                value={requirements}
                onChange={(event) => {
                  setRequirements(event.target.value);
                  setDirty(true);
                }}
              />
            </label>
            <div className="space-y-2">
              <h3 className="text-body-sm-medium">Definition of done</h3>
              {criteria.map((item, index) => (
                <div key={item.id} className="flex items-start gap-2">
                  <textarea
                    aria-label={`Acceptance criterion ${index + 1}`}
                    className={field}
                    rows={2}
                    value={item.description}
                    onChange={(event) => {
                      setCriteria(
                        criteria.map((row) => (row.id === item.id ? { ...row, description: event.target.value } : row))
                      );
                      setDirty(true);
                    }}
                  />
                  <button
                    type="button"
                    className="grid size-9 shrink-0 place-items-center rounded hover:bg-layer-2"
                    title="Remove criterion"
                    aria-label={`Remove criterion ${index + 1}`}
                    onClick={() => {
                      setCriteria(criteria.filter((row) => row.id !== item.id));
                      setDirty(true);
                    }}
                  >
                    <Trash2 className="size-4" />
                  </button>
                </div>
              ))}
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setCriteria([...criteria, criterion()]);
                  setDirty(true);
                }}
              >
                <Plus className="size-4" />
                Add criterion
              </Button>
            </div>
            <div className="grid min-w-0 gap-3 sm:grid-cols-2">
              <label className="block min-w-0 space-y-1">
                <span>Human owner</span>
                <select
                  aria-label="Human owner"
                  className={field}
                  value={owner}
                  onChange={(event) => {
                    setOwner(event.target.value);
                    setDirty(true);
                  }}
                >
                  {!members.some((member) => member.id === owner) && <option value={owner}>{memberName(owner)}</option>}
                  {members.map((member) => (
                    <option key={member.id} value={member.id}>
                      {member.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block min-w-0 space-y-1">
                <span>Project release</span>
                <select
                  aria-label="Project release"
                  className={field}
                  value={release}
                  disabled={!!releasesError}
                  onChange={(event) => {
                    setRelease(event.target.value);
                    setDirty(true);
                  }}
                >
                  <option value="">No release</option>
                  {releases?.map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.identifier} / {row.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button type="submit" disabled={!!stale}>
                <Save className="size-4" />
                Save specification
              </Button>
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setDirty(false);
                  setEditing(false);
                }}
              >
                Cancel
              </Button>
              {dirty && <span className="text-tertiary">Unsaved changes</span>}
            </div>
          </fieldset>
        </form>
      ) : (
        contract && (
          <div className="space-y-3">
            <div className="grid gap-2 sm:grid-cols-2">
              <p className="min-w-0 break-words">
                <span className="text-tertiary">Owner: </span>
                {memberName(contract.owner)}
              </p>
              <p className="min-w-0 break-words">
                <span className="text-tertiary">Release: </span>
                {releases?.find((row) => row.id === contract.release)?.name ?? "None"}
              </p>
            </div>
            <p className="break-words whitespace-pre-wrap">{contract.requirements || "No requirements"}</p>
            <h3 className="text-body-sm-medium">Definition of done</h3>
            <ul className="space-y-2">
              {contract.definition_of_done.map((item) => (
                <li key={item.id} className="flex gap-2">
                  <CheckCheck className="mt-0.5 size-4 shrink-0 text-tertiary" />
                  <span className="min-w-0 break-words whitespace-pre-wrap">{item.description}</span>
                </li>
              ))}
            </ul>
          </div>
        )
      )}
      {contract && (
        <>
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex min-w-0 items-center gap-2">
              <input
                type="checkbox"
                checked={advancePending ?? contract.auto_merge_authorized}
                disabled={!isOwner || !canWrite || frozen || busy || dirty}
                onChange={(event) => {
                  const enabled = event.target.checked;
                  setAdvancePending(enabled);
                  void action(() =>
                    service.authorizeAdvance(workspaceSlug, projectId, issueId, contract.spec_revision, enabled)
                  ).finally(() => setAdvancePending(null));
                }}
              />
              <span>Authorize automatic merge</span>
            </label>
            {["draft", "cancelled"].includes(contract.status) && canEdit && (
              <Button
                disabled={busy || dirty}
                onClick={() => void action(() => service.submit(workspaceSlug, projectId, issueId))}
              >
                <Send className="size-4" />
                {contract.status === "cancelled" ? "Resubmit to agents" : "Submit to agents"}
              </Button>
            )}
          </div>
          <details className="space-y-3" open={!!data?.reviewers.length || !!canReview}>
            <summary className="cursor-pointer text-body-sm-medium">
              Required reviewers ({data?.reviewers.length ?? 0})
            </summary>
            <div className="grid gap-2 sm:grid-cols-2">
              {members.map((member) => (
                <label key={member.id} className="flex min-w-0 items-center gap-2">
                  <input
                    type="checkbox"
                    checked={(reviewersPending ?? data?.reviewers.map((row) => row.reviewer) ?? []).includes(member.id)}
                    disabled={!isOwner || !canWrite || frozen || busy || dirty}
                    onChange={(event) => {
                      const selected = data?.reviewers.map((row) => row.reviewer) ?? [];
                      const next = event.target.checked
                        ? [...selected, member.id]
                        : selected.filter((id) => id !== member.id);
                      setReviewersPending(next);
                      void action(() =>
                        service.reviewers(workspaceSlug, projectId, issueId, contract.spec_revision, next)
                      ).finally(() => setReviewersPending(null));
                    }}
                  />
                  <span className="min-w-0 break-words">{member.name}</span>
                </label>
              ))}
            </div>
            {data?.reviewers.map((requirement) => {
              const latest = data.decisions.find(
                (row) =>
                  row.reviewer === requirement.reviewer &&
                  row.run === run?.id &&
                  row.code_revision === run.code_revision &&
                  row.spec_revision === contract.spec_revision &&
                  Date.parse(row.created_at) >= Date.parse(requirement.requested_at)
              );
              return (
                <p key={requirement.id} className="break-words">
                  {memberName(requirement.reviewer)}:{" "}
                  {latest?.decision === "approve"
                    ? "Approved"
                    : latest?.decision === "changes_requested"
                      ? "Changes requested"
                      : "Pending"}
                </p>
              );
            })}
          </details>
          {run && (
            <div className="space-y-2 border-t border-subtle pt-4">
              <h3 className="text-body-sm-medium">Execution / Attempt {run.attempt}</h3>
              <p className="break-words">
                {run.worker_id} / {run.status} / Spec {run.spec_revision}
              </p>
              <p className="text-tertiary">Last heartbeat: {new Date(run.last_heartbeat).toLocaleString()}</p>
              {run.pull_request && (
                <a
                  className="inline-flex max-w-full items-center gap-2 break-all text-accent-primary"
                  href={`https://github.com/${run.repository}/pull/${run.pull_request}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  <GitPullRequest className="size-4 shrink-0" />
                  {run.repository} #{run.pull_request}
                  <ExternalLink className="size-3 shrink-0" />
                </a>
              )}
              {run.head_sha && <p className="font-mono text-caption-sm-regular break-all">Head: {run.head_sha}</p>}
              {run.last_error && (
                <p role="status" className="break-words text-danger-primary">
                  {run.last_error}
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                {canEdit && run.status === "active" && (
                  <Button
                    variant="secondary"
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm("Cancel this agent execution?"))
                        void action(() => service.cancel(workspaceSlug, projectId, run.id));
                    }}
                  >
                    Cancel execution
                  </Button>
                )}
                {canControl && run.status === "merging" && (
                  <Button
                    variant="secondary"
                    disabled={busy}
                    onClick={() => void action(() => service.reconcile(workspaceSlug, projectId, run.id))}
                  >
                    <RefreshCw className="size-4" />
                    Check merge outcome
                  </Button>
                )}
              </div>
              {canReview && (
                <div className="space-y-2">
                  <label className="block space-y-1">
                    <span>Review notes</span>
                    <textarea
                      className={field}
                      rows={2}
                      value={reviewComment}
                      onChange={(event) => setReviewComment(event.target.value)}
                    />
                  </label>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      disabled={busy || dirty}
                      onClick={() =>
                        void action(() =>
                          service.decide(workspaceSlug, projectId, issueId, run, "approve", reviewComment)
                        )
                      }
                    >
                      <Check className="size-4" />
                      Approve merge
                    </Button>
                    <Button
                      variant="secondary"
                      disabled={busy || dirty || !reviewComment.trim()}
                      onClick={() =>
                        void action(() =>
                          service.decide(workspaceSlug, projectId, issueId, run, "changes_requested", reviewComment)
                        )
                      }
                    >
                      <X className="size-4" />
                      Request changes
                    </Button>
                  </div>
                </div>
              )}
              {data?.decisions
                .filter((row) => row.run === run.id)
                .map((row) => (
                  <p key={row.id} className="break-words text-tertiary">
                    {memberName(row.reviewer)}: {row.decision === "approve" ? "Approved" : "Changes requested"} / Code{" "}
                    {row.code_revision}
                    {row.code_revision !== run.code_revision ? " / Superseded" : ""}
                    {row.comment ? ` / ${row.comment}` : ""}
                  </p>
                ))}
            </div>
          )}
          {!!data?.questions.length && (
            <div className="space-y-4 border-t border-subtle pt-4">
              <h3 className="text-body-sm-medium">Questions</h3>
              {data.questions.map((question) => (
                <div key={question.id} className="space-y-2">
                  <p className="break-words whitespace-pre-wrap">{question.text}</p>
                  <p className="text-tertiary">
                    {memberName(question.respondent)} /{" "}
                    {question.resolved_at
                      ? "Resolved"
                      : question.answer_comment
                        ? "Answered, awaiting agent"
                        : question.blocking
                          ? "Blocking"
                          : "Open"}{" "}
                    / Spec {question.spec_revision}
                  </p>
                  {question.answer && <p className="break-words whitespace-pre-wrap">{question.answer}</p>}
                  {!question.resolved_at &&
                    question.spec_revision === contract.spec_revision &&
                    canWrite &&
                    !frozen &&
                    [question.respondent, contract.owner].includes(actorId) && (
                      <form
                        className="space-y-2"
                        onSubmit={(event) => {
                          event.preventDefault();
                          void action(() =>
                            service.answer(workspaceSlug, projectId, issueId, question.id, answers[question.id] ?? "")
                          );
                        }}
                      >
                        <textarea
                          aria-label={`Answer: ${question.text}`}
                          className={field}
                          rows={2}
                          value={answers[question.id] ?? ""}
                          onChange={(event) => setAnswers({ ...answers, [question.id]: event.target.value })}
                        />
                        <Button type="submit" disabled={busy || !answers[question.id]?.trim()}>
                          <Send className="size-4" />
                          Post answer
                        </Button>
                      </form>
                    )}
                </div>
              ))}
            </div>
          )}
          {!!data?.children.length && (
            <div className="space-y-2 border-t border-subtle pt-4">
              <h3 className="text-body-sm-medium">Agent subtasks</h3>
              {data.children.map((child) => (
                <div key={child.id} className="flex flex-wrap justify-between gap-2">
                  <a
                    className="min-w-0 break-words text-accent-primary"
                    href={`/${workspaceSlug}/projects/${projectId}/issues/${child.issue}/`}
                  >
                    {child.name}
                  </a>
                  <span className="text-tertiary">
                    {child.required ? "Required" : "Optional"} / {child.state_group} / Spec {child.spec_revision}
                    {child.spec_revision !== contract.spec_revision ? " / Superseded" : ""}
                  </span>
                </div>
              ))}
            </div>
          )}
          {!!data?.evidence.length && (
            <div className="space-y-4 border-t border-subtle pt-4">
              <h3 className="text-body-sm-medium">Evidence</h3>
              {data.evidence
                .slice()
                // eslint-disable-next-line unicorn/no-array-reverse -- The app targets ES2022; only the copy is mutated.
                .reverse()
                .map((item) => (
                  <div key={item.id} className="min-w-0 space-y-2">
                    <p className="break-words whitespace-pre-wrap">{item.summary}</p>
                    <p className="text-tertiary">
                      {item.kind} / {item.outcome} / Spec {item.spec_revision} / {item.head_sha.slice(0, 12)}
                    </p>
                    {item.asset_url && (
                      <a
                        className="inline-flex items-center gap-1 text-accent-primary"
                        href={`${API_BASE_URL}${item.asset_url}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <ExternalLink className="size-3" />
                        Artifact
                      </a>
                    )}
                    {item.kind === "screenshot" && item.asset_url && (
                      <img
                        className="max-h-96 max-w-full rounded border border-subtle object-contain"
                        src={`${API_BASE_URL}${item.asset_url}`}
                        alt={item.summary}
                        loading="lazy"
                      />
                    )}
                    {item.url && (
                      <a
                        className="block break-all text-accent-primary"
                        href={item.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {item.url}
                      </a>
                    )}
                  </div>
                ))}
            </div>
          )}
          {contract.merged_sha && (
            <p className="font-mono text-caption-sm-regular break-all">Merged: {contract.merged_sha}</p>
          )}
        </>
      )}
    </section>
  );
}
