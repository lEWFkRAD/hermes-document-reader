# Repository agent guidance

## Scope and privacy

This repository processes confidential documents. Never add real documents,
OCR output, credentials, internal hostnames, user paths, profile state, or logs.
Use temporary directories, loopback listeners, and synthetic fixtures only.

Do not call a live OCR endpoint or mutate Windows scheduled tasks during tests.
Keep profile resolution explicit and fail closed when profile or endpoint
ownership is ambiguous.

## Required validation

Run before handing off a change:

```text
npm ci --ignore-scripts
npm run validate
python -m pip install --requirement requirements.txt --requirement requirements-dev.txt
python -m pytest tests -q --basetemp ../document-reader-pytest
python -m compileall -q __init__.py cli.py dashboard engine engine_config.py install lifecycle.py mcp profile_runtime.py service tests viewer
python -m pip_audit --requirement requirements.txt
python -m pip_audit --requirement requirements-dev.txt
python -m pip_audit --local
```

Release work must also run `npm run build:release` and
`npm run smoke:release` from a clean committed tree.

## Contribution and release rules

- Sign off every new commit under the DCO.
- Keep every version source synchronized.
- Keep the current changelog entry `Unreleased` until a dedicated release PR.
- Do not create, move, or delete tags or publish GitHub Releases from feature work.
- Do not weaken archive hygiene, checksum, tag-identity, or aggregate CI gates.
- Do not use `pull_request_target` or repository secrets for contributor code.
