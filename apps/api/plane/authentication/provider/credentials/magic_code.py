# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import json
import os
import secrets


# Module imports
from plane.authentication.adapter.credential import CredentialAdapter
from plane.license.utils.instance_value import get_configuration_value
from plane.settings.redis import redis_instance
from plane.authentication.adapter.error import (
    AUTHENTICATION_ERROR_CODES,
    AuthenticationException,
)
from plane.authentication.rate_limit import MAGIC_CODE_FAILED_ATTEMPT_LIMIT
from plane.db.models import User


# Keep verification, failure counting, and consumption in one atomic Redis operation.
VERIFY_MAGIC_CODE_SCRIPT = """
local value = redis.call("GET", KEYS[1])
if not value then
    return {"expired"}
end

local data = cjson.decode(value)
if tostring(data.token) == ARGV[1] then
    redis.call("DEL", KEYS[1])
    return {"valid", data.email}
end

data.failed_attempt = (tonumber(data.failed_attempt) or 0) + 1
if data.failed_attempt >= tonumber(ARGV[2]) then
    redis.call("DEL", KEYS[1])
    return {"exhausted"}
end

redis.call("SET", KEYS[1], cjson.encode(data), "KEEPTTL")
return {"invalid"}
"""


class MagicCodeProvider(CredentialAdapter):
    provider = "magic-code"

    def __init__(self, request, key, code=None, callback=None):
        (EMAIL_HOST, ENABLE_MAGIC_LINK_LOGIN) = get_configuration_value(
            [
                {"key": "EMAIL_HOST", "default": os.environ.get("EMAIL_HOST")},
                {
                    "key": "ENABLE_MAGIC_LINK_LOGIN",
                    "default": os.environ.get("ENABLE_MAGIC_LINK_LOGIN", "1"),
                },
            ]
        )

        if not (EMAIL_HOST):
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["SMTP_NOT_CONFIGURED"],
                error_message="SMTP_NOT_CONFIGURED",
                payload={"email": str(key)},
            )

        if ENABLE_MAGIC_LINK_LOGIN == "0":
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["MAGIC_LINK_LOGIN_DISABLED"],
                error_message="MAGIC_LINK_LOGIN_DISABLED",
                payload={"email": str(key)},
            )

        super().__init__(request=request, provider=self.provider, callback=callback)
        self.key = key
        self.code = code

    def initiate(self):
        ## Generate a random token
        token = str(secrets.randbelow(900000) + 100000)

        ri = redis_instance()

        key = "magic_" + str(self.key)

        # Check if the key already exists in python
        if ri.exists(key):
            data = json.loads(ri.get(key))

            current_attempt = data["current_attempt"] + 1

            if current_attempt > 2:
                email = str(self.key).replace("magic_", "", 1)
                if User.objects.filter(email=email).exists():
                    raise AuthenticationException(
                        error_code=AUTHENTICATION_ERROR_CODES["EMAIL_CODE_ATTEMPT_EXHAUSTED_SIGN_IN"],
                        error_message="EMAIL_CODE_ATTEMPT_EXHAUSTED_SIGN_IN",
                        payload={"email": str(email)},
                    )
                else:
                    raise AuthenticationException(
                        error_code=AUTHENTICATION_ERROR_CODES["EMAIL_CODE_ATTEMPT_EXHAUSTED_SIGN_UP"],
                        error_message="EMAIL_CODE_ATTEMPT_EXHAUSTED_SIGN_UP",
                        payload={"email": self.key},
                    )

            value = {
                "current_attempt": current_attempt,
                "email": str(self.key),
                "failed_attempt": 0,
                "token": token,
            }
            expiry = 600
            ri.set(key, json.dumps(value), ex=expiry)
        else:
            value = {"current_attempt": 0, "email": self.key, "failed_attempt": 0, "token": token}
            expiry = 600

            ri.set(key, json.dumps(value), ex=expiry)
        return key, token

    def set_user_data(self):
        ri = redis_instance()
        result = ri.eval(VERIFY_MAGIC_CODE_SCRIPT, 1, self.key, str(self.code), MAGIC_CODE_FAILED_ATTEMPT_LIMIT)
        verification_status = result[0].decode("utf-8")

        if verification_status == "valid":
            super().set_user_data(
                {
                    "email": result[1].decode("utf-8"),
                    "user": {
                        "avatar": "",
                        "first_name": "",
                        "last_name": "",
                        "provider_id": "",
                        "is_password_autoset": True,
                    },
                }
            )
            return

        email = str(self.key).replace("magic_", "", 1)
        error_prefix = {
            "expired": "EXPIRED_MAGIC_CODE",
            "invalid": "INVALID_MAGIC_CODE",
            "exhausted": "EMAIL_CODE_ATTEMPT_EXHAUSTED",
        }[verification_status]
        auth_type = "SIGN_IN" if User.objects.filter(email=email).exists() else "SIGN_UP"
        error_message = f"{error_prefix}_{auth_type}"
        raise AuthenticationException(
            error_code=AUTHENTICATION_ERROR_CODES[error_message],
            error_message=error_message,
            payload={"email": email},
        )
