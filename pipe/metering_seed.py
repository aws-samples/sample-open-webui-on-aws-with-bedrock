# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Seed the metering filter into the Open WebUI database (metering module only).

Runs in the background at container start, next to the unmodified official
image, ONLY when the compute stack sets METERING_ENABLED=true — the base
sample's seeder (pipe/seed.py) is untouched, which keeps the lean-core gate
(off-state bit-identity) honest.

Behavior mirrors seed.py's contract: wait for migrations, then idempotently
upsert the metering global filter function. Re-runs refresh the filter
`content` only, so admin edits to valves survive redeploys. The filter row's
content hash is emitted to the log for the integrity check (design §4.8).
Idempotent; failure never blocks the app.
"""

import hashlib
import json
import logging
import os
import time
import urllib.parse

logging.basicConfig(level=logging.INFO, format="metering-seeder: %(message)s")
log = logging.getLogger(__name__)

FILTER_PATH = os.environ.get("METERING_FILTER_PATH", "/tmp/metering_filter.py")
FUNCTION_ID = "metering"
WAIT_SECONDS = int(os.environ.get("SEED_TIMEOUT", "600"))


def connect():
    import psycopg2

    parsed = urllib.parse.urlparse(os.environ["DATABASE_URL"])
    return psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=urllib.parse.unquote(parsed.username or ""),
        password=urllib.parse.unquote(parsed.password or ""),
        dbname=parsed.path.lstrip("/"),
    )


def table_exists(cur, name) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None


def upsert_filter(cur, content) -> str:
    now = int(time.time())
    meta = {
        "description": "Usage metering capture + soft-quota warnings (opt-in metering module).",
        "manifest": {},
    }
    cur.execute("SELECT 1 FROM function WHERE id = %s", (FUNCTION_ID,))
    if cur.fetchone():
        cur.execute(
            "UPDATE function SET content = %s, updated_at = %s WHERE id = %s",
            (content, now, FUNCTION_ID),
        )
        return "filter updated (content only)"
    cur.execute('SELECT id FROM "user" WHERE role = \'admin\' ORDER BY created_at LIMIT 1')
    row = cur.fetchone()
    owner = row[0] if row else "system"
    cur.execute(
        """INSERT INTO function (id, user_id, name, type, content, meta, created_at,
                                 updated_at, valves, is_active, is_global)
           VALUES (%s, %s, %s, 'filter', %s, %s, %s, %s, %s, true, true)""",
        (FUNCTION_ID, owner, "Usage Metering", content, json.dumps(meta), now, now, json.dumps({})),
    )
    return f"filter inserted (owner={owner})"


def main():
    if os.environ.get("METERING_ENABLED", "").lower() != "true":
        log.info("METERING_ENABLED not true; nothing to do")
        return
    content = open(FILTER_PATH).read()
    log.info(f"filter content sha256={hashlib.sha256(content.encode()).hexdigest()}")
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        try:
            conn = connect()
            conn.autocommit = True
            with conn.cursor() as cur:
                if not table_exists(cur, "function"):
                    log.info("waiting for migrations (function table)")
                    conn.close()
                    time.sleep(10)
                    continue
                log.info(upsert_filter(cur, content))
            conn.close()
            log.info("metering seeding complete")
            return
        except Exception as e:  # noqa: BLE001
            log.info(f"waiting for database ({e.__class__.__name__}: {e})")
        time.sleep(10)
    log.warning(f"gave up after {WAIT_SECONDS}s — install the filter via the admin UI (docs/METERING.md)")


if __name__ == "__main__":
    main()
