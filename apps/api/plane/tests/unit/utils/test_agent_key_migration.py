# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plane.db.models.api import hash_api_token


@pytest.mark.parametrize("rate", ["60/min", "1200/min", "not-a-rate", "1/", ""])
@pytest.mark.parametrize("service", [True, False])
def test_upgrade_hashes_keys_and_discards_untrusted_throughput_policy(rate, service):
    migration = importlib.import_module("plane.db.migrations.0122_agent_api_keys_and_idempotency")
    tokens = Mock()
    tokens.all.return_value.iterator.return_value = [
        SimpleNamespace(pk=1, token="historical-secret", allowed_rate_limit=rate, is_service=service)
    ]
    apps = Mock()
    apps.get_model.return_value.objects.using.return_value = tokens
    migration.hash_existing_keys(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
    updates = {"token": hash_api_token("historical-secret")}
    if rate:
        updates["allowed_rate_limit"] = ""
    if service:
        updates["is_service"] = False
    tokens.filter.assert_called_once_with(pk=1)
    tokens.filter.return_value.update.assert_called_once_with(**updates)


def test_upgrade_does_not_double_hash_a_digest():
    migration = importlib.import_module("plane.db.migrations.0122_agent_api_keys_and_idempotency")
    tokens = Mock()
    tokens.all.return_value.iterator.return_value = [
        SimpleNamespace(pk=1, token=hash_api_token("secret"), allowed_rate_limit="", is_service=False)
    ]
    apps = Mock()
    apps.get_model.return_value.objects.using.return_value = tokens
    migration.hash_existing_keys(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
    tokens.filter.assert_not_called()
