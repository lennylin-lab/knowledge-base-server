#!/usr/bin/env bash
# Provision the Compose Keycloak dev user into the default tenant.
# Run after: docker compose up -d && uv run alembic upgrade head
#
# Idempotent: creates the Keycloak user if missing, then upserts PG membership.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

KEYCLOAK_URL="${KEYCLOAK_URL:-http://127.0.0.1:8180}"
REALM="${KEYCLOAK_REALM:-kb}"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:-kb-web}"
USERNAME="${KEYCLOAK_DEV_USER:-dev}"
PASSWORD="${KEYCLOAK_DEV_PASSWORD:-dev}"
KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-admin}"
TENANT_SLUG="${KB_TENANT_DEFAULT_SLUG:-default}"
ROLE="${KB_DEV_MEMBERSHIP_ROLE:-editor}"

export KEYCLOAK_URL REALM KEYCLOAK_ADMIN KEYCLOAK_ADMIN_PASSWORD USERNAME PASSWORD

python3 - <<'PY'
import json
import os
import urllib.error
import urllib.parse
import urllib.request

base = os.environ["KEYCLOAK_URL"].rstrip("/")
realm = os.environ["REALM"]
admin_user = os.environ["KEYCLOAK_ADMIN"]
admin_pass = os.environ["KEYCLOAK_ADMIN_PASSWORD"]
dev_user = os.environ["USERNAME"]
dev_pass = os.environ["PASSWORD"]


def post_form(url: str, data: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def api(
    method: str,
    url: str,
    token: str,
    body: dict | None = None,
) -> dict | list | None:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Keycloak admin API {method} {url} failed: {exc.code} {detail}") from exc


admin_token = post_form(
    f"{base}/realms/master/protocol/openid-connect/token",
    {
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": admin_user,
        "password": admin_pass,
    },
)["access_token"]

# Access tokens must carry `sub` — the API verifier requires it. Realm import
# may omit mappers on an existing keycloakdata volume; ensure via Admin API.
scopes = api("GET", f"{base}/admin/realms/{realm}/client-scopes", admin_token)
scope_id = next((s["id"] for s in scopes or [] if s.get("name") == "kb-api-audience"), None)
if scope_id:
    mappers = api(
        "GET",
        f"{base}/admin/realms/{realm}/client-scopes/{scope_id}/protocol-mappers/models",
        admin_token,
    )
    has_sub = any(m.get("name") == "sub-mapper" for m in mappers or [])
    if not has_sub:
        api(
            "POST",
            f"{base}/admin/realms/{realm}/client-scopes/{scope_id}/protocol-mappers/models",
            admin_token,
            {
                "name": "sub-mapper",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-property-mapper",
                "config": {
                    "user.attribute": "id",
                    "claim.name": "sub",
                    "jsonType.label": "String",
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                    "userinfo.token.claim": "true",
                },
            },
        )
        print("Added sub-mapper to kb-api-audience client scope.")

query = urllib.parse.urlencode({"username": dev_user, "exact": "true"})
users = api("GET", f"{base}/admin/realms/{realm}/users?{query}", admin_token)
if not users:
    created = api(
        "POST",
        f"{base}/admin/realms/{realm}/users",
        admin_token,
        {
            "username": dev_user,
            "enabled": True,
            "emailVerified": True,
            "firstName": "Dev",
            "lastName": "User",
            "email": f"{dev_user}@localhost.local",
        },
    )
    if created is not None:
        raise RuntimeError("Unexpected body from Keycloak user create")
    users = api("GET", f"{base}/admin/realms/{realm}/users?{query}", admin_token)
    if not users:
        raise RuntimeError(f"Failed to create Keycloak user {dev_user!r}")

user_id = users[0]["id"]
api(
    "PUT",
    f"{base}/admin/realms/{realm}/users/{user_id}/reset-password",
    admin_token,
    {"type": "password", "value": dev_pass, "temporary": False},
)
print(f"Ensured Keycloak user {dev_user!r} exists in realm {realm!r}.")
PY

token_response="$(
  curl -sf -X POST "${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=password" \
    -d "client_id=${CLIENT_ID}" \
    -d "username=${USERNAME}" \
    -d "password=${PASSWORD}" \
    -d "scope=openid"
)"

access_token="$(printf '%s' "$token_response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
subject="$(ACCESS_TOKEN="$access_token" KEYCLOAK_URL="$KEYCLOAK_URL" REALM="$REALM" python3 - <<'PY'
import base64, json, os, urllib.request

token = os.environ["ACCESS_TOKEN"]
payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
sub = claims.get("sub")
if sub:
    print(sub)
    raise SystemExit(0)

req = urllib.request.Request(
    f'{os.environ["KEYCLOAK_URL"]}/realms/{os.environ["REALM"]}/protocol/openid-connect/userinfo',
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    print(json.load(resp)["sub"])
PY
)"

echo "Keycloak subject for ${USERNAME}: ${subject}"

docker compose exec -T postgres psql -U kb -d kb -v ON_ERROR_STOP=1 <<SQL
INSERT INTO users (id, subject, email, display_name)
VALUES (gen_random_uuid(), '${subject}', '${USERNAME}@localhost.local', 'Dev User')
ON CONFLICT (subject) DO NOTHING;

INSERT INTO tenant_memberships (id, tenant_id, user_id, role)
SELECT gen_random_uuid(), t.id, u.id, '${ROLE}'
FROM tenants t
JOIN users u ON u.subject = '${subject}'
WHERE t.slug = '${TENANT_SLUG}'
ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role;
SQL

echo "Provisioned ${USERNAME} as ${ROLE} on tenant '${TENANT_SLUG}'."
