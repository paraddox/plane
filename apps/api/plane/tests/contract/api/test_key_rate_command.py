# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from plane.api.rate_limit import get_api_key_rate


@pytest.mark.django_db
def test_operator_can_set_key_rate_without_exposing_secret(api_token):
    output = StringIO()
    call_command("set_api_key_rate", str(api_token.pk), "--rate", "300/minute", stdout=output)
    api_token.refresh_from_db()
    assert get_api_key_rate(api_token) == (300, 60)
    assert api_token.allowed_rate_limit == "300/minute"
    assert "test-api-token-12345" not in output.getvalue()
    assert api_token.token not in output.getvalue()


@pytest.mark.django_db
def test_invalid_policy_does_not_modify_key(api_token):
    previous = api_token.allowed_rate_limit
    with pytest.raises(CommandError):
        call_command("set_api_key_rate", str(api_token.pk), "--rate", "not-a-rate")
    api_token.refresh_from_db()
    assert api_token.allowed_rate_limit == previous


@pytest.mark.django_db
def test_operator_can_restore_inherited_policy(api_token, monkeypatch):
    api_token.allowed_rate_limit = "300/minute"
    api_token.save(update_fields=["allowed_rate_limit"])
    monkeypatch.setenv("API_KEY_RATE_LIMIT", "60/minute")
    call_command("set_api_key_rate", str(api_token.pk), "--inherit", stdout=StringIO())
    api_token.refresh_from_db()
    assert api_token.allowed_rate_limit == ""
    assert get_api_key_rate(api_token) == (60, 60)
