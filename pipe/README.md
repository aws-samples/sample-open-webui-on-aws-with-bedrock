# Open WebUI runtime integrations

[Documentation home](../docs/README.md) · [Gateway guide](../docs/GATEWAY_INTEGRATION_GUIDE.md) ·
[Metering guide](../docs/METERING.md)

These AWS-authored files configure the separately licensed Open WebUI
application at runtime. They do not modify, fork, vendor, or rebuild upstream
source.

## Files

| File | Installed when | Responsibility |
|---|---|---|
| [`gateway_anthropic_pipe.py`](gateway_anthropic_pipe.py) | Always | Global Claude manifold pipe: direct task-role catalog discovery, per-user gateway invocation, OpenAI↔Anthropic Messages translation, streaming/tools/images/usage. |
| [`seed.py`](seed.py) | Always | Waits for required schema tables, then idempotently installs the Claude pipe and native `gw`/`gwr` connections. Uses owner `system` when no admin exists. |
| [`metering_filter.py`](metering_filter.py) | `--metering` | Global inlet/outlet filter: soft-limit snapshot and fail-soft emission of persisted normalized usage to EventBridge. |
| [`metering_seed.py`](metering_seed.py) | `--metering` | Idempotently installs/refreshes the metering filter while preserving operator valve state. |

## Container-start behavior

`infra/lib/compute-stack.ts` packages these files as CDK S3 assets. The Fargate
command downloads the applicable assets, starts their seeders in the
background, and then runs the official image's `bash start.sh` unchanged.

The gateway seeder waits for schema readiness—not a first-admin login. It:

1. upserts the active/global Claude pipe;
2. inserts/reasserts native connection `gw` for Chat Completions;
3. inserts/reasserts native connection `gwr` for Responses; and
4. leaves model visibility and pipe valve choices under normal Open WebUI
   administration.

Seeder failure is logged but does not intentionally stop application startup.
Validate the integrations separately from the container health check.

## Identity behavior

The native connections use `auth_type: system_oauth`. The Claude pipe reads the
signed-in user's OAuth access token from Open WebUI and presents it to the
AgentCore gateway. AgentCore validates that Cognito token; the gateway IAM role
makes the downstream Mantle call.

Claude discovery is different because the pipe-discovery hook has no user
context: it signs a read-only direct Mantle catalog request with the Fargate
task role. `SIGV4_FALLBACK` is off by default. Enabling it also uses the task
role for inference when no user token exists, bypassing per-user gateway
attribution.

## Claude pipe valves

| Valve | Default | Meaning |
|---|---|---|
| `GATEWAY_INFERENCE_URL` | task environment | AgentCore `/inference` base URL. |
| `TARGET_PREFIX` | `bedrock/` | Gateway target qualifier. |
| `MANTLE_REGION` | task/AWS region | Direct discovery region. |
| `MODEL_FILTER` | empty | Exact-ID allowlist for discovered available `anthropic.*` models. |
| `MAX_TOKENS_DEFAULT` | `4096` | Messages default when the request omits `max_tokens`. |
| `SIGV4_FALLBACK` | `false` | Opt-in shared task-role inference when no user OAuth token exists. |
| `EMIT_USAGE` | `true` | Emit final OpenAI-shaped usage data when provider usage is available. |

## Metering capture boundary

The metering filter consumes usage Open WebUI persisted on an assistant
message. Only streamed Chat Completions has `include_usage` forced by the
interceptor; the other lanes depend on provider/upstream normalization. A
capture failure preserves chat and can leave an OPEN estimate for the sweeper.
The capture canary starts at EventBridge and does not test this filter.

See [`../docs/METERING.md`](../docs/METERING.md) before changing capture or
settlement semantics.
