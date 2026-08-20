<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Fable Brief — gateway-bound model pricing catalog

Run this in Claude Code with Fable 5 at high effort, from the repository root,
with the `test-web-app` AWS profile authenticated. Built from the Fable Brief +
"Delegate a task overnight" + "Design a dynamic workflow before executing"
templates in the Claude Fable 5 Operating Pack.

---

## The prompt

```text
I'm handing you this task to run unsupervised overnight. You have full autonomy to
research, design, build, test, deploy, and validate against the live environment.

## The problem

This repository's optional metering module prices Bedrock model usage from the AWS
Price List. The pricing subsystem works, but it has three compounding problems:

1. **The two sets that matter are never joined.** The set of models a user can
   actually invoke through the AgentCore inference gateway lives in
   `config/model-capabilities.json` (three lanes: chat_completions, responses,
   messages) and is served to the interceptor as the `MODEL_CAPS` env var. The set
   of models that have a price lives in DynamoDB under `PRICING#<model_id>`.
   **Nothing in the codebase compares them.** Verify this yourself, then treat it
   as the root cause. Consequences: a gateway-invokable model with no pricing row
   is invisible on the admin console pricing page (the catalog is built by scanning
   `PRICING#`, so a model with no row simply does not appear), and "unpriced" is
   only ever discovered reactively, one settled request at a time, via the
   `UnpricedModel` CloudWatch metric.

2. **Unpriced models are effectively unmetered.** In `metering/debit/index.py`,
   `unpriced = rin is None or rout is None` then `usd = 0.0 if unpriced else …`.
   The admission path in `gateway/metering-interceptor/index.py` estimates an
   unresolvable model at exactly $0 and admits it. So any model without a rate
   consumes **zero dollars** against a dollar-denominated quota — it is unlimited.
   This is the real stake: missing prices are a quota bypass, not a cosmetic gap.

3. **The pricing logic is more complicated than the problem requires.** Specific
   hot spots I want you to evaluate for simplification, not preserve out of
   deference:
   - `metering/pricing/offers.py` implements **four** near-duplicate usage-type
     grammars (`_classify_snake`, `_classify_camel`, `_classify_mantle`, and the
     `_LEGACY_DIR_RE` dash shapes). The qualifier vocabulary is spelled three
     times in three casings. An unrecognized qualifier returns `None`, which
     **silently drops the rate** with no unmatched row and no metric — a new AWS
     qualifier token becomes a silently missing price.
   - `metering/pricing/identity.py` carries a hand-curated `_NOISE_TOKENS`
     denylist and a `_SUFFIX_RULES` trailing-`-N` regex whose exact tightness is
     the only thing preventing documented silent mispricing.
   - `metering/pricing/resolver.py` duplicates dual row-shape tolerance in both
     `_override_rates` and `_published_grid`, including a legacy `tiers`→grid lift
     that **nothing in the current write path produces**. Its fallback matrix is
     up to 36 lookups (routing 3 × ladder 4 × direction chain 3) with three
     overlapping boolean fallback flags.
   - `MODEL_ID_RE` is defined in `identity.py`, imported by the admin API, and
     re-typed as an inline literal in `console/src/pages/PricingPage.tsx`
     (`BindModal`). `gridStd()` in that same file re-implements a fixed slice of
     the resolver, so the table can disagree with the resolver's `effective`.
   - Two full-table `Scan`s of `PRICING#` (refresher `_gc`, admin API
     `_pricing_rows` with a 5000-item cap), and divergent pricing cache TTLs
     (60s in the estimate path, 300s in settle) for the same rows.

Two known symptoms to use as entry points, not as the whole scope:

- **`openai.gpt-5.6` models have no price.** Two contradictory clues, and resolving
  the contradiction is the task. `offers.py`'s own docstring cites
  `USE1-openai.gpt-5.6-luna-mantle-cache-write-tokens-5m-long-ctx-flex` as an
  observed usage-type shape, and mantle shapes carry a direct model id — so this
  *should* resolve via `direct-id` with no name join at all. But the external
  reference repo reports that proprietary GPT-5.x SKUs are **not surfaced through
  the Price List API or the bulk offer files** in commercial regions. Both cannot be
  fully true. Go look at the live offer files and determine which it is: a parsing
  or join defect on our side, or an AWS publishing gap that requires an override.
  Instrument and prove it; do not reason it out from the docstring.
- **The "Unmatched Price List entries" list contains what look like old or
  deprecated models.** Be precise about the vocabulary here, because it changes
  the fix: `unmatched` means a *Price List entry* (identified only by display
  name) that could not be bound to a Bedrock model id. It does **not** mean
  "a model with no price". Many unmatched entries are almost certainly historical
  SKUs for models that are not available through this gateway at all, in which
  case the correct outcome is to stop surfacing them as actionable work rather
  than to bind them. Decide this from evidence.

## Done means

A comprehensive model pricing catalog, dynamically derived from what the gateway
actually serves, bound to the metering admin implementation, deployed and verified
in the live test environment. Concretely, all of the following must be true and
evidenced:

1. **Zero gateway-available models without a price.** Every model reachable
   through the inference gateway resolves a non-null input and output rate. This
   is the primary success criterion.
2. **The gateway↔pricing join exists as a first-class, automated artifact** — not
   a one-off script you ran. Coverage is queryable and alarmable: a gateway model
   with no price must be a visible, named condition, not something discovered when
   a request settles.
3. **The admin console shows gateway-availability as part of the pricing surface**,
   so an operator can see "these models are invokable and priced" and "these are
   invokable and unpriced" without reading logs or metrics.
4. **The unmatched queue reflects only actionable work** — entries that plausibly
   correspond to a gateway-available model. Non-actionable historical entries are
   either filtered with a documented rule or explicitly classified.
5. **The pricing logic is measurably simpler** — fewer code paths, fewer
   duplicated rules, dead compatibility branches removed. Quantify it (lines,
   branches, duplicated rule sites eliminated) rather than asserting it.
6. **Silent rate drops are eliminated.** Any Price List dimension the parser
   cannot classify must produce a visible signal, not a `None` return.
7. `python -m pytest metering/tests -q` passes, with new tests covering the join,
   the gpt-5.6 case as a regression guard, and each simplification.
8. Deployed to the live environment and validated there, with evidence.
9. **Documentation across the entire project is validated and refactored to match
   what you built.** Not an afterthought paragraph — treat it as a deliverable
   equal in weight to the code. See the section below.

## Sources to inspect

Read before changing anything:

- `metering/pricing/` — `identity.py`, `resolver.py`, `offers.py`, `__init__.py`
- `metering/pricing-refresher/index.py` — the only price ingestion path
- `metering/debit/index.py` — settle path, unpriced behavior
- `gateway/metering-interceptor/index.py` — admission estimate, `CAPS` load
- `metering/admin-api/index.py` — `_pricing_rows`, `_catalog`, override/alias
  routes, `_pricing_meta`, `_refresh_pricing`
- `console/src/pages/PricingPage.tsx`, `console/src/types.ts`
- `gateway/refresher/probe_core.py` — `fetch_catalog()` does a SigV4 GET against
  `https://bedrock-mantle.{region}.api.aws/v1/models` and keeps
  `status == "available"`. This is proven, cheap, live gateway discovery. Strongly
  consider it as the authoritative "available through the gateway" source.
- `gateway/refresher/index.py` — the existing scheduled discovery loop, including
  `_current_caps()` reading `MODEL_CAPS` off the Lambda config, and the collapse
  guard. Reuse these patterns rather than inventing new ones.
- `gateway/interceptor/index.py`, `config/model-capabilities.json`,
  `scripts/probe-model-capabilities.py`
- `infra/lib/metering-stack.ts` — `stageWithPricing()`, the refresher Lambda, the
  24h schedule, `PricingRefreshAlarm`, `PricingUnmatchedAlarm`, route wiring
- `metering/tests/` — conventions and the trimmed real offer fixtures
- `docs/METERING.md` pricing sections
- `.kiro/specs/metering-pricing-single-source/{requirements,design,tasks}.md` —
  read these verbatim; the code cites their requirement numbers throughout
- `docs/plans/metering-admin-console/05-PRICING-SINGLE-SOURCE.md` — the governing
  prior-art document
- `docs/plans/metering-admin-console/02-PRICING-INVESTIGATION.md` (historical,
  partly disproven) and `03-` / `04-` (both marked SUPERSEDED — read them only to
  avoid repeating their dead ends)

### External reference implementation — study this closely

**`https://github.com/aws-samples/sample-bedrock-spend-budget-guardrails`** (AWS
Samples; TypeScript/CDK; near-real-time Bedrock spend metering per IAM principal
with IAM-Deny budget enforcement). It solves the *same pricing problem* against the
*same AWS Price List* and has clearly gone further on it than we have. Mine it for
patterns and for corroboration or refutation of our own findings.

Start with these, via `gh api` or by cloning it to a scratch directory:

- **`docs/pricing-nuances.md`** — the single highest-value document. Live-probed
  facts about the three Bedrock service codes, their differing schemas and unit
  conventions, the `servicename` join, usagetype naming conventions, and named
  failure modes.
- **`lambda/src/pricing-refresher/`** — `usagetype.ts` (`skuPrecedence()`),
  `index.ts` (`claim()`), `name-variants.ts`, `model-aliases.ts`.
- **`docs/cur-reconciliation.md`** — cross-checking the meter against CUR 2.0
  billing data. We have a Bedrock-vs-ledger reconciler but no billing-truth check.
- **`scripts/test-pricing-refresher.ts`** — a read-only dry-run harness
  (`--model <id>` / `--all`) that reports per-model join outcomes without writing.
- **`scripts/probe-pricing-sdk.ts`**, **`docs/operator-config.md`**,
  **`scripts/docs-check.mjs`**.

Patterns I suspect are worth adopting — evaluate each on merit, do not import
wholesale:

1. **Deterministic SKU tier precedence.** They found Gemma 3 27B in us-east-1
   exposes **8 competing `input` SKUs** across batch/flex/standard/priority with a
   **3.3x spread**, so without explicit precedence the winner is decided by
   response order. Their `skuPrecedence()` + `claim()` makes the best-tier SKU win
   regardless of order. **Our refresher's `_merge_rate` uses bare
   `cell.setdefault(...)` — "first-wins by offer-file order."** That is the exact
   fragility they engineered away. This is probably our most valuable single
   adoption.
2. **Global routing as a genuinely different rate, not a fallback.** They verified
   against CUR that Anthropic frontier bills Global ~9% *below* regional (Opus 5
   input $0.005 vs $0.0055 per 1K) and route those SKUs into a separate bucket
   selected by the invoked model's `global.` prefix. Compare against our
   `geo → in_region` mapping and routing grid, and confirm ours bills correctly for
   how this gateway actually invokes.
3. **`GetProducts` Query API as a targeted fallback** when the bulk offer files
   leave a model unpriced, with a distinguishing metric
   (`PricingQueryFallbackUsed`). We read only the bulk files. Note their warning:
   `GetProducts` is low-TPS and throttles hard — they demoted it to fallback
   precisely because a fan-out across models × regions × candidate names hit the
   15-minute Lambda cap and *silently truncated the same tail models every run*.
   Adopt it as a narrow gap-closer, never as the primary path.
4. **A read-only per-model diagnostic harness** so "why is this model unpriced" is
   answerable in one command without deploying.
5. **A full before/after rate diff as the pre-deploy gate.** They are explicit that
   broadening a matcher trades visible, alarmed gaps for silently-wrong prices
   (their example: `Mistral Large 3` mis-joining to legacy `Mistral Large`, an 8x
   pricier SKU) — so the gate is a rate diff, not a gap count. This is the same
   hazard our no-fuzzy-matching rule exists to prevent, and their verification gate
   is compatible with our rule even though their normalizer is not.
6. **Non-token dimensions.** They meter images, video seconds, audio seconds, and
   search units, and they document a real class of *correctly* unpriceable SKUs —
   ones where no counter exists to multiply (`nova-reel` prices per generated
   video, `nova-grounding` per Request). We meter tokens only. Use this to reason
   about which of our gateway models are token-billed at all, since a non-token
   model may need excluding from the lane rather than pricing.

**Two important caveats before you borrow anything:**

- **Their metering scope does not transfer.** They explicitly state coverage
  depends on the *endpoint*, not the model: they meter `bedrock-runtime` /
  `bedrock-agent-runtime`, and **the same model is covered on `bedrock-runtime` and
  not covered on `bedrock-mantle`**. This repo routes through the AgentCore gateway
  to **`bedrock-mantle`**. So their enforcement and usage-capture design is
  inapplicable here. Their *pricing* work does transfer, because the Price List is
  endpoint-independent.
- **They are TypeScript; our pricing package is stdlib-only Python** copied into
  each Lambda by `stageWithPricing()`. Port ideas, not code, and do not add a
  dependency to the pricing package.

Their `MODEL_ALIASES` / `USAGETYPE_PREFIX` maps are described in their own docs as
hand-verified maintenance debt where an AWS rename silently reintroduces a gap. Our
locked-in constraints push the other way — toward exact AWS-authoritative joins with
alias binding as an operator action. Prefer our direction; borrow their rigor.

## Constraints — locked-in decisions, do not re-litigate

These were decided with measured evidence. Reversing one requires stopping and
telling me why, with data.

**Scope note: these are *architectural decisions*, not *measurements*.** The
decisions below are binding. Any number, coverage figure, or claim about what AWS
does or does not publish is **not** binding and must be re-derived — see "Treat
every prior coverage claim as an unverified hypothesis" below. Do not confuse the
two: honoring the architecture does not mean inheriting the arithmetic.

- **Two rate sources only:** operator `OVERRIDE` → `aws-published` → `unpriced`.
  Do not reintroduce a provider price list (LiteLLM), seeded default estimates, or
  a bundled price snapshot. `03-` and `04-` are superseded: hand-seeded rates were
  measurably wrong (Opus 4.7 seeded $15/$75 vs published $5.50/$27.50, +172.7%),
  and a provider list cannot express AWS's routing/tier/context axes (LiteLLM's
  Sonnet 5 rate matched AWS *global*, not the *in-region* rate this deployment
  bills under — a 10% under-bill).
- **No fuzzy name matching.** This is the hardest prohibition. A normalizing slug
  join silently mapped `claude-opus-4-7` → Opus 4.6 and `claude-sonnet-5` →
  Sonnet 4. Both looked plausible; both were wrong dollars. Alias expansion is
  safe in exactly one direction: expand the *catalog* side into candidate keys and
  require an *exact* match on the id being priced. If you improve matching, it must
  be through exact, deterministic, AWS-authoritative joins.
- **One store:** DynamoDB `PRICING#` rows are the only runtime price source. Do not
  add a JSON price file, and do not ship rates inside a deployment artifact.
- **Catalog keyed by Bedrock model id** — the same id the settle path resolves.
- **`unmatched` stays a visible work queue**, never a silent fallthrough.
- **Ambiguity never guesses.** Zero or multiple candidates means unresolved.
- **Operator `OVERRIDE` and `_ALIAS` rows are never auto-modified or GC'd.**
- Preserve MIT-0 headers. Keep the unmodified upstream Open WebUI image invariant —
  no fork, no patch, no image build.
- Keep metering optional: with metering off, the five base stacks must still synth
  and deploy unchanged.

## Treat every prior coverage claim as an unverified hypothesis

This is the most important instruction in this brief.

`05-PRICING-SINGLE-SOURCE.md` asserts 60% coverage (28/47) and **"8 models present
in Bedrock with no Price List token SKU at all."** Do **not** carry that forward as
fact. Re-derive coverage from live data before you accept any claim about what is
or is not priceable.

There are two independent reasons to distrust it:

1. **This codebase's own history.** `02-PRICING-INVESTIGATION.md` asserted that
   frontier Claude ids were "genuinely unpublished" by AWS. That was measured and
   **disproven** — every rate it claimed was missing turned out to be published in
   `AmazonBedrockFoundationModels`, a file the refresher was not reading. The "8
   genuinely unpriced models" claim is the *same class of claim from the same
   lineage*, and it has not been re-measured since.
2. **An independent implementation measured the opposite.** The reference repo
   below replayed its matcher against every model it serves (131 unique across 3
   regions) and found **37 gaps — all name-join failures, none a genuine unpriced
   model.** Its conclusion, verbatim in intent: a non-zero pricing-gap count is
   almost always a join bug on our side, not AWS lagging on pricing.

**Default assumption: an unpriced model is a join bug until you prove otherwise.**
"Genuinely unpriceable" is a conclusion you must earn with evidence — a specific
demonstration that no SKU exists in the Price List for that model in this region —
not an inherited premise.

That said, there is exactly one gap class with independent corroboration, and it
happens to be the family I flagged. The reference repo reports that proprietary
**GPT-5.x frontier models served on Bedrock's Mantle engine are commercially
available and priced on the Bedrock pricing website, but their SKUs are not
surfaced through the Price List API or the bulk offer files** (as of 2026-08, some
commercial-region SKUs absent entirely, others present only as `us-gov-*`). This
repo routes **through mantle**, so if that holds, `openai.gpt-5.6` may be a genuine
AWS publishing gap requiring an operator override rather than a matching fix. Treat
that as a strong lead to verify, not a conclusion to adopt. Check the live offer
files yourself and report what you actually find.

However each unpriced model resolves, the outcome must be one of:

- **priced** from AWS-published data via a correct exact join today's code misses, or
- **removed from the gateway lanes** so it cannot be invoked unpriced (tell me
  which models and why), or
- **carrying an operator `OVERRIDE`**, escalated to me as a decision with the rate
  you propose and its provenance — do not invent a rate.

Silently leaving a model unpriced is not acceptable. Neither is guessing a rate to
drive a number to zero, nor loosening the matcher until the gap count falls.

## Documentation is a first-class deliverable

Comprehensively validate and refactor **all** documentation across the project so it
reflects the work you performed. Documentation drift in this repo is not cosmetic:
the pricing docs are the operator's only description of how money is computed, and
stale claims in them are exactly how the false "genuinely unpublished" premise
survived long enough to drive two superseded designs.

Do all of the following:

1. **Audit every doc for claims your work invalidated.** Sweep the whole tree, not
   just the files you touched: `README.md`, `docs/METERING.md`,
   `docs/AWS_DEPLOYMENT_GUIDE.md`, `docs/GATEWAY_INTEGRATION_GUIDE.md`,
   `docs/UPGRADE_RUNBOOK.md`, `docs/COST_ANALYSIS_20K_USERS.md`, `infra/README.md`,
   `console/README.md`, `pipe/README.md`, `THIRD-PARTY-LICENSES.md`, everything
   under `docs/plans/` and `docs/reviews/`, the `.kiro/steering/*.md` files, and the
   `.kiro/specs/metering-pricing-single-source/` spec.
2. **Correct stale factual claims, and say so.** Where a doc states a coverage
   number, a model count, a "genuinely unpriced" assertion, or a precedence chain
   that your measurements contradict, fix it and note what changed. Where a prior
   analysis doc is now wrong in its conclusions, mark it superseded in the same
   style as `03-` and `04-` rather than silently editing history.
3. **Reconcile the spec.** The code cites requirement numbers from
   `.kiro/specs/metering-pricing-single-source/` throughout. If your work changes
   behavior those requirements describe, update the spec — do not leave code citing
   requirements that no longer say what the code does.
4. **Update the steering files** (`.kiro/steering/product.md`, `tech.md`,
   `structure.md`) if you add a component, a config surface, a script, or change an
   architectural boundary. These are loaded into every future session, so drift here
   compounds.
5. **Document the new pricing model end to end** for an operator: where rates come
   from, how a gateway model gets bound to a price, what the coverage signal means,
   what to do when it alarms, and how to resolve a model that cannot be priced.
6. **Write down what you deliberately did not do**, and why — especially any
   external pattern you evaluated and rejected, so the next run does not relitigate
   it. This is how `03-`/`04-` earn their keep; add to that record.
7. **Consider a doc-drift guard.** The reference repo ships
   `scripts/docs-check.mjs`: an advisory pre-commit/CI check that flags when watched
   code areas change without a corresponding docs change (exit 0 by default,
   strict mode available). If you judge it worth having here, build the equivalent
   and wire it in. Use your judgement — do not add ceremony this project will not
   maintain.

Verify docs the way you verify code: every number in a doc you touch should be one
you produced from a tool result in this session, not one you inherited.

## Environment and permissions

- AWS profile `test-web-app`, account `511884928131`, region `us-east-1`. Session
  is valid for ~12 hours; if it expires, stop and say so rather than leaving a
  deploy half-applied.
- This is a **dev/test environment with disposable data.** You may deploy, run the
  pricing refresher, invoke models, and write DynamoDB pricing rows freely. Aurora
  data is disposable.
- Live app: `https://dajaqxu4gb1m.cloudfront.net`. Admin API and console are the
  metering operator surface.
- **Deploy with the correct context or you will destroy live resources:**
  `ENABLE_MODEL_REFRESH=true ./deploy.sh --metering --profile test-web-app`
  (`.env` now sets `ENABLE_MODEL_REFRESH=true`, `METERING=on`, and
  `AWS_DEPLOY_PROFILE=test-web-app`, so `./deploy.sh` alone also works — but
  verify before running). A bare deploy without these deletes the 8 ModelRefresher
  resources and silently reverts the gateway to the non-enforcing v1 interceptor.
- `deploy.sh` now prefers the CDK CLI pinned in `infra/devDependencies`. Do not
  reintroduce a global-`cdk` preference: `aws-cdk-lib` 2.266.0 emits cloud-assembly
  schema 54.0.0, which needs CLI ≥ 2.1138.0.
- Bare `cdk diff` is misleading in this repo: without the image context it shows a
  spurious ECS task-definition replacement (digest → `DEFAULT_IMAGE` tag), and
  without `-c enableModelRefresh=true -c metering=on` it shows spurious deletions.
  Always diff with faithful context.
- Validation baseline: `python -m pytest metering/tests -q`; from `infra/`,
  `npx tsc --noEmit` and `npx cdk synth --quiet`; from `console/`, `npm run build`.
- Git: branch from `main`, which is current. The Aurora 17.7 upgrade, the
  `aws-cdk-lib` 2.266.0 / CLI 2.1138.0 bump, and the `deploy.sh` CDK-CLI-pin fix all
  landed in merge commit `4195ec5`. The live environment is already running them, so
  the deployed Aurora engine is 17.7 and the pinned CLI is what `deploy.sh` uses.

## Before executing

1. Inspect the sources. Confirm or refute the missing-join claim yourself.
2. Restate the problem you believe you are solving, in your own words.
3. Measure the current state before changing it: how many models the gateway
   serves per lane, how many resolve a price, exactly which do not and why, and
   what the unmatched entries actually are. This baseline is your before/after
   evidence — capture it as data, not prose.
4. Identify missing context, conflicting instructions, and assumptions that could
   change the result. Say plainly which of my framings above you think are wrong.
5. Decide whether this needs a dynamic workflow, a loop, subagents, or a simpler
   direct change. Do not build orchestration this job does not need.
6. Show me the proposed approach and the evidence you will use to verify it.

Then proceed without waiting for me.

## Suggested workflow

Use dynamic workflows, parallel subagents, loops, and verification where they fit.
Delegate independent work and keep going while it runs. Re-plan when evidence
invalidates the path.

- **Phase 0 — Study the reference implementation.** Read the external repo's
  pricing docs and refresher source before designing anything, in parallel with
  Phase 1. Produce a short written verdict: which of its patterns apply here, which
  do not and why, and which of its live-probed facts contradict our docs.
  Completion test: a pattern-by-pattern adopt/reject list with reasons.
- **Phase 1 — Ground truth, measured fresh.** Enumerate gateway-available models via
  `probe_core.fetch_catalog()` and the served `MODEL_CAPS`. Enumerate priced models
  from `PRICING#`. Join them. Independently re-derive what the live offer files
  actually contain for every gateway model — do not inherit any coverage number from
  our docs. Produce the coverage table and the exact unpriced list with a per-model,
  evidence-backed reason. Completion test: you can name every unpriced gateway model,
  say why, and point at the offer-file evidence for each.
- **Phase 2 — Diagnose.** Take gpt-5.6 and every member of the residue to root
  cause. Classify each as join defect, parser defect, publishing gap, or non-token
  billing dimension. Prove each failure mode with a test against real offer-file
  fixtures. Completion test: a failing test per distinct root cause, and zero models
  left in an "unknown reason" bucket.
- **Phase 3 — Design.** Decide the join's home (refresher? a new coverage
  function? the admin API?), its schedule, its persistence, and its alarm. Decide
  which simplifications are safe and which reference patterns you are adopting.
  Write the design down before building; if it changes a locked-in decision, stop
  and ask.
- **Phase 4 — Build.** Implement the join, the parser/resolver simplifications, the
  visibility signal for dropped rates, and the console surface. Keep each change
  independently testable and reversible.
- **Phase 5 — Adversarial review.** Use a fresh subagent that did not write the
  code to hunt for: rates that changed value unintentionally, a model now priced
  from the wrong routing/tier, a join that matches more by being less exact
  (the prohibited failure mode), and dead code left behind. Diff resolved rates
  per model before vs after and explain every delta.
- **Phase 6 — Deploy and validate live.** Deploy, run the refresher, and verify
  against the live catalog and console. Exercise the real admin API. Confirm the
  coverage metric reports zero unpriced gateway models.
- **Phase 7 — Documentation.** Execute the documentation deliverable above as its
  own phase with its own completion test: no doc in the tree makes a factual claim
  about pricing that your measurements contradict. Consider dispatching a fresh
  subagent that did not write the code to audit the docs against the implementation,
  since the author is the worst reviewer of their own documentation.
- **Phase 8 — Compound.** Save the durable lessons. Record the rejected reference
  patterns and any prior claim you disproved, so the next run inherits the
  correction rather than the original error.

## Autonomy and escalation

Work to completion. If you hit a blocker, do not stop: use documented assumptions,
stubs, or a narrower scope, record the workaround, and continue with everything
that does not require my decision.

Pause only for:
- a destructive or irreversible action beyond the disposable test environment,
- a change to one of the locked-in decisions above,
- inventing a dollar rate not traceable to AWS-published data or an explicit
  operator override,
- removing a model from the gateway lanes (tell me which and why — I may accept it,
  but I want to know),
- information only I can provide.

Do not stop at analysis or recommendations. You have the tools and permission to
ship this.

## Evidence required

Before reporting progress or completion, audit every claim against a tool result
from this session. If something is not verified, say so plainly. Specifically:

- The before/after coverage table, generated by running code, not asserted.
- Test output for the new and existing suites.
- `tsc --noEmit`, `cdk synth`, and console build results.
- Live verification: the deployed refresher's own output, the live admin API
  response showing zero unpriced gateway models, and the console rendering it.
- A per-model rate diff for every model whose resolved price changed, with the
  reason and the AWS provenance for the new value. Per the reference repo's own
  hard-won lesson, this diff — not a gap count — is the gate that catches a matcher
  that started matching *more* by being *less* exact.
- A quantified simplification claim.
- For every prior claim you inherited and tested: whether it held, and the evidence.
  Explicitly state what you found regarding the "8 genuinely unpriced models" claim
  and the GPT-5.x Price List publishing question.
- The documentation audit: which docs you changed, which stale claims you corrected,
  which docs you marked superseded, and confirmation that no remaining doc
  contradicts the implementation.

## When finished, return

1. The outcome, in one sentence.
2. What you completed and the most important decisions you made.
3. Evidence that the result works.
4. Anything you could not verify.
5. What should be saved or improved so the next run is better.
```
