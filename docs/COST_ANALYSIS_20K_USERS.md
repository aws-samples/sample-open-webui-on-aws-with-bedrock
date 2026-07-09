# Cost Analysis: Open WebUI on AWS with Amazon Bedrock — 20,000-User Deployment

This analysis projects infrastructure and LLM consumption costs for deploying Open WebUI with Amazon Bedrock for an organization serving up to 20,000 users. The worked example uses a university persona (faculty and students), but the math generalizes to any large user base.

> **Price disclaimer (2026):** all prices in this document are point-in-time
> estimates from the original 2026 analysis. AWS infrastructure pricing and
> Bedrock per-token pricing change frequently and vary by model and region —
> re-verify with the [AWS Pricing Calculator](https://calculator.aws/) and the
> [Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) before
> budgeting.
>
> **Scope note:** the quota/limit strategies discussed below are **cost-control
> strategies, not features of this sample** — per-user/per-group token quota
> enforcement is not included. Equivalent ceilings can be achieved with
> Bedrock service quotas, CloudWatch usage alarms, application inference
> profiles for cost allocation, or by extending the sample.

---

## Executive Summary

| Category | Monthly Cost (Low) | Monthly Cost (Mid) | Monthly Cost (High) |
|---|---|---|---|
| AWS Infrastructure | $650 | $1,200 | $2,500 |
| Bedrock LLM Consumption | $2,800 | $12,000 | $45,000 |
| **Total** | **$3,450** | **$13,200** | **$47,500** |
| **Per-user/month** | **$0.17** | **$0.66** | **$2.38** |
| **Annual** | **$41,400** | **$158,400** | **$570,000** |

The dominant cost driver is LLM token consumption (80-95% of total), not infrastructure. Model selection and per-user usage limits (however you choose to enforce them — see the scope note above) are the primary cost levers.

---

## 1. Usage Assumptions

### User Segmentation

Based on published higher education AI adoption data (Digital Education Council 2024, WebFX ChatGPT usage study, StudyAgent 2025):

| Segment | Users | % of Total | Description |
|---|---|---|---|
| Heavy users (daily) | 2,000 | 10% | Power users: CS students, researchers, writing-intensive faculty |
| Regular users (weekly) | 6,000 | 30% | Use AI several times per week for assignments, prep, grading |
| Light users (monthly) | 6,000 | 30% | Occasional use for specific tasks |
| Inactive | 6,000 | 30% | Registered but rarely/never use |
| **Active users** | **14,000** | **70%** | |

### Per-Session Token Consumption

Based on WebFX's analysis of 13,252 ChatGPT conversations (average 348 words, 1.7 messages per session) and typical education use cases:

| Use Case | Input Tokens | Output Tokens | Total Tokens | Notes |
|---|---|---|---|---|
| Quick question/answer | 200 | 400 | 600 | Simple factual query |
| Essay feedback/review | 1,500 | 800 | 2,300 | Student submits draft, gets feedback |
| Concept explanation | 300 | 1,200 | 1,500 | "Explain X in detail" |
| Code help (CS courses) | 500 | 1,500 | 2,000 | Debug or explain code |
| Research assistance | 800 | 1,500 | 2,300 | Literature review, summarization |
| Lesson plan generation | 500 | 2,000 | 2,500 | Faculty creating materials |
| Multi-turn conversation | 2,000 | 3,000 | 5,000 | Extended back-and-forth (5-8 turns) |
| **Weighted average session** | **~800** | **~1,200** | **~2,000** | |

Note: Input tokens grow with conversation length as the full history is sent with each turn. A 5-turn conversation's final message includes all prior context.

### Monthly Session Estimates

| User Segment | Users | Sessions/Month | Tokens/Session | Monthly Tokens |
|---|---|---|---|---|
| Heavy (daily) | 2,000 | 40 | 3,000 | 240M |
| Regular (weekly) | 6,000 | 12 | 2,000 | 144M |
| Light (monthly) | 6,000 | 3 | 1,500 | 27M |
| **Total** | **14,000** | | | **411M** |

**Baseline estimate: ~400M tokens/month** across all active users.

---

## 2. Bedrock LLM Cost Projections

### Model Pricing (On-Demand, Bedrock, March 2026)

| Model | Input/1M Tokens | Output/1M Tokens | Blended*/1M Tokens | Best For |
|---|---|---|---|---|
| Amazon Nova Micro | $0.035 | $0.14 | $0.10 | Simple Q&A, classification |
| Amazon Nova Lite | $0.06 | $0.24 | $0.17 | Fast multimodal tasks |
| Amazon Nova Pro | $0.80 | $3.20 | $2.24 | Complex reasoning, balanced cost |
| Claude Haiku 4.5 | $1.00 | $5.00 | $3.40 | Fast, capable, good value |
| Claude Sonnet 4.6 | $3.00 | $15.00 | $10.20 | Best quality/cost balance |
| Claude Opus 4.6 | $5.00 | $25.00 | $17.00 | Flagship, complex tasks |
| Llama 3.1 70B | $2.65 | $3.50 | $3.14 | Open-source, good reasoning |

*Blended rate assumes 40% input / 60% output token ratio (typical for education — short prompts, longer responses).

### Cost Scenarios by Model Strategy

#### Scenario A: Cost-Optimized (Nova-First)
Route most traffic to Amazon Nova models, reserve Claude for complex tasks.

| Model | % Traffic | Monthly Tokens | Monthly Cost |
|---|---|---|---|
| Nova Micro (simple Q&A) | 40% | 164M | $16 |
| Nova Pro (reasoning) | 40% | 164M | $367 |
| Claude Sonnet 4.6 (complex) | 20% | 82M | $836 |
| **Total** | | **411M** | **$1,220** |

#### Scenario B: Balanced (Claude Haiku + Sonnet)
Use Claude Haiku as the default, Sonnet for advanced tasks.

| Model | % Traffic | Monthly Tokens | Monthly Cost |
|---|---|---|---|
| Claude Haiku 4.5 (default) | 60% | 247M | $840 |
| Claude Sonnet 4.6 (advanced) | 30% | 123M | $1,255 |
| Nova Pro (overflow/cost) | 10% | 41M | $92 |
| **Total** | | **411M** | **$2,187** |

#### Scenario C: Quality-First (Claude Sonnet Default)
Claude Sonnet as the primary model for all users.

| Model | % Traffic | Monthly Tokens | Monthly Cost |
|---|---|---|---|
| Claude Sonnet 4.6 (default) | 70% | 288M | $2,938 |
| Claude Opus 4.6 (research) | 10% | 41M | $697 |
| Claude Haiku 4.5 (quick) | 20% | 82M | $279 |
| **Total** | | **411M** | **$3,914** |

### Scaling with Adoption

Token consumption scales non-linearly with adoption. As more users become regular users:

| Adoption Level | Active Users | Monthly Tokens | Cost (Scenario B) |
|---|---|---|---|
| Early (semester 1) | 5,000 | 120M | $640 |
| Growing (semester 2) | 10,000 | 300M | $1,600 |
| **Baseline (steady state)** | **14,000** | **411M** | **$2,187** |
| High adoption | 17,000 | 600M | $3,200 |
| Peak (finals week) | 14,000 | 800M | $4,260 |

Finals weeks and midterms can see 2x normal usage as students use AI for study assistance and essay review.

---

## 3. Infrastructure Cost Projections

Infrastructure costs scale with concurrent users, not total users. At 20,000 total users with 70% active, peak concurrent users during class hours might be 1,000-3,000.

### Compute (ECS Fargate)

| Load Level | Tasks | vCPU | Memory | Monthly Cost |
|---|---|---|---|---|
| Low (off-hours, breaks) | 1 | 1 | 2 GB | $35 |
| Normal (weekday) | 3 | 3 | 6 GB | $105 |
| Peak (midday, finals) | 8 | 8 | 16 GB | $280 |
| **Weighted average** | | | | **$120-200** |

Auto-scaling (1-10 tasks) handles this automatically. ECS Fargate pricing: ~$0.04/vCPU-hour + ~$0.004/GB-hour.

### Database (Aurora PostgreSQL Serverless v2)

| Load Level | ACUs | Monthly Cost |
|---|---|---|
| Idle (nights, weekends) | 0.5 | — |
| Normal | 2-4 | — |
| Peak | 6-8 | — |
| **Weighted average** | ~2 ACU avg | **$90-180** |

Aurora Serverless v2 pricing: ~$0.12/ACU-hour. Scales to 0.5 ACU during idle.

### Cache (ElastiCache Redis)

| Configuration | Monthly Cost |
|---|---|
| cache.t3.micro (dev/small) | $13 |
| cache.t3.small (production) | $25 |
| cache.m5.large (high concurrency) | $120 |

For 20K users, `cache.t3.small` is sufficient. Redis handles session caching and Socket.IO state sharing — lightweight operations.

### Networking

| Component | Monthly Cost | Notes |
|---|---|---|
| NAT Gateway | $35 + data | $0.045/hr + $0.045/GB processed |
| NAT data processing | $10-50 | Depends on external API calls (Ollama, OpenAI) |
| CloudFront | $10-30 | 1 distribution, mostly static asset caching |
| ALB | $18-25 | Internal, light traffic |
| VPC Endpoints | $0 | Included (interface endpoints for S3, ECR, Bedrock, etc.) |

Wait — VPC interface endpoints are NOT free. Let me correct:

<Actually, VPC Gateway endpoints (S3, DynamoDB) are free. Interface endpoints cost ~$0.01/hr per AZ. With endpoints for ECR, Bedrock, CloudWatch, Secrets Manager across 2 AZs:>

| VPC Endpoints | Count × AZs | Monthly Cost |
|---|---|---|
| Interface endpoints (ECR, Bedrock, CW, SM) | ~6 × 2 AZs | $87 |

This is a significant hidden cost. Consider whether all endpoints are needed or if NAT Gateway can handle some traffic.

### Storage

| Component | Monthly Cost |
|---|---|
| S3 (file uploads, 50 GB) | $1-2 |
| ECR (container images, 5 GB) | $0.50 |
| CloudWatch Logs (50 GB/month) | $25 |
| Aurora storage (20 GB) | $2 |

### Secrets & Security

| Component | Monthly Cost |
|---|---|
| Secrets Manager (3 secrets) | $2 |
| ACM certificate | Free |
| Cognito (Essentials tier) | See below |

### Cognito Pricing (Essentials Tier)

Cognito Essentials pricing is based on Monthly Active Users (MAU):

| MAU Tier | Price/MAU | Monthly Cost |
|---|---|---|
| First 10,000 | $0.015 | $150 |
| Next 90,000 | $0.0065 | — |

For 14,000 active users: 10,000 × $0.015 + 4,000 × $0.0065 = **$176/month**

Note: First 10,000 MAU are free for the first 12 months on new accounts.

### Infrastructure Cost Summary

| Component | Low | Mid | High |
|---|---|---|---|
| ECS Fargate (1-10 tasks) | $35 | $150 | $350 |
| Aurora Serverless v2 | $45 | $130 | $250 |
| ElastiCache Redis | $13 | $25 | $120 |
| NAT Gateway + data | $35 | $50 | $80 |
| CloudFront | $10 | $20 | $40 |
| ALB | $18 | $22 | $30 |
| VPC Interface Endpoints | $87 | $87 | $87 |
| CloudWatch Logs | $10 | $25 | $50 |
| S3 + ECR | $2 | $3 | $5 |
| Secrets Manager | $2 | $2 | $2 |
| Cognito (Essentials) | $0* | $176 | $176 |
| **Total Infrastructure** | **$257** | **$690** | **$1,190** |

*Free tier for first 12 months.

---

## 4. Cost Optimization Strategies

### Model Routing (Biggest Lever)

Implement intelligent model routing based on task complexity:

| Strategy | Savings vs. Sonnet-for-all | How |
|---|---|---|
| Default to Nova Pro, upgrade on demand | 60-70% | Use Nova Pro as default, let users select Sonnet/Opus |
| Default to Haiku, Sonnet for faculty | 40-50% | Group-based model access (already built) |
| Auto-route by prompt complexity | 50-60% | Pipe Function that classifies and routes |

Our group-based model access control is perfectly suited for this. In the admin UI, set expensive models (Sonnet, Opus) to Private and grant access only to faculty/power-users groups. Use `scripts/set-model-access.sh` for bulk operations:

```bash
./scripts/set-model-access.sh --url $URL --token $TOKEN --group faculty --pattern "us.anthropic.claude-*"
./scripts/set-model-access.sh --url $URL --token $TOKEN --group basic-users --pattern "us.amazon.nova-*"
```

### Token Budgets as a Cost Ceiling (strategy — enforcement not included)

A hard per-user token budget bounds worst-case spend. **This sample does not
enforce per-user token quotas**; the table below is a planning model for what
tiered budgets would cap costs at, whichever enforcement mechanism you choose:

| Budget Strategy | Daily (tokens) | Monthly (tokens) | Max Cost/User/Month (Haiku) |
|---|---|---|---|
| Conservative | 10,000 | 200,000 | $0.68 |
| Standard | 25,000 | 500,000 | $1.70 |
| Generous | 50,000 | 1,000,000 | $3.40 |
| Faculty (unbounded) | — | — | Variable |

Ways to realize these ceilings without application-level enforcement:

- **Bedrock service quotas** — per-model requests/tokens-per-minute ceilings at
  the account level (coarse, but a real hard stop).
- **CloudWatch alarms on Bedrock usage metrics** — alert (or trigger an
  automation) when daily/monthly token consumption crosses a threshold.
- **Application inference profiles** — tag Bedrock usage per workload for cost
  allocation and per-profile monitoring.
- **Group-based model access** (included in this sample) — restrict expensive
  models to small groups via `scripts/set-model-access.sh`; the cheapest
  effective control because the price gap between models is the largest lever.
- **Extend the sample** — Open WebUI's middleware pipeline offers inlet/outlet
  hooks where a quota service could be added; per-response usage is already
  emitted in OpenAI-shape fields.

With 14,000 active users at the "Standard" budget on Haiku: worst-case ceiling = 14,000 × $1.70 = **$23,800/month**. In practice, most users won't reach their budget, so actual cost is 30-50% of the ceiling.

### Infrastructure Optimization

| Optimization | Savings | Trade-off |
|---|---|---|
| Remove unnecessary VPC endpoints | $50-70/month | Traffic routes through NAT instead (slightly higher latency) |
| Use Aurora I/O-Optimized | 10-20% on DB | Better for read-heavy workloads |
| Schedule ECS scale-down overnight | $30-50/month | 1 task from midnight-6am |
| Use CloudFront caching aggressively | $5-10/month | Static assets cached at edge |

### Batch Processing for Non-Interactive Use Cases

Bedrock Batch Inference is 50% off on-demand pricing. Suitable for:
- Bulk essay grading/feedback (faculty uploads batch)
- Course material generation
- Automated quiz generation

If 20% of token volume shifts to batch: saves ~$200-800/month depending on model mix.

---

## 5. Comparison: Open WebUI + Bedrock vs. Alternatives

| Solution | Per-User/Month | 20K Users/Month | Notes |
|---|---|---|---|
| **Our solution (Scenario B)** | **$0.66** | **$13,200** | Self-hosted, full control, multi-model |
| ChatGPT Team (OpenAI) | $25-30 | $500K-600K | Per-seat licensing, no volume discount |
| Claude for Education (Anthropic) | $12-18* | $240K-360K | Estimated edu pricing, per-seat |
| Copilot for Education (Microsoft) | $0-5* | $0-100K | Bundled with M365 A5, limited models |
| LiteLLM + Bedrock (self-hosted) | $0.50-0.80 | $10K-16K | Similar infra, less integrated UI |

*Estimated based on published education pricing tiers.

Our solution is **20-40x cheaper** than per-seat SaaS alternatives because we pay only for actual token consumption, not per-seat licenses. The 70% of users who are light or inactive cost nearly nothing.

---

## 6. Recommended Configuration for Launch

### Phase 1: Pilot (Semester 1, ~2,000 users)
- **Models:** Claude Haiku 4.5 (default) + Nova Pro (cost-conscious option)
- **Usage budgets:** plan for 25K daily / 500K monthly tokens per user (see §5 — enforcement external to this sample)
- **Infrastructure:** Minimum config (1-3 ECS tasks, 0.5-2 ACU Aurora)
- **Estimated cost:** $800-1,500/month
- **Goal:** Establish usage baselines, gather feedback

### Phase 2: Expansion (Semester 2, ~10,000 users)
- **Models:** Add Claude Sonnet 4.6 for faculty and graduate students
- **Usage budgets:** tiered by group (faculty generous, undergrad standard)
- **Infrastructure:** Scale to 3-5 ECS tasks, 2-4 ACU Aurora
- **Estimated cost:** $5,000-10,000/month
- **Goal:** Validate cost model at scale

### Phase 3: Full Deployment (Semester 3+, ~20,000 users)
- **Models:** Full model menu with group-based access
- **Usage budgets:** refined based on Phase 1-2 data
- **Infrastructure:** Auto-scaling 1-10 tasks
- **Estimated cost:** $10,000-20,000/month
- **Goal:** Steady-state operations

---

## 7. Key Takeaways

1. **LLM tokens are 80-95% of total cost.** Infrastructure is a rounding error compared to model consumption.

2. **Model selection is the #1 cost lever.** Nova Pro at $2.24/M blended vs. Claude Sonnet at $10.20/M is a 4.5x difference for the same token volume.

3. **Per-seat alternatives are dramatically more expensive.** At $25-30/user/month for ChatGPT Team, 20K users = $500K+/month. A pay-per-token model with 30% inactive users and tiered usage budgets brings this to $10-20K/month.

4. **Group-based access control is a cost management tool**, not just a security feature. Restricting expensive models to faculty/grad students while giving undergrads access to Haiku/Nova is the most effective cost optimization.

5. **Token budgets provide a hard cost ceiling** — when enforced. This sample doesn't enforce them; pick a mechanism from §5 so the bound is real rather than aspirational.

6. **Usage is seasonal.** Expect 2x spikes during peak periods (e.g., midterms and finals). Auto-scaling infrastructure handles this; bound the proportional LLM cost spike with the §5 controls.

7. **VPC interface endpoints are a hidden $87/month cost.** Evaluate whether all are needed or if NAT Gateway can handle some traffic.
