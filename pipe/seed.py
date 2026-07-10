# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Seed the Amazon Bedrock gateway integration into the Open WebUI database.

Runs in the background at container start, next to the unmodified official
Open WebUI image. Waits for the `function` table (first boot runs the app's
alembic migrations) and the first admin user (Open WebUI makes the first
sign-up an admin), then idempotently upserts:

  1. The Claude manifold pipe function (pipe/gateway_anthropic_pipe.py) — serves
     the Anthropic Claude models, which are Messages-API-only on Bedrock.

  2. Two native OpenAI connections in the app config, both pointing at the
     AgentCore inference gateway with auth_type "system_oauth" (each request
     carries the logged-in user's own OAuth token) and a per-connection
     `x-models-flavor` header the gateway interceptor uses to return only
     API-compatible models:
        - "gw"  → Chat Completions lane  (flavor: chat_completions)
        - "gwr" → Responses lane          (flavor: responses, api_type: responses)

Together the three lanes surface every Bedrock model that works on Open WebUI,
all through one governed gateway with per-user identity.

Upsert semantics: INSERT the full row/config on a fresh install; on an existing
install, refresh only the pipe `content` and (re)assert the two connections if
absent — admin edits to valves, model visibility, or connection settings are
preserved. Idempotent; failure never blocks the app.

Environment (set by the CDK compute stack):
  GATEWAY_INFERENCE_URL  - gateway …/inference base URL (required to seed connections)
  DATABASE_URL           - composed by the container command
"""

import json
import logging
import os
import time
import urllib.parse

logging.basicConfig(level=logging.INFO, format="bedrock-gateway-seeder: %(message)s")
log = logging.getLogger(__name__)

PIPE_PATH = os.environ.get("CLAUDE_PIPE_PATH", "/tmp/gateway_anthropic_pipe.py")
FUNCTION_ID = "gateway_anthropic"
WAIT_SECONDS = int(os.environ.get("SEED_TIMEOUT", "600"))
GATEWAY_INFERENCE_URL = os.environ.get("GATEWAY_INFERENCE_URL", "").strip()


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


def upsert_pipe(cur, content) -> str:
    now = int(time.time())
    meta = {"description": "Anthropic Claude models via the AgentCore inference gateway (per-user OAuth).", "manifest": {}}
    valves = {}  # pipe reads GATEWAY_INFERENCE_URL / MANTLE_REGION from the task env; SigV4 fallback OFF by default
    cur.execute("SELECT 1 FROM function WHERE id = %s", (FUNCTION_ID,))
    if cur.fetchone():
        cur.execute("UPDATE function SET content = %s, updated_at = %s WHERE id = %s", (content, now, FUNCTION_ID))
        return "pipe updated (content only)"
    # Owner: prefer the first admin if one exists yet, else a placeholder ("system").
    # function.user_id is a plain string column (no FK), and is_global=true makes the
    # pipe visible to everyone regardless of owner — so we DON'T block on an admin
    # signing in. Seeding at first boot is what makes the models appear immediately.
    cur.execute("SELECT id FROM \"user\" WHERE role = 'admin' ORDER BY created_at LIMIT 1")
    row = cur.fetchone()
    owner = row[0] if row else "system"
    cur.execute(
        """INSERT INTO function (id, user_id, name, type, content, meta, created_at,
                                 updated_at, valves, is_active, is_global)
           VALUES (%s, %s, %s, 'pipe', %s, %s, %s, %s, %s, true, true)""",
        (FUNCTION_ID, owner, "Claude (Bedrock)", content, json.dumps(meta), now, now, json.dumps(valves)),
    )
    return f"pipe inserted (owner={owner})"


def _cfg_get(cur, key, default):
    # Per-key config table (key TEXT PK, value JSON).
    cur.execute("SELECT value FROM config WHERE key = %s", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def _cfg_set(cur, key, value):
    now = int(time.time())
    cur.execute(
        """INSERT INTO config (key, value, updated_at) VALUES (%s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
        (key, json.dumps(value), now),
    )


def seed_connections(cur) -> str:
    """(Re)assert the two gateway OpenAI connections in app config, without
    disturbing any other connections the admin has added."""
    if not GATEWAY_INFERENCE_URL:
        return "connections skipped (no GATEWAY_INFERENCE_URL)"
    gw_url = f"{GATEWAY_INFERENCE_URL.rstrip('/')}/v1"

    urls = _cfg_get(cur, "openai.api_base_urls", [])
    keys = _cfg_get(cur, "openai.api_keys", [])
    configs = _cfg_get(cur, "openai.api_configs", {})

    def ensure(prefix, api_type):
        # Skip if a connection to this gateway URL with this prefix already exists.
        for idx, u in enumerate(urls):
            c = configs.get(str(idx), {})
            if u == gw_url and c.get("prefix_id") == prefix:
                return False
        urls.append(gw_url)
        keys.append("")
        idx = len(urls) - 1
        conf = {
            "enable": True,
            "prefix_id": prefix,
            "model_ids": [],
            "connection_type": "external",
            "auth_type": "system_oauth",
            "tags": [],
            "headers": {"x-models-flavor": "responses" if api_type == "responses" else "chat_completions"},
        }
        if api_type == "responses":
            conf["api_type"] = "responses"
        configs[str(idx)] = conf
        return True

    added = []
    if ensure("gw", "chat_completions"):
        added.append("gw/chat_completions")
    if ensure("gwr", "responses"):
        added.append("gwr/responses")

    if added:
        _cfg_set(cur, "openai.enable", True)
        _cfg_set(cur, "openai.api_base_urls", urls)
        _cfg_set(cur, "openai.api_keys", keys)
        _cfg_set(cur, "openai.api_configs", configs)
        return f"connections added: {', '.join(added)}"
    return "connections already present"


def main():
    content = open(PIPE_PATH).read()
    deadline = time.time() + WAIT_SECONDS
    pipe_done = False
    conns_done = False
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

                if not pipe_done:
                    log.info(upsert_pipe(cur, content))
                    pipe_done = True

                if not conns_done and table_exists(cur, "config"):
                    log.info(seed_connections(cur))
                    conns_done = True

            conn.close()
            if pipe_done and conns_done:
                log.info("seeding complete")
                return
        except Exception as e:  # noqa: BLE001
            log.info(f"waiting for database ({e.__class__.__name__}: {e})")
        time.sleep(10)
    log.warning(
        f"gave up after {WAIT_SECONDS}s (pipe_done={pipe_done}, conns_done={conns_done}); "
        "finish setup via the admin UI — see docs/GATEWAY_INTEGRATION_GUIDE.md"
    )


if __name__ == "__main__":
    main()
