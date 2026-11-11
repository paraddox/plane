# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Keep existing import paths on the canonical API-key validation and scope policy.
from plane.api.middleware.api_authentication import APIKeyAuthentication  # noqa: F401
