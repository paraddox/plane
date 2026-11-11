# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed


class BaseSessionAuthentication(SessionAuthentication):
    def authenticate(self, request):
        principal = super().authenticate(request)
        if principal is not None and principal[0].is_bot:
            raise AuthenticationFailed("Agent identities must use machine credentials, not browser sessions.")
        return principal
