# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4
from unittest.mock import patch

import pytest

from plane.authentication.adapter.error import AuthenticationException
from plane.authentication.provider.credentials.magic_code import MagicCodeProvider
from plane.authentication.rate_limit import MAGIC_CODE_FAILED_ATTEMPT_LIMIT
from plane.settings.redis import redis_instance


@pytest.fixture
def challenge():
    client = redis_instance()
    email = f"magic-code-test-{uuid4().hex}@example.test"
    key = f"magic_{email}"
    token = "123456"
    client.set(key, json.dumps({"email": email, "token": token, "current_attempt": 0, "failed_attempt": 0}), ex=600)

    with (
        patch("plane.authentication.provider.credentials.magic_code.redis_instance", return_value=client),
        patch(
            "plane.authentication.provider.credentials.magic_code.get_configuration_value", return_value=("smtp", "1")
        ),
        patch("plane.authentication.provider.credentials.magic_code.User") as user,
    ):
        user.objects.filter.return_value.exists.return_value = True
        yield client, key, token

    client.delete(key)
    client.close()


def submit(key, code):
    provider = MagicCodeProvider(request=None, key=key, code=code)
    try:
        provider.set_user_data()
        return "accepted"
    except AuthenticationException as exc:
        return exc.error_message


@pytest.mark.unit
def test_concurrent_failures_exhaust_challenge(challenge, monkeypatch):
    client, key, _ = challenge
    barrier = Barrier(MAGIC_CODE_FAILED_ATTEMPT_LIMIT)
    original_get = client.get

    def simultaneous_read(key):
        snapshot = original_get(key)
        barrier.wait()
        return snapshot

    monkeypatch.setattr(client, "get", simultaneous_read)
    with ThreadPoolExecutor(max_workers=MAGIC_CODE_FAILED_ATTEMPT_LIMIT) as pool:
        results = list(pool.map(lambda _: submit(key, "000000"), range(MAGIC_CODE_FAILED_ATTEMPT_LIMIT)))

    assert results.count("EMAIL_CODE_ATTEMPT_EXHAUSTED_SIGN_IN") == 1
    assert not client.exists(key)


@pytest.mark.unit
def test_failure_cannot_restore_consumed_challenge(challenge, monkeypatch):
    client, key, token = challenge
    barrier = Barrier(2)
    consumed = Event()
    original_get, original_set = client.get, client.set

    def simultaneous_read(key):
        snapshot = original_get(key)
        barrier.wait()
        return snapshot

    def stale_write(*args, **kwargs):
        consumed.wait()
        return original_set(*args, **kwargs)

    def accept():
        try:
            return submit(key, token)
        finally:
            consumed.set()

    with monkeypatch.context() as scoped:
        scoped.setattr(client, "get", simultaneous_read)
        scoped.setattr(client, "set", stale_write)
        with ThreadPoolExecutor(max_workers=2) as pool:
            wrong = pool.submit(submit, key, "000000")
            correct = pool.submit(accept)
            assert correct.result() == "accepted"
            assert wrong.result() in {"INVALID_MAGIC_CODE_SIGN_IN", "EXPIRED_MAGIC_CODE_SIGN_IN"}

    assert not client.exists(key)
    assert submit(key, token) == "EXPIRED_MAGIC_CODE_SIGN_IN"


@pytest.mark.unit
def test_concurrent_correct_codes_are_consumed_once(challenge, monkeypatch):
    client, key, token = challenge
    barrier = Barrier(2)
    original_get = client.get

    def simultaneous_read(key):
        snapshot = original_get(key)
        barrier.wait()
        return snapshot

    monkeypatch.setattr(client, "get", simultaneous_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(key, token), range(2)))

    assert results.count("accepted") == 1
    assert results.count("EXPIRED_MAGIC_CODE_SIGN_IN") == 1
    assert not client.exists(key)


@pytest.mark.unit
def test_failed_attempt_preserves_expiry(challenge):
    client, key, _ = challenge
    expiration = client.pexpiretime(key)
    assert submit(key, "000000") == "INVALID_MAGIC_CODE_SIGN_IN"
    assert client.pexpiretime(key) == expiration


@pytest.mark.unit
def test_missing_challenge_is_not_recreated(challenge):
    client, key, token = challenge
    client.delete(key)
    assert submit(key, token) == "EXPIRED_MAGIC_CODE_SIGN_IN"
    assert not client.exists(key)


@pytest.mark.unit
def test_legacy_challenge_preserves_user_data(challenge):
    client, key, token = challenge
    data = json.loads(client.get(key))
    del data["failed_attempt"]
    client.set(key, json.dumps(data), keepttl=True)
    assert submit(key, "000000") == "INVALID_MAGIC_CODE_SIGN_IN"

    provider = MagicCodeProvider(request=None, key=key, code=token)
    provider.set_user_data()
    assert provider.user_data["email"] == data["email"]
    assert provider.user_data["user"]["is_password_autoset"] is True
    assert not client.exists(key)


@pytest.mark.unit
def test_sign_up_errors_are_preserved(challenge):
    client, key, token = challenge
    with patch("plane.authentication.provider.credentials.magic_code.User") as user:
        user.objects.filter.return_value.exists.return_value = False
        assert submit(key, "000000") == "INVALID_MAGIC_CODE_SIGN_UP"
        data = json.loads(client.get(key))
        data["failed_attempt"] = MAGIC_CODE_FAILED_ATTEMPT_LIMIT - 1
        client.set(key, json.dumps(data), keepttl=True)
        assert submit(key, "000000") == "EMAIL_CODE_ATTEMPT_EXHAUSTED_SIGN_UP"
        assert submit(key, token) == "EXPIRED_MAGIC_CODE_SIGN_UP"
