import { observer } from "mobx-react";
import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import { TaskCoordinationPanel } from "@/components/coordination/task-panel";
import { TaskIntentEditor } from "@/components/coordination/task-intent-editor";
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
import { useMember } from "@/hooks/store/use-member";
import { useUser, useUserPermissions } from "@/hooks/store/user";

type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  isEditable: boolean;
  isArchived: boolean;
  section: "intent" | "execution";
};

export const IssueTaskCoordination = observer(function IssueTaskCoordination(props: Props) {
  const { workspaceSlug, projectId, issueId, isEditable, isArchived, section } = props;
  const { data: user } = useUser();
  const {
    issue: { getIssueById },
  } = useIssueDetail();
  const memberStore = useMember();
  const { allowPermissions } = useUserPermissions();
  const issue = getIssueById(issueId);
  if (!user || !issue) return null;
  const Panel = section === "intent" ? TaskIntentEditor : TaskCoordinationPanel;
  return (
    <Panel
      key={issueId}
      workspaceSlug={workspaceSlug}
      projectId={projectId}
      issueId={issueId}
      actorId={user.id}
      creatorId={issue.created_by ?? user.id}
      canAdmin={allowPermissions([EUserPermissions.ADMIN], EUserPermissionsLevel.PROJECT, workspaceSlug, projectId)}
      canWrite={isEditable && !isArchived}
      members={
        section === "execution"
          ? (memberStore.project.getProjectMemberIds(projectId, false) ?? []).flatMap((id) => {
              const member = memberStore.getUserDetails(id);
              return member && !member.is_bot ? [{ id, name: member.display_name }] : [];
            })
          : []
      }
    />
  );
});
