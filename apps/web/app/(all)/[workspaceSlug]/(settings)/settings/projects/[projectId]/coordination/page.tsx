/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
import { Bot } from "lucide-react";
import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import { CoordinationProjectSettings } from "@/components/coordination/project-settings";
import { SettingsContentWrapper } from "@/components/settings/content-wrapper";
import { SettingsPageHeader } from "@/components/settings/page-header";
import { PageHead } from "@/components/core/page-title";
import { useUserPermissions } from "@/hooks/store/user";
import type { Route } from "./+types/page";

function AgentCoordinationSettingsPage({ params }: Route.ComponentProps) {
  const { workspaceSlug, projectId } = params;
  const { allowPermissions } = useUserPermissions();
  return (
    <SettingsContentWrapper
      header={
        <SettingsPageHeader
          leftItem={
            <div className="flex items-center gap-2">
              <Bot className="size-4" />
              Agent Coordination
            </div>
          }
        />
      }
      hugging
    >
      <PageHead title="Agent Coordination" />
      <CoordinationProjectSettings
        key={`${workspaceSlug}/${projectId}`}
        workspaceSlug={workspaceSlug}
        projectId={projectId}
        canAdmin={allowPermissions([EUserPermissions.ADMIN], EUserPermissionsLevel.PROJECT, workspaceSlug, projectId)}
      />
    </SettingsContentWrapper>
  );
}
export default observer(AgentCoordinationSettingsPage);
