# API Access for Agents

Agents use the same HTTPS entrypoint as the web application. No additional public port is required. The supported machine API is `/api/v1/`, authenticated with `X-Api-Key` rather than browser cookies.

## Credentials and Permissions

Create a token in Account Settings > API Tokens. Use a separate label and token for each agent. The secret is returned only when the token is created; store it in the agent's secret configuration, not in project descriptions or logs.

The token-creation form supports an optional workspace, optional project, and explicit read-only or read/write permissions. These restrictions narrow the owning user's existing permissions; they never grant membership or administrative privileges. A key without resource restrictions can act within the owner's memberships, subject to its read/write scopes. Empty or invalid scopes do not grant unrestricted access.

Credential management remains session-authenticated and CSRF-protected. An agent cannot use its API key to mint additional credentials or access instance administration. Delete a token from the owner's settings to revoke it. Disabled users must not authenticate with an API key.

```bash
curl --fail-with-body "$PLANE_URL/api/v1/users/me/" \
  -H "X-Api-Key: $PLANE_API_KEY"
```

Keys are stored as one-way digests. API audit records identify the credential without storing its secret. Credentials used before this upgrade may have appeared in legacy activity logs: rotate those keys and follow your existing backup/log-retention policy when removing legacy copies. Keep a database backup before applying the digest migration; an older application version cannot recover plaintext keys from the migrated database.

## Business Workflows

Use the deployed OpenAPI schema for exact paths, methods, request bodies, and response shapes. The machine API covers workspace discovery and management, projects and work items, pages, saved views, notifications, webhook configuration, and estimates, alongside existing comments, attachments, labels, states, cycles, modules, members, invitations, and intake.

Use the canonical `work-items` and `members` routes. Deprecated `issues` and `project-members` aliases are removed so the schema has one operation for each action.

Scoped discovery returns only resources allowed by the key and the owner's memberships. A project-scoped key cannot use a workspace-wide operation to read or mutate a different project. Personal notification access is also constrained by the authenticated owner. Browser session endpoints retain their existing authentication and CSRF requirements.

## Throughput

`API_KEY_RATE_LIMIT` supplies the existing normal-token default. Service tokens retain the existing service policy. Administrators can configure a valid `allowed_rate_limit` override on an individual token; ordinary token metadata updates cannot change its service tier or throughput policy.

The upgrade resets historical per-key rate overrides and service flags because owners could previously edit those fields. Operators must explicitly reapply approved higher rates after migration.

These defaults are inherited project policy, not a statement of VPS capacity. Measure the expected agent workload before raising them. Monitor response latency, database contention, CPU/memory, and background-worker backlog under both reads and mutations. Bulk operations and paginated reads reduce unnecessary requests.

An operator with access to the backend can set the measured policy without exposing the secret or editing database rows directly:

```bash
python manage.py set_api_key_rate "$TOKEN_ID" --rate "$AGENT_API_RATE"
python manage.py set_api_key_rate "$TOKEN_ID" --inherit
```

`TOKEN_ID` is the UUID from token metadata. `AGENT_API_RATE` is the operator-selected positive count and period in DRF rate syntax. These commands are server maintenance operations, not API permissions available to an agent.

Clients should respect HTTP 429 and `Retry-After`, and use the returned rate-limit information when scheduling work. Authentication, role checks, and resource restrictions remain in force at higher rates. A successful API response can enqueue asynchronous activity/webhook work; API acceptance does not mean every downstream worker task has completed.

## Retries and Concurrent Writes

Supply `Idempotency-Key` for mutations that may be retried after a timeout. Reuse the same key only for the same logical operation and request payload. A repeated authorized request returns the completed result without repeating the mutation; a different payload with the same identity is a conflict. Use a new key for a new operation.

Page, saved-view, notification, and webhook deletion retries use retained soft-deleted metadata to recheck current access before replaying the completed result. If that metadata has been purged or the caller no longer has permission, the retry fails closed instead of returning the cached response. Workspace deletion does not support `Idempotency-Key`: it changes the workspace slug, so the original URL no longer reliably identifies the deleted workspace. Supplying the header on workspace deletion returns 400 before any deletion occurs.

Successful response snapshots are stored in PostgreSQL for replay. Include this data in the deployment's retention and backup policy.

External IDs are useful integration identifiers, but a preflight search alone is not a concurrency guarantee. Use idempotency for retried creates. When work-item responses provide an ETag, pass it with `If-Match` on updates to avoid overwriting a newer edit; refresh the item and reconcile changes after a precondition failure.

Bulk actions must use the documented request shape and preserve the same role checks, validation, activity, and webhook behavior as individual actions. Do not treat higher throughput as permission to bypass those checks.

Atomic work-item updates are available at `POST /api/v1/workspaces/{slug}/projects/{project_id}/work-items/bulk/`:

```json
{
  "updates": [{ "id": "work-item-uuid", "data": { "name": "Updated title" } }]
}
```

Each entry can include `if_match` from its current ETag. The request either commits all updates or rolls them all back. A work item can appear only once per batch. `Idempotency-Key` also protects bulk retries. Request-body limits configured for the deployment still apply.

## API Documentation and Deployment

Set `ENABLE_DRF_SPECTACULAR=1` in the production environment and recreate the backend service to enable `/api/schema/`, `/api/schema/swagger-ui/`, and `/api/schema/redoc/`. The production Compose file forwards this flag. Schema server URLs are relative to the deployment, so agents do not accidentally target a hosted Plane instance.

Validate the schema after endpoint changes and exercise workflows using an actual API key, including denied cross-workspace/project access, expiry/revocation, retries, and concurrent writes. Tests must not replace authentication with `force_authenticate` when the property being checked is key validation or scoping.

For Cloudflare, verify requests from the actual agent runtime return API JSON instead of browser challenge pages. Keep appropriate WAF and DDoS protection. Avoid blanket exceptions based only on the presence of an `X-Api-Key` header: the application must validate that credential. Deployment-specific Cloudflare rules and VPS capacity require verification in the owner's environment.
