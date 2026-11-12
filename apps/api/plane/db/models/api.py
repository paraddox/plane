# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import hashlib
from uuid import uuid4

# Django imports
from django.db import models
from django.conf import settings

from .base import BaseModel


def generate_label_token():
    return uuid4().hex


def generate_token():
    return "plane_api_" + uuid4().hex


def default_api_token_scopes():
    return ["read", "write"]


def hash_api_token(token):
    if token.startswith("sha256$"):
        return token
    return "sha256$" + hashlib.sha256(token.encode("utf-8")).hexdigest()


class APIToken(BaseModel):
    # Meta information
    label = models.CharField(max_length=255, default=generate_label_token)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    last_used = models.DateTimeField(null=True)

    # Token
    token = models.CharField(max_length=255, unique=True, default=generate_token, db_index=True)

    # User Information
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bot_tokens")
    user_type = models.PositiveSmallIntegerField(choices=((0, "Human"), (1, "Bot")), default=0)
    workspace = models.ForeignKey("db.Workspace", related_name="api_tokens", on_delete=models.CASCADE, null=True)
    project = models.ForeignKey(
        "db.Project", related_name="api_tokens", on_delete=models.CASCADE, null=True, blank=True
    )
    scopes = models.JSONField(default=default_api_token_scopes)
    expired_at = models.DateTimeField(blank=True, null=True)
    is_service = models.BooleanField(default=False)
    allowed_rate_limit = models.CharField(max_length=255, default="", blank=True)
    purpose = models.CharField(
        max_length=20,
        default="personal",
        choices=[("personal", "Personal"), ("dispatcher", "Dispatcher"), ("execution", "Execution")],
    )
    delegated_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="delegated_keys"
    )
    agent_run = models.ForeignKey(
        "db.AgentRun", on_delete=models.SET_NULL, null=True, blank=True, related_name="credentials"
    )
    capabilities = models.JSONField(default=list)

    class Meta:
        verbose_name = "API Token"
        verbose_name_plural = "API Tokems"
        db_table = "api_tokens"
        ordering = ("-created_at",)

    def __str__(self):
        return str(self.user.id)

    def save(self, *args, **kwargs):
        self.token = hash_api_token(self.token)
        return super().save(*args, **kwargs)


class APIActivityLog(BaseModel):
    token_identifier = models.CharField(max_length=255)
    agent_run_id = models.UUIDField(null=True, blank=True)
    delegated_by_id = models.UUIDField(null=True, blank=True)
    worker_id = models.CharField(max_length=255, null=True, blank=True)

    # Request Info
    path = models.CharField(max_length=255)
    method = models.CharField(max_length=10)
    query_params = models.TextField(null=True, blank=True)
    headers = models.TextField(null=True, blank=True)
    body = models.TextField(null=True, blank=True)

    # Response info
    response_code = models.PositiveIntegerField()
    response_body = models.TextField(null=True, blank=True)

    # Meta information
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, null=True, blank=True)

    class Meta:
        verbose_name = "API Activity Log"
        verbose_name_plural = "API Activity Logs"
        db_table = "api_activity_logs"
        ordering = ("-created_at",)

    def __str__(self):
        return str(self.token_identifier)
