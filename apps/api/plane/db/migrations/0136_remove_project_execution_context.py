from django.db import migrations


def remove_snapshot_context(apps, schema_editor):
    run_model = apps.get_model("db", "AgentRun")
    runs = run_model.objects.using(schema_editor.connection.alias).filter(spec_snapshot__has_key="context_references")
    for run in runs.iterator():
        run.spec_snapshot.pop("context_references", None)
        run.save(using=schema_editor.connection.alias, update_fields=["spec_snapshot"])


class Migration(migrations.Migration):
    dependencies = [("db", "0135_work_item_credential_lifetime")]

    operations = [
        migrations.RunPython(remove_snapshot_context, migrations.RunPython.noop),
        migrations.RemoveField(model_name="projectexecutionpolicy", name="required_checks"),
        migrations.RemoveField(model_name="projectexecutionpolicy", name="context_references"),
    ]
