# Bedrock gateway integration — runtime pieces

These files deliver the Amazon Bedrock integration *without touching the Open
WebUI image*. They run beside the unmodified official container and configure it
at start. See [`../docs/GATEWAY_INTEGRATION_GUIDE.md`](../docs/GATEWAY_INTEGRATION_GUIDE.md)
for the full design.

## Files

| File | Purpose |
|---|---|
| `gateway_anthropic_pipe.py` | Open WebUI manifold **pipe** for Anthropic Claude models (Messages-API-only on Bedrock). Translates OpenAI ↔ Anthropic Messages and calls the AgentCore gateway with the logged-in user's OAuth token. |
| `seed.py` | Container-start **seeder**: waits for DB migrations + the first admin sign-in, then installs the pipe and the two gateway-backed OpenAI connections. Idempotent. |

## How they deploy

`infra/lib/compute-stack.ts` uploads both files as CDK S3 assets and prefixes
the container command with a small bootstrap: the official image (which already
ships `python` + `boto3`) downloads the two files and runs `seed.py` in the
background, then `exec bash start.sh` unchanged. The seeder:

1. waits for the `function` table (first boot runs upstream's migrations),
2. waits for the first admin user (Open WebUI promotes the first sign-in),
3. upserts the Claude pipe (active + global), and
4. (re)asserts two OpenAI connections pointing at the gateway:
   - `gw` — Chat Completions lane (`x-models-flavor: chat_completions`),
   - `gwr` — Responses lane (`api_type: responses`, `x-models-flavor: responses`).

Both connections use `auth_type: system_oauth`, so each request carries the
logged-in user's own Cognito token. Re-runs refresh the pipe code and only add
connections that are absent — admin edits to valves or model visibility survive.

If seeding can't complete (e.g. no admin signs in within the timeout), the app
still runs; finish setup from **Admin → Functions / Connections** per the
integration guide. Failure never blocks or crashes the container.

## The Claude pipe's auth model

`SIGV4_FALLBACK` defaults to **off**: every request uses the user's OAuth token
through the gateway (per-user identity). A user with no OAuth session gets a
clear "sign in with SSO" error. Turn the valve **on** to let non-SSO users
(e.g. local-password logins) fall back to the ECS task role (SigV4, direct to
Bedrock) — this works but routes as a single shared identity, so it's opt-in.

## Valves (Claude pipe)

| Valve | Default | Meaning |
|---|---|---|
| `GATEWAY_INFERENCE_URL` | from `GATEWAY_INFERENCE_URL` task env | Gateway `…/inference` base URL (set by the CDK deployment). |
| `TARGET_PREFIX` | `bedrock/` | Gateway target qualifier for routing. |
| `MANTLE_REGION` | from `MANTLE_REGION` task env | Region for model discovery. |
| `MODEL_FILTER` | *(empty — all `anthropic.*`)* | Optional allow-list of model ids. |
| `SIGV4_FALLBACK` | `false` | Opt-in task-role fallback for non-SSO users. |
| `EMIT_USAGE` | `true` | Emit the final usage chunk (native token display). |

## Capabilities

Streaming + non-streaming, OpenAI-shape tool calling, vision (base64 images),
per-response token usage, and Claude extended-thinking rendered as Open WebUI
`reasoning_content`. Model access control is stock Open WebUI (Cognito groups →
Workspace → Models).
