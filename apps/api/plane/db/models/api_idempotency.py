# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import models


class APIIdempotency(models.Model):
    """A successful mutation, committed in the same transaction as its result."""

    api_token = models.ForeignKey("db.APIToken", on_delete=models.CASCADE, related_name="idempotency_records")
    # SHA-256 digests keep client keys and payloads out of the claim identifier.
    key_digest = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True)
    response_body = models.JSONField(null=True)
    response_headers = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "api_idempotency"
        constraints = [
            models.UniqueConstraint(fields=["api_token", "key_digest"], name="api_idempotency_token_key_unique"),
        ]
