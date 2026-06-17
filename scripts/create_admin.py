#!/usr/bin/env python3
"""AUTH-2a: one-off admin-account seed.

Reads ``ADMIN_USERNAME`` and ``ADMIN_PASSWORD`` from the environment and
creates a single ``role='admin'`` account. Idempotent: if the username
already exists, prints a non-secret notice and exits 0.

This script NEVER prints, logs, or hardcodes the password or its hash, and
NEVER falls back to a default credential. The password is read from the
environment only and handed straight to the (hashing) create helper.

Operator usage (post-deploy, NOT run by CI):

    # Render also needs USE_POSTGRES_WRITE=true + DATABASE_URL set.
    ADMIN_USERNAME=... ADMIN_PASSWORD=... python scripts/create_admin.py

Exit codes:
    0 — admin created, or already existed (idempotent)
    1 — missing/blank ADMIN_USERNAME or ADMIN_PASSWORD, or a persist error
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python scripts/create_admin.py` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    username = (os.environ.get("ADMIN_USERNAME") or "").strip()
    # Do NOT strip the password — leading/trailing characters may be intended.
    password = os.environ.get("ADMIN_PASSWORD") or ""

    if not username or not password:
        print(
            "ERROR: both ADMIN_USERNAME and ADMIN_PASSWORD must be set "
            "(non-empty) in the environment. No account created.",
            file=sys.stderr,
        )
        return 1

    # Imported only after the env check so the missing-creds path needs no
    # DATABASE_URL / DB connectivity.
    import database

    try:
        existing = database.get_account_by_username(username)
    except Exception as exc:  # surfaced to operator; no secret in message
        print(f"ERROR: could not query accounts: {exc}", file=sys.stderr)
        return 1

    if existing:
        print("admin already exists; no changes made.")
        return 0

    try:
        database.create_account(username, password, role="admin")
    except database.AccountExistsError:
        # Race / pre-existing — treat as idempotent success.
        print("admin already exists; no changes made.")
        return 0
    except Exception as exc:  # no secret in message
        print(f"ERROR: failed to create admin account: {exc}", file=sys.stderr)
        return 1

    print("admin account created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
