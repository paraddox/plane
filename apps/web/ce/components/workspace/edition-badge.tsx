/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import packageJson from "package.json";

export function WorkspaceEditionBadge() {
  return <span className="text-11 text-tertiary">v{packageJson.version}</span>;
}
