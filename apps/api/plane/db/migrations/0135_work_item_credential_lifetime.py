from django.db import migrations, models


def move_policy_checks_and_remove_key_expiry(apps, schema_editor):
    policy_model = apps.get_model("db", "ProjectExecutionPolicy")
    for policy in policy_model.objects.using(schema_editor.connection.alias).all().iterator():
        checks = list(dict.fromkeys(name for repo in policy.repositories for name in repo.get("required_checks", [])))
        policy.required_checks = checks
        policy.save(using=schema_editor.connection.alias, update_fields=["required_checks"])
    token_model = apps.get_model("db", "APIToken")
    token_model.objects.using(schema_editor.connection.alias).filter(purpose="execution", is_active=True).update(
        expired_at=None
    )


class Migration(migrations.Migration):
    dependencies = [("db", "0134_agent_notifications")]

    operations = [
        migrations.AddField(
            model_name="projectexecutionpolicy", name="required_checks", field=models.JSONField(default=list)
        ),
        migrations.RunPython(move_policy_checks_and_remove_key_expiry, migrations.RunPython.noop),
        migrations.RemoveField(model_name="projectexecutionpolicy", name="repositories"),
        migrations.RemoveField(model_name="projectexecutionpolicy", name="credential_lifetime"),
        migrations.RemoveField(model_name="projectexecutionpolicy", name="execution_lease"),
        migrations.RemoveField(model_name="agentrun", name="lease_expires_at"),
    ]
