import { useState } from "react";
import { Bot, Check, Copy, Eye, EyeOff, KeyRound, Plus, RefreshCw, Trash2 } from "lucide-react";
import useSWR from "swr";
import { Button } from "@plane/propel/button";
import { Dialog, EDialogWidth } from "@plane/propel/dialog";
import type { TAgentCredential, TAgentCredentialSecret, TCoordinationAgent } from "@plane/types";
import { CoordinationService } from "@/services/coordination.service";
import { coordinationError } from "./helpers";

const service = new CoordinationService();
const field = "w-full min-w-0 rounded border border-subtle bg-surface-1 px-3 py-2 text-body-sm-regular";
const iconButton = "grid size-8 shrink-0 place-items-center rounded hover:bg-layer-2 disabled:opacity-60";
type Secret = { name: string; credential: TAgentCredentialSecret };
type Props = {
  workspaceSlug: string;
  projectId: string;
  canAdmin: boolean;
  agents: TCoordinationAgent[] | undefined;
  onRefresh: () => Promise<unknown>;
};

function AgentKeyDialog({ secret, onClose }: { secret: Secret; onClose: () => void }) {
  const [visible, setVisible] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  return (
    <Dialog open onOpenChange={onClose}>
      <Dialog.Panel width={EDialogWidth.LG} className="max-w-[calc(100%_-_2rem)] space-y-4 p-5">
        <Dialog.Title>API key created</Dialog.Title>
        <p className="text-body-sm-regular break-words">{secret.name}</p>
        <p className="text-body-sm-regular text-secondary">This secret is shown only once.</p>
        <div className="flex min-w-0 items-center gap-1">
          <input
            aria-label="Main agent API key"
            readOnly
            autoComplete="off"
            data-1p-ignore
            type={visible ? "text" : "password"}
            className={`${field} font-mono`}
            value={secret.credential.token}
          />
          <button
            type="button"
            className={iconButton}
            title={visible ? "Hide API key" : "Show API key"}
            aria-label={visible ? "Hide API key" : "Show API key"}
            onClick={() => setVisible(!visible)}
          >
            {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
          </button>
          <button
            type="button"
            className={iconButton}
            title={copied ? "Copied API key" : "Copy API key"}
            aria-label={copied ? "Copied API key" : "Copy API key"}
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(secret.credential.token);
                setCopied(true);
                setError("");
              } catch {
                setError("Could not copy API key.");
              }
            }}
          >
            {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
          </button>
        </div>
        {error && (
          <p role="alert" className="text-body-sm-regular text-danger-primary">
            {error}
          </p>
        )}
        <div className="flex justify-end">
          <Button onClick={onClose}>Done</Button>
        </div>
      </Dialog.Panel>
    </Dialog>
  );
}

function AgentCredentials({
  workspaceSlug,
  projectId,
  agent,
  onIssued,
}: {
  workspaceSlug: string;
  projectId: string;
  agent: TCoordinationAgent;
  onIssued: (secret: Secret) => void;
}) {
  const {
    data: keys,
    error: loadError,
    mutate: refresh,
  } = useSWR(["coordination-agent-credentials", workspaceSlug, projectId, agent.id], () =>
    service.agentCredentials(workspaceSlug, projectId, agent.id)
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revoking, setRevoking] = useState<TAgentCredential | null>(null);
  return (
    <div className="mt-3 min-w-0 space-y-3" aria-label={`API keys for ${agent.name}`}>
      {!revoking && (error || loadError) && (
        <p role="alert" className="break-words text-danger-primary">
          {error || coordinationError(loadError)}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="secondary"
          disabled={busy || !agent.is_active || !keys || !!loadError}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              const credential = await service.createAgentCredential(workspaceSlug, projectId, agent.id);
              onIssued({ name: agent.name, credential });
              await refresh();
            } catch (failure) {
              setError(coordinationError(failure));
            } finally {
              setBusy(false);
            }
          }}
        >
          <Plus className="size-4" />
          Create API key
        </Button>
        <button
          type="button"
          className={iconButton}
          title="Refresh API keys"
          aria-label="Refresh API keys"
          disabled={busy}
          onClick={() => void refresh().catch((failure) => setError(coordinationError(failure)))}
        >
          <RefreshCw className="size-4" />
        </button>
      </div>
      {!keys ? (
        <p aria-busy={!loadError}>Loading API keys...</p>
      ) : !keys.length ? (
        <p className="text-tertiary">No API keys</p>
      ) : (
        <ul className="divide-y divide-subtle">
          {keys.map((key) => (
            <li key={key.id} className="flex min-w-0 items-center gap-3 py-2">
              <div className="min-w-0 flex-1">
                <p className="break-words">
                  {key.is_active ? "Active" : "Revoked"} / Created {key.created_at.split("T")[0]}
                </p>
                <p className="font-mono text-caption-sm-regular break-all text-tertiary">{key.id}</p>
              </div>
              {key.is_active && (
                <button
                  type="button"
                  className={iconButton}
                  title="Revoke API key"
                  aria-label={`Revoke API key ${key.id}`}
                  disabled={busy}
                  onClick={() => setRevoking(key)}
                >
                  <Trash2 className="size-4" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {revoking && (
        <Dialog open onOpenChange={() => !busy && setRevoking(null)}>
          <Dialog.Panel width={EDialogWidth.LG} className="max-w-[calc(100%_-_2rem)] space-y-4 p-5">
            <Dialog.Title>Revoke API key?</Dialog.Title>
            <p className="text-body-sm-regular">This also stops access for workers assigned through this key.</p>
            <p className="font-mono text-caption-sm-regular break-all">{revoking.id}</p>
            {error && (
              <p role="alert" className="text-body-sm-regular break-words text-danger-primary">
                {error}
              </p>
            )}
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="secondary" disabled={busy} onClick={() => setRevoking(null)}>
                Cancel
              </Button>
              <Button
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError("");
                  try {
                    await service.revokeAgentCredential(workspaceSlug, projectId, agent.id, revoking.id);
                    await refresh(
                      (current) => current?.map((key) => (key.id === revoking.id ? { ...key, is_active: false } : key)),
                      { revalidate: false }
                    );
                    setRevoking(null);
                    await refresh();
                  } catch (failure) {
                    setError(coordinationError(failure));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <Trash2 className="size-4" />
                Revoke API key
              </Button>
            </div>
          </Dialog.Panel>
        </Dialog>
      )}
    </div>
  );
}

export function ProjectAgents({ workspaceSlug, projectId, canAdmin, agents, onRefresh }: Props) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [secret, setSecret] = useState<Secret | null>(null);
  return (
    <section id="registered-agents" className="space-y-3 border-t border-subtle pt-5" aria-label="Registered agents">
      <h2 className="text-body-md-medium">Registered Agents</h2>
      {error && (
        <p role="alert" className="break-words text-danger-primary">
          {error}
        </p>
      )}
      {canAdmin && (
        <form
          className="flex min-w-0 flex-wrap items-end gap-3"
          onSubmit={async (event) => {
            event.preventDefault();
            if (busy || secret) return;
            setBusy(true);
            setError("");
            try {
              const result = await service.createMainAgent(workspaceSlug, projectId, name.trim());
              setSecret({ name: result.agent.name, credential: result.credential });
              setSelected(result.agent.id);
              setName("");
              await onRefresh();
            } catch (failure) {
              setError(coordinationError(failure));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="block min-w-0 flex-1 space-y-1">
            <span>Main agent name</span>
            <input
              className={field}
              required
              maxLength={255}
              value={name}
              disabled={busy || !!secret}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <Button type="submit" disabled={busy || !!secret || !name.trim()}>
            <Plus className="size-4" />
            Create main agent
          </Button>
        </form>
      )}
      {!agents ? (
        <p>Loading agents...</p>
      ) : !agents.length ? (
        <p className="text-tertiary">No registered agents</p>
      ) : (
        <ul className="divide-y divide-subtle">
          {agents.map((agent) => (
            <li key={agent.id} className="min-w-0 py-3">
              <div className="flex items-start gap-3">
                <Bot className="mt-0.5 size-4 shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="break-words">
                    {agent.name || agent.agent} / {agent.role === "dispatcher" ? "Main agent" : "Worker"} /{" "}
                    {agent.is_active ? "Active" : "Inactive"}
                  </p>
                  <p className="font-mono text-caption-sm-regular break-all text-tertiary">{agent.agent}</p>
                </div>
                {canAdmin && agent.role === "dispatcher" && (
                  <button
                    type="button"
                    className={iconButton}
                    title="Manage API keys"
                    aria-label={`Manage API keys for ${agent.name}`}
                    aria-expanded={selected === agent.id}
                    onClick={() => setSelected(selected === agent.id ? null : agent.id)}
                  >
                    <KeyRound className="size-4" />
                  </button>
                )}
              </div>
              {canAdmin && selected === agent.id && agent.role === "dispatcher" && (
                <AgentCredentials
                  key={agent.id}
                  workspaceSlug={workspaceSlug}
                  projectId={projectId}
                  agent={agent}
                  onIssued={setSecret}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      {canAdmin && secret && <AgentKeyDialog secret={secret} onClose={() => setSecret(null)} />}
    </section>
  );
}
