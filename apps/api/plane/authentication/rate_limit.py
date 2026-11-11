# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import hashlib
import os

# Django imports
from django.core.cache import cache
from django.http import JsonResponse

# Third party imports
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

# Module imports
from plane.authentication.adapter.error import (
    AuthenticationException,
    AUTHENTICATION_ERROR_CODES,
)
from plane.utils.ip_address import get_client_ip


AUTH_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60"))
AUTH_RATE_LIMIT_IP_ATTEMPTS = int(os.environ.get("AUTH_RATE_LIMIT_IP_ATTEMPTS", "30"))
AUTH_RATE_LIMIT_IDENTIFIER_ATTEMPTS = int(os.environ.get("AUTH_RATE_LIMIT_IDENTIFIER_ATTEMPTS", "10"))
MAGIC_CODE_FAILED_ATTEMPT_LIMIT = int(os.environ.get("MAGIC_CODE_FAILED_ATTEMPT_LIMIT", "5"))


def _rate_limit_cache_key(scope, identifier_type, identifier):
    digest = hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()
    return f"auth_rate_limit:{scope}:{identifier_type}:{digest}"


def _is_cache_key_limited(key, limit):
    if cache.add(key, 1, timeout=AUTH_RATE_LIMIT_WINDOW_SECONDS):
        return False

    try:
        attempt_count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=AUTH_RATE_LIMIT_WINDOW_SECONDS)
        return False

    return attempt_count > limit


def _get_rate_limit_ip(request):
    return request.META.get("HTTP_CF_CONNECTING_IP") or get_client_ip(request=request) or "unknown"


def is_authentication_rate_limited(request, scope, identifier=None):
    client_ip = _get_rate_limit_ip(request=request)
    is_limited = _is_cache_key_limited(
        key=_rate_limit_cache_key(scope=scope, identifier_type="ip", identifier=client_ip),
        limit=AUTH_RATE_LIMIT_IP_ATTEMPTS,
    )

    if identifier:
        is_limited = (
            _is_cache_key_limited(
                key=_rate_limit_cache_key(
                    scope=scope,
                    identifier_type="identifier",
                    identifier=str(identifier).strip().lower(),
                ),
                limit=AUTH_RATE_LIMIT_IDENTIFIER_ATTEMPTS,
            )
            or is_limited
        )

    return is_limited


def authentication_rate_limit_response(request, scope, identifier=None):
    if not is_authentication_rate_limited(request=request, scope=scope, identifier=identifier):
        return None

    exc = AuthenticationException(
        error_code=AUTHENTICATION_ERROR_CODES["RATE_LIMIT_EXCEEDED"],
        error_message="RATE_LIMIT_EXCEEDED",
    )
    return JsonResponse(exc.get_error_dict(), status=status.HTTP_429_TOO_MANY_REQUESTS)


class AuthenticationThrottle(AnonRateThrottle):
    rate = "30/minute"
    scope = "authentication"

    def throttle_failure_view(self, request, *args, **kwargs):
        try:
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["RATE_LIMIT_EXCEEDED"],
                error_message="RATE_LIMIT_EXCEEDED",
            )
        except AuthenticationException as e:
            return Response(e.get_error_dict(), status=status.HTTP_429_TOO_MANY_REQUESTS)


class EmailVerificationThrottle(UserRateThrottle):
    """
    Throttle for email verification code generation.
    Limits to 3 requests per hour per user to prevent abuse.
    """

    rate = "3/hour"
    scope = "email_verification"

    def throttle_failure_view(self, request, *args, **kwargs):
        try:
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["RATE_LIMIT_EXCEEDED"],
                error_message="RATE_LIMIT_EXCEEDED",
            )
        except AuthenticationException as e:
            return Response(e.get_error_dict(), status=status.HTTP_429_TOO_MANY_REQUESTS)
