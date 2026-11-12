import { useState } from "react";
import useSWR from "swr";
import { observer } from "mobx-react";
import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import { TaskReleaseSelect } from "@/components/coordination/task-release-select";
import { coordinationError } from "@/components/coordination/helpers";
import { useUser, useUserPermissions } from "@/hooks/store/user";
import { CoordinationService } from "@/services/coordination.service";
import { useTaskWriteState } from "@/hooks/use-task-write-state";

const service = new CoordinationService();
type Props = { workspaceSlug: string; projectId: string; issueId: string; disabled: boolean };

export const IssueReleaseSelect = observer(function IssueReleaseSelect({
  workspaceSlug,
  projectId,
  issueId,
  disabled,
}: Props) {
  const { data: user } = useUser();
  const writeState = useTaskWriteState(workspaceSlug, projectId, issueId);
  const { allowPermissions } = useUserPermissions();
  const { data, mutate } = useSWR(["task-coordination", workspaceSlug, projectId, issueId], () =>
    service.overview(workspaceSlug, projectId, issueId)
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const contract = data?.contract;
  const canEdit =
    !!user &&
    !!contract &&
    !data?.inherited_from &&
    !["merging", "merged"].includes(contract.status) &&
    (contract.owner_ids.includes(user.id) ||
      allowPermissions([EUserPermissions.ADMIN], EUserPermissionsLevel.PROJECT, workspaceSlug, projectId));
  async function change(release: string | null) {
    if (!contract) return;
    setBusy(true);
    setError("");
    await writeState.begin();
    try {
      await service.updateTask(workspaceSlug, projectId, issueId, {
        release_id: release,
        expected_revision: contract.spec_revision,
      });
      await mutate();
    } catch (failure) {
      setError(coordinationError(failure));
    } finally {
      await writeState.end();
      setBusy(false);
    }
  }
  return (
    <div className="min-w-0">
      <TaskReleaseSelect
        workspaceSlug={workspaceSlug}
        projectId={projectId}
        value={contract?.release ?? null}
        onChange={(release) => void change(release)}
        disabled={disabled || !canEdit || busy || writeState.pending}
      />
      {error && (
        <p role="alert" className="break-words text-danger-primary">
          {error}
        </p>
      )}
    </div>
  );
});
