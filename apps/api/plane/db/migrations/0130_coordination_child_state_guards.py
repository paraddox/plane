# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import migrations


FORWARD = """
CREATE OR REPLACE FUNCTION coordination_guard_state_group() RETURNS trigger AS $$
BEGIN
    IF NEW."group" IS DISTINCT FROM OLD."group" OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
        IF (NEW."group" = 'completed' AND EXISTS (
            SELECT 1 FROM issues i JOIN work_item_contracts c ON c.issue_id = i.id
            WHERE i.state_id = NEW.id AND c.deleted_at IS NULL
            AND NOT (c.status = 'merged' AND c.merged_sha <> '')
        )) OR EXISTS (
            SELECT 1 FROM coordination_merge_attempts m WHERE m.completion_state_id = NEW.id
            AND m.status IN ('prepared', 'uncertain')
        ) OR EXISTS (
            SELECT 1 FROM issues i JOIN agent_run_children ch ON ch.issue_id = i.id
            JOIN work_item_contracts c ON c.id = ch.contract_id
            WHERE i.state_id = NEW.id AND ch.required AND ch.deleted_at IS NULL
            AND ch.spec_revision = c.spec_revision AND c.status = 'merging' AND c.deleted_at IS NULL
        ) THEN
            RAISE EXCEPTION 'State changes cannot bypass an outstanding coordinated merge'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

REVERSE = """
CREATE OR REPLACE FUNCTION coordination_guard_state_group() RETURNS trigger AS $$
BEGIN
    IF NEW."group" IS DISTINCT FROM OLD."group" OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
        IF (NEW."group" = 'completed' AND EXISTS (
            SELECT 1 FROM issues i JOIN work_item_contracts c ON c.issue_id = i.id
            WHERE i.state_id = NEW.id AND c.deleted_at IS NULL
            AND NOT (c.status = 'merged' AND c.merged_sha <> '')
        )) OR EXISTS (
            SELECT 1 FROM coordination_merge_attempts m WHERE m.completion_state_id = NEW.id
            AND m.status IN ('prepared', 'uncertain')
        ) THEN
            RAISE EXCEPTION 'State changes cannot bypass an outstanding coordinated merge'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [("db", "0129_coordination_answer_snapshots")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
