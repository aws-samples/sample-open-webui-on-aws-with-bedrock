<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Screenshot capture specification

No authenticated product screenshot is committed yet. The deployed application
and metering console require Cognito sign-in, and documentation work must not
create a user, elevate a user, or expose a real identity merely to obtain an
image. An operator with an existing test administrator can capture the two
frames below in about ten minutes.

## Safety gate

1. Use only a non-production test deployment and an existing synthetic/test
   administrator. Do not create a Cognito user or change group membership for
   the capture.
2. Confirm the caller before any demo-data write:

   ```bash
   aws sts get-caller-identity --profile YOUR_TEST_PROFILE
   ```

3. Resolve the table and console URL from CloudFormation; do not copy account,
   API, pool, client, distribution, or resource identifiers into the image:

   ```bash
   TABLE=$(aws cloudformation describe-stacks \
     --stack-name OpenWebUI-Metering \
     --query "Stacks[0].Outputs[?OutputKey=='MeteringTableName'].OutputValue" \
     --output text --profile YOUR_TEST_PROFILE --region us-east-1)

   CONSOLE_URL=$(aws cloudformation describe-stacks \
     --stack-name OpenWebUI-Metering \
     --query "Stacks[0].Outputs[?OutputKey=='ConsoleUrl'].OutputValue" \
     --output text --profile YOUR_TEST_PROFILE --region us-east-1)
   ```

4. Seed once, capture, and clean up in the same session. The script creates a
   unique demo namespace, refuses to overwrite any row, and records each
   successful write incrementally. An existing manifest blocks a second run.
   If seeding fails after any writes, use that retained manifest with
   `--cleanup` before retrying.

   ```bash
   python3 scripts/seed-demo-metering-data.py \
     --table "$TABLE" --profile YOUR_TEST_PROFILE --region us-east-1 \
     --manifest local_only/docs-screenshot-demo-manifest.json
   ```

## Frame 1 — consumption dashboard

- **Destination:** `docs/images/metering-console-dashboard.png`
- **URL:** `$CONSOLE_URL/`
- **Viewport:** 1440 × 900 CSS pixels, device scale factor 1, browser chrome
  excluded.
- **Theme:** Light.
- **State:** Signed in as an existing test administrator; **Dashboard** selected;
  current UTC month; wait for all KPI cards and charts to finish loading.
- **Include:** top navigation, enforcement-mode indicator, side navigation,
  summary KPIs, spend/call charts, and any near-limit view visible without
  scrolling.
- **Exclude or redact:** the account menu email, all real names/emails, URL bar,
  browser profile, notifications, account IDs, hostnames, API IDs, pool/client
  IDs, ARNs, and chat content. Replace the account-menu label with
  `admin.demo@example.com` if it cannot be cropped.
- **Alt text:** “Cloudscape metering dashboard with synthetic monthly spend,
  request, quota, and module-health summaries.”

## Frame 2 — pricing coverage

- **Destination:** `docs/images/metering-console-pricing.png`
- **URL:** `$CONSOLE_URL/pricing`
- **Viewport:** 1440 × 900 CSS pixels, device scale factor 1, browser chrome
  excluded.
- **Theme:** Light.
- **State:** **Model pricing** selected; wait for the coverage strip and pricing
  table to load; leave filters at their defaults; do not open an edit modal.
- **Include:** coverage status, pricing source badges, gateway availability/lane
  indicators, and the first complete rows that fit in the viewport.
- **Exclude or redact:** the same topology and identity fields as Frame 1. Model
  IDs, rate provenance labels, and synthetic/demo values are acceptable; do not
  expose operator notes that contain internal URLs or ticket references.
- **Alt text:** “Cloudscape model-pricing page showing gateway coverage and
  whether each available model has an AWS-published or operator-supplied rate.”

## Cleanup and verification

Run cleanup even if capture fails. Reconfirm the same test-account caller first;
the script also refuses cleanup unless the selected table ARN matches the seed
manifest:

```bash
aws sts get-caller-identity --profile YOUR_TEST_PROFILE
python3 scripts/seed-demo-metering-data.py \
  --table "$TABLE" --profile YOUR_TEST_PROFILE --region us-east-1 \
  --manifest local_only/docs-screenshot-demo-manifest.json --cleanup
```

Verify the manifest is gone and manually inspect every pixel at 100% zoom before
committing. Search OCR text for the account ID, `cloudfront.net`,
`execute-api`, `cognito`, `arn:aws`, and `@amazon.com`. Compress each PNG and
keep it below 500 KiB. Do not add either image to Markdown until both the file
and its alt text pass `node scripts/docs-integrity.mjs`.

A later screenshot remains a factual view of the separately licensed Open WebUI
integration and AWS-authored console. Preserve any Open WebUI branding visible
in its own interface, do not combine it into a project logo, and keep the
third-party attribution in `NOTICE` and `THIRD-PARTY-LICENSES.md` unchanged.
