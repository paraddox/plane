# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import logging
import json
import time
from urllib.parse import parse_qsl, urlencode

# Django imports
from django.http import HttpRequest
from django.utils import timezone

# Third party imports
from rest_framework.request import Request

# Module imports
from plane.utils.ip_address import get_client_ip
from plane.utils.exception_logger import log_exception
from plane.bgtasks.logger_task import process_logs

api_logger = logging.getLogger("plane.api.request")


class RequestLoggerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def _should_log_route(self, request: Request | HttpRequest) -> bool:
        """
        Determines whether a route should be logged based on the request and status code.
        """
        # Don't log health checks
        if request.path == "/" and request.method == "GET":
            return False
        return True

    def __call__(self, request):
        # get the start time
        start_time = time.time()

        # Get the response
        response = self.get_response(request)

        # calculate the duration
        duration = time.time() - start_time

        # Check if logging is required
        log_true = self._should_log_route(request=request)

        # If logging is not required, return the response
        if not log_true:
            return response

        user_id = (
            request.user.id if getattr(request, "user") and getattr(request.user, "is_authenticated", False) else None
        )

        user_agent = request.META.get("HTTP_USER_AGENT", "")
        api_key = request.headers.get("X-Api-Key")
        path = request.get_full_path()
        if api_key:
            # API audit records deliberately exclude arbitrary caller-supplied values.
            path = request.path.replace(api_key, "[REDACTED]")
            user_agent = "[REDACTED]"

        # Log the request information
        api_logger.info(
            f"{request.method} {path} {response.status_code}",
            extra={
                "path": path,
                "method": request.method,
                "status_code": response.status_code,
                "duration_ms": int(duration * 1000),
                "remote_addr": get_client_ip(request),
                "user_agent": user_agent,
                "user_id": user_id,
            },
        )

        # return the response
        return response


class APITokenLogMiddleware:
    """
    Middleware to log External API requests to MongoDB or PostgreSQL.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        self.process_request(request, response)
        return response

    def process_request(self, request, response):
        api_key_header = "X-Api-Key"
        api_key = request.headers.get(api_key_header)

        # If the API key is not present, return
        if not api_key:
            return

        try:
            token = getattr(request, "auth", None)
            # Headers and free-text bodies can contain credentials or business data
            # under arbitrary names. Keep audit metadata, never their raw values.
            log_data = {
                "token_identifier": str(token.pk) if getattr(token, "pk", None) else "unauthenticated",
                "agent_run_id": str(token.agent_run_id) if getattr(token, "agent_run_id", None) else None,
                "delegated_by_id": str(token.delegated_by_id) if getattr(token, "delegated_by_id", None) else None,
                "worker_id": token.agent_run.worker_id if getattr(token, "agent_run_id", None) else None,
                "path": request.path.replace(api_key, "[REDACTED]"),
                "method": request.method,
                "query_params": urlencode(
                    [
                        (name.replace(api_key, "[REDACTED]"), "[REDACTED]")
                        for name, _ in parse_qsl(request.META.get("QUERY_STRING", ""), keep_blank_values=True)
                    ]
                ),
                "headers": json.dumps({name: "[REDACTED]" for name in request.headers}),
                "body": None,
                "response_body": None,
                "response_code": response.status_code,
                "ip_address": get_client_ip(request=request),
                "user_agent": None,
            }
            user_id = (
                str(request.user.id)
                if getattr(request, "user") and getattr(request.user, "is_authenticated", False)
                else None
            )
            # Additional fields for MongoDB
            mongo_log = {
                **log_data,
                "created_at": timezone.now(),
                "updated_at": timezone.now(),
                "created_by": user_id,
                "updated_by": user_id,
            }

            process_logs.delay(log_data=log_data, mongo_log=mongo_log)

        except Exception as e:
            log_exception(e)

        return None
