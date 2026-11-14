# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone

from .project import ProjectBaseModel


class ProjectRelease(ProjectBaseModel):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        ACTIVE = "active", "Active"
        RELEASED = "released", "Released"
        CANCELLED = "cancelled", "Cancelled"

    identifier = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    target_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED)

    class Meta:
        db_table = "project_releases"
        ordering = ["target_date", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "identifier"],
                condition=models.Q(deleted_at__isnull=True),
                name="coord_release_project_identifier_unique",
            )
        ]


class ProjectExecutionPolicy(ProjectBaseModel):
    enabled = models.BooleanField(default=False)

    class Meta:
        db_table = "project_execution_policies"
        constraints = [
            models.UniqueConstraint(
                fields=["project"], condition=models.Q(deleted_at__isnull=True), name="coord_project_policy_unique"
            )
        ]


class WorkItemContract(ProjectBaseModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        READY = "ready", "Ready for Agent"
        IN_PROGRESS = "in_progress", "In Progress"
        AWAITING_INPUT = "awaiting_input", "Awaiting Input"
        REVIEW = "review", "Ready for Review"
        MERGING = "merging", "Merging"
        MERGED = "merged", "Done"
        CANCELLED = "cancelled", "Cancelled"

    issue = models.OneToOneField("db.Issue", on_delete=models.CASCADE, related_name="coordination_contract")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_contracts")
    release = models.ForeignKey(
        ProjectRelease, on_delete=models.SET_NULL, null=True, blank=True, related_name="contracts"
    )
    requirements = models.TextField(blank=True)
    definition_of_done = models.JSONField(default=list)
    spec_revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.READY)
    auto_merge_authorized = models.BooleanField(default=False)
    auto_merge_revision = models.PositiveIntegerField(null=True, blank=True)
    auto_merge_authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="auto_merge_grants"
    )
    auto_merge_authorized_at = models.DateTimeField(null=True, blank=True)
    merged_sha = models.CharField(max_length=64, blank=True)
    merged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "work_item_contracts"


class CoordinationSequence(models.Model):
    project = models.OneToOneField("db.Project", on_delete=models.CASCADE, primary_key=True)
    last_value = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "coordination_sequences"


class CoordinationEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE, related_name="coordination_events")
    sequence = models.PositiveBigIntegerField()
    issue = models.ForeignKey("db.Issue", on_delete=models.SET_NULL, null=True, related_name="coordination_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    kind = models.CharField(max_length=100)
    spec_revision = models.PositiveIntegerField(null=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "coordination_events"
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["project", "sequence"], name="coord_event_project_sequence_unique")
        ]


class AgentSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE)
    dispatcher = models.ForeignKey("db.APIToken", on_delete=models.CASCADE)
    run = models.ForeignKey("db.AgentRun", on_delete=models.CASCADE, null=True, blank=True)
    url = models.URLField(max_length=1024)
    event_types = models.JSONField(default=list)
    signing_secret = models.CharField(max_length=255)
    after_sequence = models.PositiveBigIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "agent_subscriptions"
        ordering = ["created_at", "id"]


class AgentEventDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    subscription = models.ForeignKey(AgentSubscription, on_delete=models.CASCADE, related_name="deliveries")
    event = models.ForeignKey(CoordinationEvent, on_delete=models.CASCADE, related_name="deliveries")
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Pending"),
            ("sending", "Sending"),
            ("delivered", "Delivered"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
    )
    attempts = models.PositiveIntegerField(default=0)
    retry_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_status = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=100, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "agent_event_deliveries"
        ordering = ["event__sequence", "id"]
        constraints = [models.UniqueConstraint(fields=["subscription", "event"], name="agent_event_delivery_unique")]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="agent_delivery_due")]


class AgentProjectAccess(ProjectBaseModel):
    class Role(models.TextChoices):
        DISPATCHER = "dispatcher", "Dispatcher"
        WORKER = "worker", "Worker"

    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_project_access")
    role = models.CharField(max_length=20, choices=Role.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "agent_project_access"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "agent"],
                condition=models.Q(deleted_at__isnull=True),
                name="coord_agent_project_access_unique",
            )
        ]


class AgentRun(ProjectBaseModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MERGING = "merging", "Merging"
        MERGED = "merged", "Merged"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    contract = models.ForeignKey(WorkItemContract, on_delete=models.CASCADE, related_name="runs")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agent_runs")
    dispatcher = models.ForeignKey("db.APIToken", on_delete=models.SET_NULL, null=True, related_name="dispatched_runs")
    worker_id = models.CharField(max_length=255)
    attempt = models.PositiveIntegerField()
    spec_revision = models.PositiveIntegerField()
    spec_snapshot = models.JSONField(default=dict)
    repository = models.CharField(max_length=511)
    base_branch = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    last_heartbeat = models.DateTimeField(default=timezone.now)
    handed_off_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    head_sha = models.CharField(max_length=64, blank=True)
    code_revision = models.PositiveIntegerField(default=0)
    pull_request = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "agent_runs"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract"],
                condition=models.Q(status__in=["active", "merging"], deleted_at__isnull=True),
                name="coord_one_live_run_per_contract",
            ),
            models.UniqueConstraint(fields=["contract", "attempt"], name="coord_run_attempt_unique"),
        ]


class RunTarget(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="targets")
    issue = models.ForeignKey("db.Issue", on_delete=models.CASCADE)

    class Meta:
        db_table = "agent_run_targets"
        constraints = [models.UniqueConstraint(fields=["run", "issue"], name="coord_run_target_unique")]


class WorkItemQuestion(ProjectBaseModel):
    contract = models.ForeignKey(WorkItemContract, on_delete=models.CASCADE, related_name="questions")
    run = models.ForeignKey(AgentRun, on_delete=models.RESTRICT, related_name="questions")
    spec_revision = models.PositiveIntegerField()
    text = models.TextField()
    blocking = models.BooleanField(default=True)
    respondent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agent_questions")
    question_comment = models.ForeignKey("db.IssueComment", on_delete=models.RESTRICT, related_name="agent_questions")
    answer_comment = models.ForeignKey(
        "db.IssueComment", on_delete=models.RESTRICT, related_name="agent_answers", null=True, blank=True
    )
    answer_text = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_agent_questions",
    )

    class Meta:
        db_table = "work_item_questions"
        ordering = ["created_at", "id"]


class RunChild(ProjectBaseModel):
    contract = models.ForeignKey(WorkItemContract, on_delete=models.CASCADE, related_name="children")
    run = models.ForeignKey(AgentRun, on_delete=models.RESTRICT, related_name="children")
    issue = models.OneToOneField("db.Issue", on_delete=models.RESTRICT, related_name="agent_child")
    spec_revision = models.PositiveIntegerField()
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_agent_children")
    release = models.ForeignKey(ProjectRelease, on_delete=models.SET_NULL, null=True, blank=True)
    requirements = models.TextField()
    definition_of_done = models.JSONField(default=list)
    required = models.BooleanField(default=True)

    class Meta:
        db_table = "agent_run_children"
        ordering = ["created_at", "id"]


class RunEvidence(ProjectBaseModel):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="evidence")
    spec_revision = models.PositiveIntegerField()
    head_sha = models.CharField(max_length=64)
    kind = models.CharField(
        max_length=20, choices=[(value, value.title()) for value in ("test", "screenshot", "document", "note")]
    )
    outcome = models.CharField(
        max_length=20, choices=[(value, value.title()) for value in ("passed", "failed", "info")]
    )
    summary = models.TextField()
    criterion_ids = models.JSONField(default=list)
    url = models.URLField(blank=True)
    asset = models.ForeignKey("db.FileAsset", on_delete=models.RESTRICT, null=True, blank=True)

    class Meta:
        db_table = "agent_run_evidence"
        ordering = ["created_at", "id"]


class ReviewRequirement(ProjectBaseModel):
    contract = models.ForeignKey(WorkItemContract, on_delete=models.CASCADE, related_name="review_requirements")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="required_agent_reviews"
    )
    is_active = models.BooleanField(default=True)
    requested_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "coordination_review_requirements"
        constraints = [models.UniqueConstraint(fields=["contract", "reviewer"], name="coord_required_reviewer_unique")]


class ReviewDecision(ProjectBaseModel):
    contract = models.ForeignKey(WorkItemContract, on_delete=models.CASCADE, related_name="review_decisions")
    run = models.ForeignKey(AgentRun, on_delete=models.RESTRICT, related_name="review_decisions")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agent_review_decisions"
    )
    spec_revision = models.PositiveIntegerField()
    code_revision = models.PositiveIntegerField()
    head_sha = models.CharField(max_length=64)
    pull_request = models.PositiveIntegerField()
    decision = models.CharField(
        max_length=20, choices=[("approve", "Approve"), ("changes_requested", "Changes Requested")]
    )
    comment = models.TextField(blank=True)

    class Meta:
        db_table = "coordination_review_decisions"
        ordering = ["-created_at", "-id"]


class MergeAttempt(ProjectBaseModel):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="merge_attempts")
    completion_state = models.ForeignKey("db.State", on_delete=models.RESTRICT, related_name="coordination_merges")
    head_sha = models.CharField(max_length=64)
    code_revision = models.PositiveIntegerField()
    pull_request = models.PositiveIntegerField()
    authorization = models.JSONField(default=dict)
    status = models.CharField(
        max_length=20,
        default="prepared",
        choices=[
            ("prepared", "Prepared"),
            ("uncertain", "Uncertain"),
            ("failed", "Failed"),
            ("confirmed", "Confirmed"),
        ],
    )
    merge_sha = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "coordination_merge_attempts"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run"],
                condition=models.Q(status__in=["prepared", "uncertain"]),
                name="coord_one_pending_merge_per_run",
            )
        ]
