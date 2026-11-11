/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import { Bot, Check, Pencil, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import useSWR from "swr";
import { Button } from "@plane/propel/button";
import type { TExecutionPolicy, TProjectRelease, TRepositoryPolicy } from "@plane/types";
import { CoordinationService } from "@/services/coordination.service";
import { coordinationError, durationSeconds } from "./helpers";

const service = new CoordinationService();
const field =
  "w-full min-w-0 rounded border border-subtle bg-surface-1 px-3 py-2 text-body-sm-regular disabled:opacity-60";
type Props = { workspaceSlug: string; projectId: string; canAdmin: boolean };

export function CoordinationProjectSettings({ workspaceSlug, projectId, canAdmin }: Props) {
  const {
    data: policy,
    error: policyError,
    mutate: refreshPolicy,
  } = useSWR(["coordination-policy", workspaceSlug, projectId], () => service.policy(workspaceSlug, projectId));
  const {
    data: releases,
    error: releaseError,
    mutate: refreshReleases,
  } = useSWR(["coordination-releases", workspaceSlug, projectId], () => service.releases(workspaceSlug, projectId));
  const {
    data: agents,
    error: agentError,
    mutate: refreshAgents,
  } = useSWR(["coordination-agents", workspaceSlug, projectId], () => service.agents(workspaceSlug, projectId));
  const [enabled, setEnabled] = useState(false);
  const [repositories, setRepositories] = useState<(TRepositoryPolicy & { localKey: string })[]>([]);
  const [contexts, setContexts] = useState("");
  const [lifetime, setLifetime] = useState<number | "">("");
  const [lease, setLease] = useState<number | "">("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [releaseId, setReleaseId] = useState<string | null>(null);
  const [identifier, setIdentifier] = useState("");
  const [releaseName, setReleaseName] = useState("");
  const [targetDate, setTargetDate] = useState("");
  const [releaseStatus, setReleaseStatus] = useState<TProjectRelease["status"]>("planned");
  const [description, setDescription] = useState("");

  useEffect(() => {
    if (!policy || dirty) return;
    setEnabled(policy.enabled);
    setRepositories(policy.repositories.map((row) => ({ ...row, localKey: crypto.randomUUID() })));
    setContexts(policy.context_references.join("\n"));
    setLifetime(durationSeconds(policy.credential_lifetime));
    setLease(durationSeconds(policy.execution_lease));
  }, [policy, dirty]);

  const change = () => {
    setDirty(true);
    setSaved(false);
  };
  const updateRepository = (index: number, values: Partial<TRepositoryPolicy>) => {
    setRepositories(repositories.map((row, rowIndex) => (rowIndex === index ? { ...row, ...values } : row)));
    change();
  };
  async function action(operation: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await operation();
    } catch (failure) {
      setError(coordinationError(failure));
    } finally {
      setBusy(false);
    }
  }
  function editRelease(row?: TProjectRelease) {
    setReleaseId(row?.id ?? null);
    setIdentifier(row?.identifier ?? "");
    setReleaseName(row?.name ?? "");
    setTargetDate(row?.target_date ?? "");
    setReleaseStatus(row?.status ?? "planned");
    setDescription(row?.description ?? "");
  }

  return (
    <div className="min-w-0 space-y-8 text-body-sm-regular">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-h5-medium">Agent Coordination</h1>
        <button
          type="button"
          className="grid size-8 place-items-center rounded hover:bg-layer-2"
          title="Refresh project coordination"
          aria-label="Refresh project coordination"
          disabled={busy}
          onClick={() => {
            void refreshPolicy();
            void refreshReleases();
            void refreshAgents();
          }}
        >
          <RefreshCw className="size-4" />
        </button>
      </header>
      {(error || policyError || releaseError || agentError) && (
        <p role="alert" className="break-words text-danger-primary">
          {error || coordinationError(policyError || releaseError || agentError)}
        </p>
      )}
      <section className="space-y-4 border-t border-subtle pt-5" aria-label="Execution policy">
        <h2 className="text-body-md-medium">Execution Policy</h2>
        {!policy ? (
          <p aria-busy={!policyError}>Loading policy...</p>
        ) : (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              void action(async () => {
                const input: Omit<TExecutionPolicy, "project"> = {
                  enabled,
                  repositories: repositories.map(({ repository, base_branch, required_checks, merge_method }) => ({
                    repository,
                    base_branch,
                    required_checks,
                    merge_method,
                  })),
                  context_references: contexts
                    .split("\n")
                    .map((value) => value.trim())
                    .filter(Boolean),
                  credential_lifetime: lifetime === "" ? null : String(lifetime),
                  execution_lease: lease === "" ? null : String(lease),
                };
                await service.savePolicy(workspaceSlug, projectId, input);
                setDirty(false);
                setSaved(true);
                await refreshPolicy();
              });
            }}
          >
            <fieldset disabled={!canAdmin || busy} className="min-w-0 space-y-4">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(event) => {
                    setEnabled(event.target.checked);
                    change();
                  }}
                />
                Agent execution enabled
              </label>
              <div className="grid gap-4 sm:grid-cols-2">
                <label className="block space-y-1">
                  <span>Credential lifetime (seconds)</span>
                  <input
                    className={field}
                    type="number"
                    min="0"
                    step="any"
                    value={lifetime}
                    required={enabled}
                    onChange={(event) => {
                      setLifetime(event.target.value === "" ? "" : Number(event.target.value));
                      change();
                    }}
                  />
                </label>
                <label className="block space-y-1">
                  <span>Execution lease (seconds)</span>
                  <input
                    className={field}
                    type="number"
                    min="0"
                    step="any"
                    value={lease}
                    required={enabled}
                    onChange={(event) => {
                      setLease(event.target.value === "" ? "" : Number(event.target.value));
                      change();
                    }}
                  />
                </label>
              </div>
              <h3 className="text-body-sm-medium">Repositories</h3>
              {repositories.map((row, index) => (
                <div key={row.localKey} className="space-y-3 border-b border-subtle pb-4">
                  <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                    <label className="block min-w-0 space-y-1">
                      <span>Repository (owner/name)</span>
                      <input
                        className={field}
                        required
                        value={row.repository}
                        onChange={(event) => updateRepository(index, { repository: event.target.value })}
                      />
                    </label>
                    <label className="block min-w-0 space-y-1">
                      <span>Base branch</span>
                      <input
                        className={field}
                        required
                        value={row.base_branch}
                        onChange={(event) => updateRepository(index, { base_branch: event.target.value })}
                      />
                    </label>
                  </div>
                  <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                    <label className="block min-w-0 space-y-1">
                      <span>Required CI checks</span>
                      <textarea
                        className={field}
                        rows={2}
                        value={row.required_checks.join("\n")}
                        onChange={(event) =>
                          updateRepository(index, { required_checks: event.target.value.split("\n") })
                        }
                        onBlur={() =>
                          updateRepository(index, {
                            required_checks: row.required_checks.map((name) => name.trim()).filter(Boolean),
                          })
                        }
                      />
                    </label>
                    <div className="space-y-3">
                      <button
                        type="button"
                        className="grid size-8 place-items-center rounded hover:bg-layer-2"
                        title="Remove repository"
                        aria-label={`Remove repository ${index + 1}`}
                        onClick={() => {
                          setRepositories(repositories.filter((_, rowIndex) => rowIndex !== index));
                          change();
                        }}
                      >
                        <Trash2 className="size-4" />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setRepositories([
                    ...repositories,
                    {
                      repository: "",
                      base_branch: "main",
                      required_checks: [],
                      merge_method: "merge",
                      localKey: crypto.randomUUID(),
                    },
                  ]);
                  change();
                }}
              >
                <Plus className="size-4" />
                Add repository
              </Button>
              <label className="block space-y-1">
                <span>Context references</span>
                <textarea
                  className={field}
                  rows={3}
                  value={contexts}
                  onChange={(event) => {
                    setContexts(event.target.value);
                    change();
                  }}
                />
              </label>
              <div className="flex flex-wrap items-center gap-3">
                <Button type="submit" disabled={!dirty}>
                  <Save className="size-4" />
                  Save policy
                </Button>
                {dirty && <span className="text-tertiary">Unsaved changes</span>}
                {saved && (
                  <span role="status" className="inline-flex items-center gap-1">
                    <Check className="size-4" />
                    Saved
                  </span>
                )}
              </div>
            </fieldset>
          </form>
        )}
      </section>
      <section className="space-y-4 border-t border-subtle pt-5" aria-label="Project releases">
        <h2 className="text-body-md-medium">Project Releases</h2>
        {!releases ? (
          <p>Loading releases...</p>
        ) : !releases.length ? (
          <p className="text-tertiary">No releases</p>
        ) : (
          <ul className="divide-y divide-subtle">
            {releases.map((row) => (
              <li key={row.id} className="flex min-w-0 flex-wrap items-center gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <p className="break-words">
                    {row.identifier} / {row.name}
                  </p>
                  <p className="text-tertiary">
                    {row.status} / {row.target_date ?? "No target date"}
                  </p>
                </div>
                {canAdmin && (
                  <button
                    type="button"
                    className="grid size-8 shrink-0 place-items-center rounded hover:bg-layer-2"
                    title="Edit release"
                    aria-label={`Edit release ${row.identifier}`}
                    onClick={() => editRelease(row)}
                  >
                    <Pencil className="size-4" />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        {canAdmin && (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              void action(async () => {
                const input = {
                  identifier,
                  name: releaseName,
                  target_date: targetDate || null,
                  description,
                  status: releaseStatus,
                };
                if (releaseId)
                  await service.updateRelease(workspaceSlug, projectId, releaseId, {
                    ...input,
                    status: releaseStatus,
                    description,
                  });
                else await service.createRelease(workspaceSlug, projectId, input);
                editRelease();
                await refreshReleases();
              });
            }}
          >
            <fieldset disabled={busy} className="min-w-0 space-y-3">
              <h3 className="text-body-sm-medium">{releaseId ? "Edit release" : "New release"}</h3>
              <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                <label className="block min-w-0 space-y-1">
                  <span>Identifier</span>
                  <input
                    className={field}
                    required
                    value={identifier}
                    onChange={(event) => setIdentifier(event.target.value)}
                  />
                </label>
                <label className="block min-w-0 space-y-1">
                  <span>Release name</span>
                  <input
                    className={field}
                    required
                    value={releaseName}
                    onChange={(event) => setReleaseName(event.target.value)}
                  />
                </label>
                <label className="block min-w-0 space-y-1">
                  <span>Target date</span>
                  <input
                    className={field}
                    type="date"
                    value={targetDate}
                    onChange={(event) => setTargetDate(event.target.value)}
                  />
                </label>
                <label className="block min-w-0 space-y-1">
                  <span>Release status</span>
                  <select
                    aria-label="Release status"
                    className={field}
                    value={releaseStatus}
                    onChange={(event) => setReleaseStatus(event.target.value as TProjectRelease["status"])}
                  >
                    {["planned", "active", "released", "cancelled"].map((status) => (
                      <option key={status} value={status}>
                        {status}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="block space-y-1">
                <span>Description</span>
                <textarea
                  className={field}
                  rows={2}
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </label>
              <div className="flex flex-wrap gap-2">
                <Button type="submit">
                  <Save className="size-4" />
                  {releaseId ? "Save release" : "Create release"}
                </Button>
                {releaseId && (
                  <Button variant="secondary" type="button" onClick={() => editRelease()}>
                    Cancel
                  </Button>
                )}
              </div>
            </fieldset>
          </form>
        )}
      </section>
      <section className="space-y-3 border-t border-subtle pt-5" aria-label="Registered agents">
        <h2 className="text-body-md-medium">Registered Agents</h2>
        {!agents ? (
          <p>Loading agents...</p>
        ) : !agents.length ? (
          <p className="text-tertiary">No registered agents</p>
        ) : (
          <ul className="divide-y divide-subtle">
            {agents.map((agent) => (
              <li key={agent.id} className="flex items-start gap-3 py-3">
                <Bot className="mt-0.5 size-4 shrink-0" />
                <div className="min-w-0">
                  <p className="break-words">
                    {agent.name || agent.agent} / {agent.role} / {agent.is_active ? "Active" : "Inactive"}
                  </p>
                  <p className="font-mono text-caption-sm-regular break-all text-tertiary">{agent.agent}</p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
