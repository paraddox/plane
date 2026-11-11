# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import math
import os
from uuid import uuid4

from redis.exceptions import RedisError
from rest_framework.exceptions import APIException
from rest_framework.throttling import SimpleRateThrottle

from plane.settings.redis import redis_instance


def get_api_key_rate(token):
    if token.purpose == "execution" and token.delegated_by_id:
        token = token.delegated_by
    rate = token.allowed_rate_limit
    if not rate:
        rate = "300/minute" if token.is_service else os.environ.get("API_KEY_RATE_LIMIT", "60/minute")
    try:
        count, duration = SimpleRateThrottle.parse_rate(None, rate)
        if count <= 0 or duration <= 0:
            raise ValueError
        return count, duration
    except (ValueError, KeyError, TypeError, IndexError):
        raise APIKeyRateLimitUnavailable() from None


class APIKeyRateLimitUnavailable(APIException):
    status_code = 503
    default_detail = "API-key rate limiting is unavailable."
    default_code = "api_key_rate_limit_unavailable"


# Redis TIME avoids clock skew across API workers. A sliding window preserves the
# existing policy while making admission and accounting atomic across all routes.
API_KEY_RATE_LIMIT_SCRIPT = """
local time = redis.call("TIME")
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local duration = tonumber(ARGV[2])
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now - duration)
local count = redis.call("ZCARD", KEYS[1])
local allowed = 0
if count < tonumber(ARGV[1]) then
    redis.call("ZADD", KEYS[1], now, ARGV[3])
    redis.call("PEXPIRE", KEYS[1], duration)
    count = count + 1
    allowed = 1
end
local oldest = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
local reset = tonumber(oldest[2]) + duration
return {allowed, math.max(0, tonumber(ARGV[1]) - count), reset, reset - now}
"""


class ApiKeyRateThrottle(SimpleRateThrottle):
    scope = "api_key"
    rate = "60/minute"

    def allow_request(self, request, view):
        token = request.auth
        if token is None:
            return True
        self.num_requests, self.duration = get_api_key_rate(token)
        budget_id = token.delegated_by_id if token.purpose == "execution" else token.pk
        self.key = f"{self.scope}:{budget_id}"
        try:
            allowed, remaining, reset, wait = redis_instance().eval(
                API_KEY_RATE_LIMIT_SCRIPT, 1, self.key, self.num_requests, self.duration * 1000, uuid4().hex
            )
        except RedisError as exc:
            raise APIKeyRateLimitUnavailable() from exc
        self.wait_seconds = wait / 1000
        request.META["X-RateLimit-Remaining"] = remaining
        request.META["X-RateLimit-Reset"] = math.ceil(reset / 1000)
        return bool(allowed)

    def wait(self):
        return self.wait_seconds


class APIKeyRateLimitMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        from plane.coordination.access import guard_coordination_mutation

        guard_coordination_mutation(self, request)

    def get_throttles(self):
        return [ApiKeyRateThrottle()]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        for header in ("X-RateLimit-Remaining", "X-RateLimit-Reset"):
            if header in request.META:
                response[header] = request.META[header]
        return response
