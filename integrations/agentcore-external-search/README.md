# AgentCore external search for Open WebUI

A standalone AWS-hosted implementation of Open WebUI's documented
[External Search API](https://docs.openwebui.com/features/chat-conversations/web-search/providers/external).
Connect an existing Open WebUI installation using a search URL and API key. Open
WebUI can run anywhere with outbound HTTPS access; it needs no AWS credentials,
Cognito, sample inference gateway, custom tools, model presets, or application patches.

```text
Open WebUI → POST /search + bearer key → Lambda → IAM MCP Gateway → AgentCore Web Search
```

This integration only finds pages. Open WebUI retains its existing page loader,
retrieval/embedding settings, native tool loop, user permissions, and citations.
There is no Browser, custom URL loader, daily quota database, or user-attribution
system. Search relevance and provider-specific features are not identical to SerpAPI.

## Deploy independently

Prerequisites: an AWS account, AWS CLI credentials authorized to deploy this stack,
Node.js22 or newer, Python3 with pip (or `uv`), and a Git checkout containing this
directory. AgentCore Web Search is currently supported in `us-east-1`, `eu-west-1`,
and `ap-northeast-1`; the stack rejects other regions. No running Open WebUI resource
is an infrastructure dependency. Do not run the sample's root `deploy.sh`.

From this directory, with your chosen account/profile and region:

```sh
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --region "$AWS_REGION" --query Account --output text)
npm ci
npm run build
sh build.sh /absolute/new/external-search-bundle
npx cdk diff --profile "$AWS_PROFILE" -c account="$ACCOUNT" -c region="$AWS_REGION" -c assetPath=/absolute/new/external-search-bundle
npx cdk deploy --profile "$AWS_PROFILE" -c account="$ACCOUNT" -c region="$AWS_REGION" -c assetPath=/absolute/new/external-search-bundle
```

The build uses hash-locked Python3.12 ARM64 wheels and records the source commit.
Commit source changes before building. Set `UV=/path/to/uv` when using uv instead
of pip; it does not require a local Python3.12 interpreter. Keep bundles outside
the checkout and use a new destination for each build.

A new account/region may first need the standard CDK bootstrap. Review its bucket
and deployment-role changes before running `npx cdk bootstrap aws://ACCOUNT/REGION
--profile YOUR_PROFILE`; deployment never silently bootstraps the account.

The standalone stack owns only its search Gateway/target, adapter, key, IAM, logs,
and the small connector-version provisioning helper. That helper is required to pin
connector1.2.0 and synchronize discovery because the tested CloudFormation connector
source schema does not expose its version. It is not an agent or a second search service.

## Configure Open WebUI

In **Admin Panel → Settings → Web Search**:

1. Enable **Web Search**.
2. Set **Web Search Engine** to **external**.
3. Set **External Search URL** to the stack's `SearchURL` output.
4. Obtain the raw API key from the Secrets Manager secret identified by
   `ApiKeySecretArn`; put it in **External Search API Key** and save.

The key is a generated64-character shared service credential—not an AWS access key.
Treat it as a secret; do not commit it, include it in screenshots, or distribute it
to ordinary chat clients. Existing persisted admin settings can override environment
defaults; use the admin interface to configure an existing installation.

**Do not change the web loader or enable retrieval bypasses for this integration.**
Configure normal result counts, domain filters, embeddings, and model capabilities
as you would with another search provider. Native/agentic search still requires a
compatible model and Open WebUI's native tool-calling setting; no custom model is
required. Ordinary URL fetching remains the responsibility of your current loader.

The extension contract is tested against official Open WebUI **v0.11.3**, source
[`2a960a59fe1dbbd35282f0556b3666d81102e781`](https://github.com/open-webui/open-webui/tree/2a960a59fe1dbbd35282f0556b3666d81102e781).
Other releases implementing the same contract can integrate without this sample,
but revalidate when upgrading. This is not a guarantee for every Open WebUI release.

## HTTP contract and operational limits

```http
POST /search
Authorization: Bearer YOUR_EXTERNAL_SEARCH_API_KEY
Content-Type: application/json

{"query":"Python official documentation","count":5}
```

The response is a JSON array of `{"link":"https://...","title":"...","snippet":"..."}`
records, or `[]` for no usable results. These are indexed search snippets, not fetched
page contents. There is no `/load` endpoint and no automatic crawling or Browser fallback.

- Queries contain1–200 characters, per the managed service. Invalid queries are
  rejected rather than silently truncated.
- `count` is a positive integer, capped at the service's25-result maximum. Fewer
  results may be available; there is no arbitrary three-result limit.
- Requests are limited to8192 bytes and20 seconds of total upstream work, with a
  30-second Lambda timeout and reserved concurrency5. There are no daily quotas or
  automatic search retries. Client disconnect does not cancel the Lambda invocation.
- Unauthorized requests return401; invalid inputs400; unavailable/throttled upstream
  requests503; other upstream failures502; timeouts504. The pinned Open WebUI hook
  converts provider exceptions to empty results, so inspect sanitized adapter logs
  to distinguish a service failure from an honest empty search.
- Secret values are cached for up to60 seconds per warm execution environment.
  Allow that delay when rotating the key; failed refreshes do not reuse an expired key.
- Caller user/chat headers are not an authorization boundary. The adapter uses its
  own scoped IAM role, and the inference ledger does not account for these requests.

The Function URL is publicly reachable HTTPS with application bearer authentication,
not unauthenticated search access. Its IAM permissions allow URL invocation only;
ordinary direct Lambda invocation is not made public. Concurrency bounds are not
a WAF or a complete denial-of-service/cost-control system. Add organization-specific
controls separately if required; do not mistake this sample for a managed public SaaS.

## Costs, source attribution, and retention

Costs include AgentCore Web Search and Gateway requests, Lambda, logs, and one
Secrets Manager secret (standard list price about$0.40/month plus API calls).
Open WebUI's own page loading, embedding, and model usage remain separate costs.
No VPC, Browser resource, NAT gateway, or always-on adapter compute is created.
See [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/),
[Lambda pricing](https://aws.amazon.com/lambda/pricing/), and
[Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/).

[AWS acceptable use](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html)
requires source citations/links in end-user outputs using search results and prohibits
bulk extraction/storage/reproduction of results or populating a competing index/database.
Keep source links and citations. Ordinary Open WebUI history, logging, exports and
knowledge features remain application/operator responsibilities. This adapter neither
disables them nor asserts blanket compliance. Review retention and indexing use cases
under the service terms; the adapter itself does not persist queries or results.

## Validate and remove

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install pytest==9.1.1
.venv/bin/python -m pytest tests -q
npm test
```

CI additionally checks out the exact Open WebUI source above and sets
`OWUI_TEST_SOURCE_DIR` to execute its actual external-provider function with offline
dependencies. Without that source, those explicitly identified tests skip locally.
Mocks verify contracts, not managed-service availability. Live acceptance should cover
native search/citations, ordinary search with the installation's normal page-loading
and retrieval path, requested counts above3, an empty/error case, and unaffected chat.

To disconnect, disable Web Search or restore the previous provider settings in Open
WebUI. No application redeployment or database migration is needed. After all clients
stop using this endpoint, destroy only this standalone stack using the same CDK
context. The service-key secret is retained and needs explicit cleanup; log groups
are deleted with the stack. Review that deletion before destroying the stack;
never remove unrelated Open WebUI, inference, or user-data resources.
