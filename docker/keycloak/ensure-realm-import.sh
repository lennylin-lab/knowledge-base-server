#!/usr/bin/env bash
# Copy the tracked realm template into the gitignored import path Keycloak reads.
# Run once before the first `docker compose up` that starts Keycloak.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE="${DIR}/import/kb-realm.json.example"
TARGET="${DIR}/import/kb-realm.json"

if [[ ! -f "$EXAMPLE" ]]; then
  echo "Missing realm template: ${EXAMPLE}" >&2
  exit 1
fi

if [[ -f "$TARGET" ]]; then
  echo "Realm import already present: ${TARGET}"
  exit 0
fi

cp "$EXAMPLE" "$TARGET"
echo "Created ${TARGET} from kb-realm.json.example (gitignored)."
echo "Dev user credentials are not stored in git; bootstrap-dev-user.sh creates them via the Keycloak Admin API."
