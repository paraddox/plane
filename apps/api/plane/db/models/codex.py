"""Plane-owned routing and archive state; Codex owns the actual sessions."""

from uuid import uuid4

from django.conf import settings
from django.db import models


class CodexProject(models.Model):
    project = models.OneToOneField("db.Project", on_delete=models.CASCADE, primary_key=True)
    event_cursor = models.PositiveBigIntegerField(default=0)


class CodexThread(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE)
    run = models.ForeignKey("db.AgentRun", on_delete=models.CASCADE, null=True)
    role = models.CharField(max_length=20)
    host_id = models.CharField(max_length=255)
    runtime_id = models.CharField(max_length=255)
    thread_id = models.CharField(max_length=255, blank=True)
    settings = models.JSONField(default=dict)
    state = models.CharField(max_length=24, default="offline")
    active_turn = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project"], condition=models.Q(role="overseer"), name="codex_one_overseer"),
            models.UniqueConstraint(
                fields=["run", "role"], condition=models.Q(run__isnull=False), name="codex_run_role"
            ),
            models.UniqueConstraint(
                fields=["runtime_id", "thread_id"], condition=~models.Q(thread_id=""), name="codex_thread_address"
            ),
        ]


class AgentConversation(models.Model):
    issue = models.OneToOneField("db.Issue", on_delete=models.CASCADE, primary_key=True)
    chat = models.ForeignKey(CodexThread, on_delete=models.SET_NULL, null=True)
    revision = models.PositiveBigIntegerField(default=0)


class AgentChatMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    conversation = models.ForeignKey(AgentConversation, on_delete=models.CASCADE, related_name="messages")
    ordinal = models.PositiveBigIntegerField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    role = models.CharField(max_length=20)
    body = models.TextField(blank=True)
    assets = models.ManyToManyField("db.FileAsset")
    thread = models.ForeignKey(CodexThread, on_delete=models.SET_NULL, null=True)
    turn_id = models.CharField(max_length=255, blank=True)
    item_id = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=24, default="queued")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["conversation", "ordinal"], name="codex_chat_order"),
            models.UniqueConstraint(
                fields=["thread", "item_id"], condition=~models.Q(item_id=""), name="codex_chat_item"
            ),
        ]


class CodexCommand(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    thread = models.ForeignKey(CodexThread, on_delete=models.CASCADE, related_name="commands")
    sender = models.ForeignKey(CodexThread, on_delete=models.SET_NULL, null=True, related_name="sent_commands")
    event = models.ForeignKey("db.CoordinationEvent", on_delete=models.SET_NULL, null=True)
    message = models.OneToOneField(AgentChatMessage, on_delete=models.CASCADE, null=True)
    text = models.TextField()
    state = models.CharField(max_length=24, default="queued")
    turn_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["thread", "event"], name="codex_event_once")]
