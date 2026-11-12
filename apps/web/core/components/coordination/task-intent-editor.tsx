import { useEffect, useRef, useState } from "react";
import { RefreshCw, Save } from "lucide-react";
import useSWR from "swr";
import { CoordinationService } from "@/services/coordination.service";
import { useTaskWriteState } from "@/hooks/use-task-write-state";
import { coordinationError } from "./helpers";
import type { TaskCoordinationPanelProps } from "./task-panel";
import { TaskIntentFields } from "./task-intent-fields";
import type { TaskIntent } from "./task-intent-fields";

const service = new CoordinationService();
const empty = (): TaskIntent => ({
  requirements: "",
  definition_of_done: [{ id: crypto.randomUUID(), description: "" }],
});

export function TaskIntentEditor(props: TaskCoordinationPanelProps) {
  const { workspaceSlug, projectId, issueId, actorId, creatorId, canAdmin, canWrite } = props;
  const writeState = useTaskWriteState(workspaceSlug, projectId, issueId);
  const {
    data,
    error: loadError,
    mutate,
  } = useSWR(["task-coordination", workspaceSlug, projectId, issueId], () =>
    service.overview(workspaceSlug, projectId, issueId)
  );
  const [value, setValue] = useState<TaskIntent>(empty);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const latest = useRef(value);
  const revision = useRef<number | undefined>(undefined);
  const writes = useRef(Promise.resolve());
  const queued = useRef<TaskIntent | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const contract = data?.contract;
  const owns = contract?.owner_ids.includes(actorId) ?? actorId === creatorId;
  const canEdit =
    !!contract &&
    !data?.inherited_from &&
    canWrite &&
    (owns || canAdmin) &&
    !["merging", "merged"].includes(contract.status);
  const stale = !saving && contract && revision.current !== undefined && contract.spec_revision !== revision.current;

  useEffect(() => {
    if (!contract || dirty) return;
    const next = {
      requirements: contract.requirements,
      definition_of_done: contract.definition_of_done.length
        ? contract.definition_of_done
        : [latest.current.definition_of_done.find((item) => !item.description.trim()) ?? empty().definition_of_done[0]],
    };
    latest.current = next;
    revision.current = contract.spec_revision;
    setValue(next);
  }, [contract, dirty]);

  function save() {
    if (!dirty || !canEdit || stale) return;
    const snapshot = latest.current;
    if (queued.current === snapshot) return;
    queued.current = snapshot;
    async function persistSnapshot() {
      setSaving(true);
      setError("");
      await writeState.begin();
      try {
        const result = await service.updateTask(workspaceSlug, projectId, issueId, {
          requirements: snapshot.requirements,
          definition_of_done: snapshot.definition_of_done.filter((item) => item.description.trim()),
          expected_revision: revision.current,
        });
        revision.current = result.spec_revision;
        if (latest.current === snapshot) setDirty(false);
        await mutate();
      } catch (failure) {
        setError(coordinationError(failure));
      } finally {
        await writeState.end();
        if (queued.current === snapshot) queued.current = null;
        setSaving(false);
      }
    }
    writes.current = writes.current.then(persistSnapshot);
  }

  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const outside = (event: MouseEvent) => {
      if (event.target instanceof Node && !container.current?.contains(event.target)) save();
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("mousedown", outside, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("mousedown", outside, true);
    };
  });

  return (
    <div ref={container} className="min-w-0 space-y-2 py-4" role="region" aria-label="Task requirements">
      <TaskIntentFields
        value={value}
        disabled={!canEdit}
        onBlur={save}
        onChange={(next) => {
          latest.current = next;
          setValue(next);
          setDirty(true);
        }}
      />
      <div className="flex min-h-8 items-center gap-2 text-body-sm-regular text-tertiary">
        <span role="status">{!data ? "Loading..." : saving ? "Saving..." : dirty ? "Unsaved changes" : "Saved"}</span>
        {dirty && (
          <button
            type="button"
            title="Save task requirements"
            aria-label="Save task requirements"
            className="grid size-8 place-items-center rounded hover:bg-layer-2"
            disabled={saving || !!stale}
            onClick={save}
          >
            <Save className="size-4" />
          </button>
        )}
        {(stale || loadError || error) && (
          <button
            type="button"
            title="Reload task requirements"
            aria-label="Reload task requirements"
            className="grid size-8 place-items-center rounded hover:bg-layer-2"
            onClick={() => {
              setDirty(false);
              setError("");
              void mutate();
            }}
          >
            <RefreshCw className="size-4" />
          </button>
        )}
      </div>
      {(error || loadError || stale) && (
        <p role="alert" className="break-words text-danger-primary">
          {error || (loadError ? coordinationError(loadError) : "This task changed. Reload before saving.")}
        </p>
      )}
    </div>
  );
}
