/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ArrowLeft } from "lucide-react";
import { useNavigate } from "react-router";
import { useTranslation } from "@plane/i18n";
import { IconButton } from "@plane/propel/icon-button";
import { Tooltip } from "@plane/propel/tooltip";

export function InboxBackButton({ workspaceSlug }: { workspaceSlug: string }) {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const goBack = () => {
    // React Router starts each new document at index zero, even with external history.
    const index = window.history.state?.idx;
    if (typeof index === "number" && index > 0) {
      void navigate(-1);
    } else {
      void navigate(`/${workspaceSlug}/`, { replace: true });
    }
  };

  return (
    <Tooltip tooltipContent={t("common.go_back")} position="bottom">
      <IconButton
        icon={ArrowLeft}
        size="xl"
        variant="ghost"
        className="shrink-0"
        aria-label={t("common.go_back")}
        onClick={goBack}
      />
    </Tooltip>
  );
}
