# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import migrations


FORWARD = """
CREATE FUNCTION coordination_guard_issue_completion() RETURNS trigger AS $$
DECLARE state_group text;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.state_id IS DISTINCT FROM OLD.state_id OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
        OR NEW.parent_id IS DISTINCT FROM OLD.parent_id OR NEW.archived_at IS DISTINCT FROM OLD.archived_at
    ) AND EXISTS (
        SELECT 1 FROM work_item_contracts c WHERE c.issue_id = NEW.id AND c.status = 'merging'
        UNION ALL
        SELECT 1 FROM agent_run_children ch JOIN work_item_contracts c ON c.id = ch.contract_id
        WHERE ch.issue_id = NEW.id AND c.status = 'merging' AND ch.spec_revision = c.spec_revision
    ) THEN
        RAISE EXCEPTION 'Reconcile the in-flight merge before changing task state' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM work_item_contracts c WHERE c.issue_id = NEW.id
        AND c.deleted_at IS NULL AND NOT (c.status = 'merged' AND c.merged_sha <> '')
    ) THEN
        SELECT s."group" INTO state_group FROM states s WHERE s.id = NEW.state_id FOR SHARE;
        IF state_group = 'completed' OR NEW.completed_at IS NOT NULL THEN
            RAISE EXCEPTION 'Coordinated work requires a confirmed repository merge before completion'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER coordination_issue_completion BEFORE INSERT OR UPDATE ON issues
FOR EACH ROW EXECUTE FUNCTION coordination_guard_issue_completion();

CREATE FUNCTION coordination_guard_contract_creation() RETURNS trigger AS $$
DECLARE state_group text;
BEGIN
    SELECT s."group" INTO state_group FROM states s JOIN issues i ON i.state_id = s.id
        WHERE i.id = NEW.issue_id FOR SHARE OF s;
    IF state_group = 'completed' THEN
        RAISE EXCEPTION 'Reopen the work item before adding an agent specification' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER coordination_contract_creation BEFORE INSERT ON work_item_contracts
FOR EACH ROW EXECUTE FUNCTION coordination_guard_contract_creation();

CREATE FUNCTION coordination_guard_state_group() RETURNS trigger AS $$
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
CREATE TRIGGER coordination_state_group BEFORE UPDATE ON states
FOR EACH ROW EXECUTE FUNCTION coordination_guard_state_group();
"""

REVERSE = """
DROP TRIGGER coordination_state_group ON states;
DROP FUNCTION coordination_guard_state_group();
DROP TRIGGER coordination_contract_creation ON work_item_contracts;
DROP FUNCTION coordination_guard_contract_creation();
DROP TRIGGER coordination_issue_completion ON issues;
DROP FUNCTION coordination_guard_issue_completion();
"""


class Migration(migrations.Migration):
    dependencies = [("db", "0127_coordination_reviews")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
