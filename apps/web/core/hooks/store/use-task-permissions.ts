import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import { useIssueDetail } from "./use-issue-detail";
import { useUser, useUserPermissions } from "./user";

export function useTaskPermissions(workspaceSlug: string, projectId: string, issueId: string) {
  const { data: user } = useUser();
  const {
    issue: { getIssueById },
  } = useIssueDetail();
  const { allowPermissions } = useUserPermissions();
  const issue = getIssueById(issueId);
  const isOwner = !!user && (issue?.created_by === user.id || !!issue?.assignee_ids?.includes(user.id));
  const canAdmin = allowPermissions([EUserPermissions.ADMIN], EUserPermissionsLevel.PROJECT, workspaceSlug, projectId);
  const canEditIntent =
    (isOwner || canAdmin) &&
    allowPermissions(
      [EUserPermissions.ADMIN, EUserPermissions.MEMBER],
      EUserPermissionsLevel.PROJECT,
      workspaceSlug,
      projectId
    );
  return { isOwner, canAdmin, canEditIntent };
}
