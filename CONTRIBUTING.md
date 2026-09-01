# Contributing guidelines

Thank you for contributing fixes, documentation, tests, or focused features.
Open WebUI itself is a separate upstream project: this repository deploys its
official image but does not vendor, fork, or patch it. Send upstream application
changes to the [Open WebUI repository](https://github.com/open-webui/open-webui).

## Before opening work

- Search existing open and recently closed issues and pull requests.
- Open an issue before a significant architecture, dependency, security, or
  behavior change so scope and ownership are clear.
- Work from current `main` and keep the pull request focused. Avoid unrelated
  formatting or generated-file churn.
- Never commit `.env`, credentials, tokens, deployment identifiers, customer
  data, real user details, or private screenshots.

GitHub documents how to [fork a repository](https://docs.github.com/en/get-started/quickstart/fork-a-repo)
and [create a pull request](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request).

## Validate the affected area

Run the smallest relevant checks during development and the complete affected
set before opening a pull request.

### Documentation and assets

```bash
node scripts/docs-integrity.mjs
node scripts/docs-check.mjs
```

If diagram source changes, regenerate and recheck it:

```bash
node scripts/render-doc-diagrams.mjs
node scripts/docs-integrity.mjs
```

Inspect light and dark output, provide meaningful alt text, keep assets small,
and follow [`docs/images/SCREENSHOT-SPEC.md`](docs/images/SCREENSHOT-SPEC.md)
for any authenticated UI capture.

### Metering Python

```bash
python3 -m pytest metering/tests -q
```

### CDK infrastructure

```bash
cd infra
npm install
npm run build
npx tsc --noEmit
npx cdk synth --quiet
```

Run `npx cdk diff` only with an account/profile you have independently verified.
Do not deploy resources merely to make a contribution check pass.

### Metering console

```bash
cd console
npm install
npm run build
```

### Deployment script

```bash
bash -n deploy.sh
```

## Pull-request expectations

- Explain the user/operator value and any compatibility or migration impact.
- Include tests for behavior changes where a practical seam exists.
- Update the guide that owns changed behavior rather than duplicating prose.
- Preserve the metering-off default and unmodified-upstream-image boundary.
- Preserve copyright and SPDX headers on authored source files.
- Use inclusive language and accessible images/tables.
- Respond to the repository's read-only documentation workflow and reviewer
  findings without bypassing checks.

## Code of conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
See the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or
contact opensource-codeofconduct@amazon.com with questions.

## Security issues

Do not report a potential vulnerability in a public GitHub issue. Follow
[`SECURITY.md`](SECURITY.md).

## Licensing

Contributions are accepted under the repository's [MIT-0 license](LICENSE).
Confirm that new dependencies/assets can be redistributed and update
[`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md) when required.
