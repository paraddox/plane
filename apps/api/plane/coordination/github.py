# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
import math
import re
import time
from urllib.parse import quote

import jwt
import requests
from django.conf import settings
from rest_framework.exceptions import APIException

from .contracts import CoordinationConflict


class ProviderUnavailable(APIException):
    status_code = 503
    default_detail = "GitHub could not confirm the operation. Retry observation; do not assume it merged."


class MergeRejected(CoordinationConflict):
    default_detail = "GitHub rejected the merge. Check branch protections, permissions, CI, and the current head."


class GitHubClient:
    api_url = "https://api.github.com"
    api_version = "2026-03-10"

    def __init__(self, project_id, repository):
        self.repository = repository
        try:
            timeout = float(settings.COORDINATION_GITHUB_HTTP_TIMEOUT)
            installations = json.loads(settings.COORDINATION_GITHUB_INSTALLATIONS)
            installation = int(installations[str(project_id)][repository])
            if not math.isfinite(timeout) or timeout <= 0 or installation <= 0:
                raise ValueError
            if not settings.COORDINATION_GITHUB_APP_ID or not settings.COORDINATION_GITHUB_PRIVATE_KEY:
                raise ValueError
            self.timeout = timeout
            now = int(time.time())
            # GitHub requires RS256 and exp no more than ten minutes in the future.
            app_jwt = jwt.encode(
                {"iat": now, "exp": now + 600, "iss": settings.COORDINATION_GITHUB_APP_ID},
                settings.COORDINATION_GITHUB_PRIVATE_KEY,
                algorithm="RS256",
            )
        except (ValueError, TypeError, KeyError, jwt.PyJWTError):
            raise ProviderUnavailable("Configure this project's GitHub App installation and HTTP timeout.") from None
        result = self._request(
            "POST",
            f"/app/installations/{installation}/access_tokens",
            token=app_jwt,
            body={
                "repositories": [repository.split("/", 1)[1]],
                "permissions": {"contents": "write", "pull_requests": "read"},
            },
        )
        self.token = result.get("token")
        if not isinstance(self.token, str) or not self.token:
            raise ProviderUnavailable("GitHub did not issue an installation credential.")

    def _request(self, method, path, *, token=None, body=None, params=None, merging=False):
        try:
            response = requests.request(
                method,
                self.api_url + path,
                json=body,
                params=params,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token or self.token}",
                    "X-GitHub-Api-Version": self.api_version,
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise ProviderUnavailable() from None
        if not 200 <= response.status_code < 300:
            if merging and 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                raise MergeRejected()
            raise ProviderUnavailable()
        if merging and response.status_code == 204:
            return {}
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ValueError:
            raise ProviderUnavailable("GitHub returned an invalid response.") from None

    def pull_request(self, number):
        value = self._request("GET", f"/repos/{self.repository}/pulls/{number}")
        try:
            result = {
                "head_sha": value["head"]["sha"],
                "base_branch": value["base"]["ref"],
                "repository": value["base"]["repo"]["full_name"],
                "merged": value["merged"],
                "merge_sha": value.get("merge_commit_sha") or "",
                "state": value["state"],
                "draft": value["draft"],
                "mergeable": value["mergeable"],
            }
            if (
                not all(isinstance(result[name], str) for name in ("head_sha", "base_branch", "repository", "state"))
                or not isinstance(result["merged"], bool)
                or not isinstance(result["draft"], bool)
                or (result["mergeable"] is not None and not isinstance(result["mergeable"], bool))
            ):
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError):
            raise ProviderUnavailable("GitHub returned incomplete pull request metadata.") from None

    def target_status(self, base_branch, head_sha):
        value = self._request("GET", f"/repos/{self.repository}/branches/{quote(base_branch, safe='')}")
        try:
            target = value["commit"]["sha"]
            if (
                value["name"] != base_branch
                or not isinstance(target, str)
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", target)
            ):
                raise ValueError
            comparison = self._request(
                "GET", f"/repos/{self.repository}/compare/{head_sha}...{target}", params={"per_page": 1}
            )
            if comparison["base_commit"]["sha"] != head_sha or comparison["status"] not in (
                "ahead",
                "behind",
                "identical",
                "diverged",
            ):
                raise ValueError
            merged = comparison["status"] in ("ahead", "identical")
            if merged and comparison["merge_base_commit"]["sha"] != head_sha:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ProviderUnavailable("GitHub could not prove the approved commit is on the target branch.") from None
        return {
            "repository": self.repository,
            "base_branch": base_branch,
            "head_sha": head_sha,
            "merged": merged,
            "merge_sha": target if merged else "",
        }

    def merge(self, base_branch, sha):
        # The target is explicit, not taken from mutable pull-request metadata.
        result = self._request(
            "POST",
            f"/repos/{self.repository}/merges",
            body={"base": base_branch, "head": sha},
            merging=True,
        )
        if result and (
            not isinstance(result.get("sha"), str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", result["sha"])
        ):
            raise ProviderUnavailable("GitHub returned incomplete merge confirmation.")
        # Completion requires a separate target-branch ancestry check.
        return result
