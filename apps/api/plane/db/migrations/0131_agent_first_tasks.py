from django.db import migrations, models


def initialize_tasks(apps, schema_editor):
    Issue = apps.get_model("db", "Issue")
    Contract = apps.get_model("db", "WorkItemContract")
    Event = apps.get_model("db", "CoordinationEvent")
    Sequence = apps.get_model("db", "CoordinationSequence")
    database = schema_editor.connection.alias
    sequences = {}
    issues = Issue._base_manager.using(database).filter(
        deleted_at__isnull=True, created_by__is_bot=False,
        coordination_contract__isnull=True, agent_child__isnull=True,
    ).exclude(state__group="completed")
    for issue in issues.iterator():
        contract = Contract.objects.using(database).create(
            issue_id=issue.pk, project_id=issue.project_id, workspace_id=issue.workspace_id,
            owner_id=issue.created_by_id, created_by_id=issue.created_by_id,
            status="draft" if issue.is_draft else "ready", requirements="", definition_of_done=[],
        )
        if issue.is_draft:
            continue
        if issue.project_id not in sequences:
            sequence, _ = Sequence.objects.using(database).get_or_create(project_id=issue.project_id)
            sequences[issue.project_id] = sequence
        sequence = sequences[issue.project_id]
        sequence.last_value += 1
        Event.objects.using(database).create(
            project_id=issue.project_id, sequence=sequence.last_value, kind="task.ready",
            issue_id=issue.pk, actor_id=issue.created_by_id, spec_revision=contract.spec_revision,
        )
    for sequence in sequences.values():
        sequence.save(using=database, update_fields=["last_value"])
    Contract.objects.using(database).filter(status="draft", issue__is_draft=False).update(status="ready")


class Migration(migrations.Migration):
    dependencies = [("db", "0130_coordination_child_state_guards")]
    operations = [
        migrations.AlterField(
            model_name="workitemcontract", name="status",
            field=models.CharField(max_length=24, default="ready", choices=[
                ("draft", "Draft"), ("ready", "Ready for Agent"), ("in_progress", "In Progress"),
                ("awaiting_input", "Awaiting Input"), ("review", "Ready for Review"),
                ("merging", "Merging"), ("merged", "Done"), ("cancelled", "Cancelled"),
            ]),
        ),
        migrations.RunPython(initialize_tasks, migrations.RunPython.noop),
    ]
