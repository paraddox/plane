# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from plane.coordination.github import GitHubClient, MergeRejected, ProviderUnavailable


@pytest.fixture
def github_http(settings, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings.COORDINATION_GITHUB_APP_ID = "test-app"
    settings.COORDINATION_GITHUB_PRIVATE_KEY = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    settings.COORDINATION_GITHUB_INSTALLATIONS = json.dumps({"project-id": {"example/product": 42}})
    settings.COORDINATION_GITHUB_HTTP_TIMEOUT = "10"
    state = {"requests": [], "merged": False, "merge_status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            parsed = urlparse(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or "null")
            state["requests"].append((self.command, parsed.path, body))
            assert self.headers["X-GitHub-Api-Version"] == GitHubClient.api_version
            status = 200
            if parsed.path == "/app/installations/42/access_tokens":
                claims = jwt.decode(
                    self.headers["Authorization"].removeprefix("Bearer "), private.public_key(), algorithms=["RS256"]
                )
                assert claims["iss"] == "test-app" and claims["exp"] - claims["iat"] == 600
                assert body["repositories"] == ["product"]
                assert body["permissions"] == {"contents": "write", "pull_requests": "read"}
                result, status = {"token": "synthetic-installation-token"}, 201
            else:
                assert self.headers["Authorization"] == "Bearer synthetic-installation-token"
                if parsed.path.endswith("/merges"):
                    status = state["merge_status"]
                    state["merged"] = status in (200, 201, 204)
                    state["merged_branch"] = body["base"]
                    state["merged_head"] = body["head"]
                    if state.get("retarget_on_merge"):
                        state["base_branch"] = "elsewhere"
                    result = {"sha": "f" * 40}
                elif "/branches/" in parsed.path:
                    result = {"name": "main", "commit": {"sha": "f" * 40 if state["merged"] else "c" * 40}}
                elif "/compare/" in parsed.path:
                    sha = parsed.path.rsplit("/", 1)[-1].split("...")[0]
                    merged = state["merged"] and sha == state.get("merged_head", sha)
                    result = {
                        "status": "ahead" if merged else "diverged",
                        "base_commit": {"sha": sha},
                        "merge_base_commit": {"sha": sha if merged else "d" * 40},
                    }
                else:
                    result = {
                        "head": {"sha": state.get("head_sha", "a" * 40)},
                        "base": {"ref": state.get("base_branch", "main"), "repo": {"full_name": "example/product"}},
                        "merged": state["merged"],
                        "merge_commit_sha": "f" * 40,
                        "state": "open",
                        "draft": False,
                        "mergeable": True,
                    }
            payload = b"" if status == 204 else json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(GitHubClient, "api_url", f"http://127.0.0.1:{server.server_port}")
    try:
        yield state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_github_contract_uses_repository_limited_app_credential_and_expected_sha(github_http):
    client = GitHubClient("project-id", "example/product")
    assert client.pull_request(7)["merged"] is False
    client.merge("main", "a" * 40)
    assert client.pull_request(7)["merged"] is True
    merge = next(row for row in github_http["requests"] if row[1].endswith("/merges"))
    assert merge == ("POST", "/repos/example/product/merges", {"base": "main", "head": "a" * 40})
    assert client.target_status("main", "a" * 40)["merged"] is True


@pytest.mark.parametrize("status,error", [(409, MergeRejected), (503, ProviderUnavailable), (429, ProviderUnavailable)])
def test_provider_distinguishes_definite_rejection_from_unknown_outcome(github_http, status, error):
    github_http["merge_status"] = status
    client = GitHubClient("project-id", "example/product")
    with pytest.raises(error):
        client.merge("main", "a" * 40)


def test_project_policy_cannot_select_another_projects_installation(github_http):
    with pytest.raises(ProviderUnavailable):
        GitHubClient("other-project", "example/product")
    assert not github_http["requests"]
