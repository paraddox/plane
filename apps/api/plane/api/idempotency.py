# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Opt-in retry safety for JSON API mutations.

Put IdempotencyMixin first in each API base's MRO. DRF calls initial() before
selecting the handler, including ViewSet actions bound to HTTP method names.
Replay therefore follows the normal authentication, permission and throttle
checks. Views with additional handler-level authorization must also implement
check_idempotency_permissions(request, *args, **kwargs).
"""

import hashlib
import json
from functools import wraps

from django.db import transaction
from rest_framework.exceptions import ValidationError, UnsupportedMediaType
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from plane.db.models.api import APIToken
from plane.db.models.api_idempotency import APIIdempotency


def run_idempotent_mutation(view, handler, request, *args, **kwargs):
    key = request.headers.get("Idempotency-Key")
    if key is None:
        return handler(request, *args, **kwargs)
    if not key.strip():
        raise ValidationError({"Idempotency-Key": "The key must not be empty."})

    content_type = request.content_type.split(";", 1)[0].lower()
    if content_type and content_type != "application/json" and not content_type.endswith("+json"):
        raise UnsupportedMediaType(content_type)

    api_token = getattr(request, "api_token", None) or request.auth
    if not isinstance(api_token, APIToken):
        raise ValidationError({"Idempotency-Key": "Idempotency requires API-key authentication."})

    fingerprint = json.dumps(
        {
            "method": request.method,
            "path": request.get_full_path(),
            "body": json.loads(JSONRenderer().render(request.data)),
            "if_match": request.headers.get("If-Match"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    request_digest = hashlib.sha256(fingerprint).hexdigest()
    key_digest = hashlib.sha256(key.encode()).hexdigest()

    with transaction.atomic():
        # get_or_create's unique insert waits for a concurrent claimant to
        # commit or roll back. No pending claim survives a failed transaction.
        record, created = APIIdempotency.objects.using("default").get_or_create(
            api_token=api_token,
            key_digest=key_digest,
            defaults={"request_digest": request_digest},
        )
        permission_check = getattr(view, "check_idempotency_permissions", None)
        if permission_check is not None:
            permission_response = permission_check(request, *args, **kwargs)
            if permission_response is not None:
                transaction.set_rollback(True)
                return permission_response
        if not created:
            if record.request_digest != request_digest:
                return Response({"error": "This Idempotency-Key was already used for a different request."}, status=409)
            response = Response(record.response_body, status=record.response_status, headers=record.response_headers)
            response["Idempotency-Replayed"] = "true"
            return response

        response = handler(request, *args, **kwargs)
        if not 200 <= response.status_code < 300:
            transaction.set_rollback(True)
            return response

        if response.status_code == 204 or (isinstance(response, Response) and response.data is None):
            body = None
        elif isinstance(response, Response):
            body = json.loads(JSONRenderer().render(response.data))
        elif not response.streaming and response.get("Content-Type", "").startswith("application/json"):
            body = json.loads(response.content)
        else:
            raise ValidationError({"Idempotency-Key": "Only JSON mutation responses support idempotency."})

        record.response_status = response.status_code
        record.response_body = body
        record.response_headers = {
            header: response[header] for header in ("ETag", "Location", "Content-Disposition") if header in response
        }
        record.save(update_fields=["response_status", "response_body", "response_headers"])
        return response


class IdempotencyMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or "Idempotency-Key" not in request.headers:
            return
        if request.method in getattr(self, "idempotency_unsupported_methods", set()):
            raise ValidationError({"Idempotency-Key": "This operation does not store replay responses."})
        method = request.method.lower()
        handler = getattr(self, method, None)
        if handler is None:
            return

        @wraps(handler)
        def idempotent_handler(request, *args, **kwargs):
            return run_idempotent_mutation(self, handler, request, *args, **kwargs)

        setattr(self, method, idempotent_handler)
