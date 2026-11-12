# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import migrations, models
import django.db.models.deletion


SNAPSHOT_ANSWERS = """
UPDATE work_item_questions q SET answer_text = COALESCE(c.comment_stripped, '')
FROM issue_comments c WHERE c.id = q.answer_comment_id;
"""

GUARD_DELETE = """
CREATE FUNCTION coordination_guard_contract_deletion() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'merging' THEN
        RAISE EXCEPTION 'Reconcile the in-flight merge before deleting task history'
            USING ERRCODE = '23514';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER coordination_contract_deletion BEFORE DELETE ON work_item_contracts
FOR EACH ROW EXECUTE FUNCTION coordination_guard_contract_deletion();
"""

REVERSE_DELETE = """
DROP TRIGGER coordination_contract_deletion ON work_item_contracts;
DROP FUNCTION coordination_guard_contract_deletion();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0128_coordination_completion_guards"),
    ]

    operations = [
        migrations.AddField(
            model_name="workitemquestion",
            name="answer_text",
            field=models.TextField(blank=True),
        ),
        migrations.RunSQL(SNAPSHOT_ANSWERS, migrations.RunSQL.noop),
        migrations.RunSQL(GUARD_DELETE, REVERSE_DELETE),
        migrations.AlterField(
            model_name="fileasset",
            name="agent_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="artifacts",
                to="db.agentrun",
            ),
        ),
        migrations.AlterField(
            model_name="mergeattempt",
            name="completion_state",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT, related_name="coordination_merges", to="db.state"
            ),
        ),
        migrations.AlterField(
            model_name="mergeattempt",
            name="run",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="merge_attempts", to="db.agentrun"
            ),
        ),
        migrations.AlterField(
            model_name="reviewdecision",
            name="run",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT, related_name="review_decisions", to="db.agentrun"
            ),
        ),
        migrations.AlterField(
            model_name="runchild",
            name="issue",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.RESTRICT, related_name="agent_child", to="db.issue"
            ),
        ),
        migrations.AlterField(
            model_name="runchild",
            name="run",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT, related_name="children", to="db.agentrun"
            ),
        ),
        migrations.AlterField(
            model_name="runevidence",
            name="asset",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, to="db.fileasset"
            ),
        ),
        migrations.AlterField(
            model_name="runevidence",
            name="run",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="evidence", to="db.agentrun"
            ),
        ),
        migrations.AlterField(
            model_name="workitemquestion",
            name="answer_comment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="agent_answers",
                to="db.issuecomment",
            ),
        ),
        migrations.AlterField(
            model_name="workitemquestion",
            name="question_comment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT, related_name="agent_questions", to="db.issuecomment"
            ),
        ),
        migrations.AlterField(
            model_name="workitemquestion",
            name="run",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT, related_name="questions", to="db.agentrun"
            ),
        ),
    ]
