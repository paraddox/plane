import useSWR from "swr";
import { CoordinationService } from "@/services/coordination.service";
import { coordinationError } from "./helpers";

const service = new CoordinationService();
type Props = {
  workspaceSlug: string;
  projectId: string;
  value: string | null;
  onChange: (value: string | null) => void;
  disabled?: boolean;
};

export function TaskReleaseSelect({ workspaceSlug, projectId, value, onChange, disabled }: Props) {
  const { data, error } = useSWR(projectId ? ["coordination-releases", workspaceSlug, projectId] : null, () =>
    service.releases(workspaceSlug, projectId)
  );
  return (
    <div className="min-w-0">
      <select
        aria-label="Release"
        className="w-full min-w-0 rounded bg-transparent py-1 text-body-sm-regular"
        value={value ?? ""}
        disabled={disabled || !data || !!error}
        onChange={(event) => onChange(event.target.value || null)}
      >
        <option value="">No release</option>
        {data?.map((release) => (
          <option key={release.id} value={release.id}>
            {release.identifier} / {release.name}
          </option>
        ))}
      </select>
      {error && (
        <p role="alert" className="break-words text-danger-primary">
          {coordinationError(error)}
        </p>
      )}
    </div>
  );
}
