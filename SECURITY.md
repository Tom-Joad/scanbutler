# Security policy

## Supported versions

Only the latest release receives fixes. Security fixes go into a new
release of the current major version.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Report them
privately through GitHub instead: on the
[Security tab](https://github.com/Tom-Joad/scanbutler/security),
choose **Report a vulnerability**. You will get an answer within a few days.

## Scope notes

This tool handles personal documents and an API key, so the following are of
particular interest:

- document text, file names or the API key ending up in logs, the webhook
  payload or the image
- files left behind in Mistral's storage after a batch job
- path handling that could write or delete outside the configured folders,
  for example through a crafted `plan.json` or file name

The container runs as a non-root user and needs no inbound ports.

## Supply chain

Images are built by GitHub Actions from version tags only. Third-party
actions are pinned to commit SHAs. Dependencies are checked with `pip-audit`,
and the repository is scanned with `gitleaks`. Images are scanned with Trivy
and signed keylessly with cosign. To verify an image:

```bash
cosign verify ghcr.io/tom-joad/scanbutler:latest \
  --certificate-identity-regexp 'https://github.com/Tom-Joad/scanbutler/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
