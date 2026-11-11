# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""
Schema processing hooks for drf-spectacular OpenAPI generation.

This module provides preprocessing and postprocessing functions that modify
the generated OpenAPI schema to apply custom filtering, tagging, and other
transformations.
"""


def preprocess_filter_api_v1_paths(endpoints):
    """
    Include all supported methods on the public /api/v1/ contract.
    """
    filtered = []
    for path, path_regex, method, callback in endpoints:
        if path.startswith("/api/v1/"):
            filtered.append((path, path_regex, method, callback))
    return filtered


def _add_header_parameter(operation, name, description):
    parameters = operation.setdefault("parameters", [])
    if not any(
        parameter.get("in") == "header" and parameter.get("name", "").lower() == name.lower()
        for parameter in parameters
    ):
        parameters.append(
            {
                "in": "header",
                "name": name,
                "required": False,
                "schema": {"type": "string"},
                "description": description,
            }
        )


def _add_error_response(operation, code, description):
    operation["responses"].setdefault(
        code,
        {"description": description, "content": {"application/json": {"schema": {"type": "object"}}}},
    )


def postprocess_api_write_headers(result, generator, request, public):
    """Describe retry and concurrency headers only where the handlers support them."""
    from plane.api.idempotency import IdempotencyMixin
    from plane.api.views.issue import IssueDetailAPIEndpoint, IssueListCreateAPIEndpoint

    for path, _, method, callback in generator.endpoints:
        method = method.lower()
        operation = result.get("paths", {}).get(path, {}).get(method)
        view_class = getattr(callback, "cls", None)
        if operation is None or not isinstance(view_class, type):
            continue

        action = getattr(callback, "actions", {}).get(method)
        unsupported = action in getattr(view_class, "idempotency_unsupported_actions", set())
        if unsupported:
            operation["description"] = (
                operation.get("description", "")
                + "\n\nThis operation does not support Idempotency-Key. Supplying it returns 400 before mutation."
            ).strip()
            _add_error_response(operation, "400", "This operation does not support Idempotency-Key.")
        elif issubclass(view_class, IdempotencyMixin) and method in {"post", "put", "patch", "delete"}:
            _add_header_parameter(
                operation,
                "Idempotency-Key",
                "Optional nonblank key for safely retrying a JSON mutation. Send application/json when supplying "
                "a body; non-JSON content types are rejected with 415 when this header is supplied. Reusing a key "
                "with the same method, URL, JSON body and If-Match replays the successful response after current "
                "authorization checks. Reusing it for a different request returns 409.",
            )
            _add_error_response(operation, "409", "The idempotency key was already used for a different request.")
            _add_error_response(operation, "415", "Idempotency-Key requires a JSON request content type.")

        is_detail = issubclass(view_class, IssueDetailAPIEndpoint)
        if is_detail and method in {"patch", "delete", "put"}:
            _add_header_parameter(
                operation,
                "If-Match",
                "Optional work-item ETag from a previous read or write, including its quotes. The mutation returns "
                "412 if the version no longer matches. '*' accepts any existing version. An upsert with this "
                "header returns 412 if the work item does not exist.",
            )
            _add_error_response(operation, "412", "The work item no longer matches If-Match or does not exist.")

        returns_etag = (is_detail and method in {"get", "patch", "put"}) or (
            issubclass(view_class, IssueListCreateAPIEndpoint) and method == "post"
        )
        if returns_etag:
            for code, response in operation["responses"].items():
                if str(code).startswith("2"):
                    response.setdefault("headers", {})["ETag"] = {
                        "schema": {"type": "string"},
                        "description": "Quoted work-item version. Send it as If-Match on a conditional mutation.",
                    }
    return result


def generate_operation_summary(method, path, tag):
    """
    Generate a human-readable summary for an operation.
    """
    # Extract the main resource from the path
    path_parts = [part for part in path.split("/") if part and not part.startswith("{")]

    if len(path_parts) > 0:
        resource = path_parts[-1].replace("-", " ").title()
    else:
        resource = tag

    # Generate summary based on method
    method_summaries = {
        "GET": f"Retrieve {resource}",
        "POST": f"Create {resource}",
        "PUT": f"Update {resource}",
        "PATCH": f"Update {resource}",
        "DELETE": f"Delete {resource}",
    }

    # Handle specific cases
    if "archive" in path.lower():
        if method == "POST":
            return f"Archive {tag.rstrip('s')}"
        elif method == "DELETE":
            return f"Unarchive {tag.rstrip('s')}"

    if "transfer" in path.lower():
        return f"Transfer {tag.rstrip('s')}"

    return method_summaries.get(method, f"{method} {resource}")
