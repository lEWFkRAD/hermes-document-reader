# Contributing to Hermes Document Reader

Thank you for improving the Document Reader. It processes sensitive source
documents and connects a local Hermes Desktop surface to an OCR service, so
changes must preserve privacy, profile isolation, and fail-closed defaults.

## Development setup

Use Node.js 20 or 22 and Python 3.11 or 3.14. Create and activate a fresh
virtual environment before installing the Python requirements; the final
`pip-audit --local` command is meaningful only when unrelated packages are not
present.

```text
git clone https://github.com/lEWFkRAD/hermes-document-reader.git
cd hermes-document-reader
npm ci --ignore-scripts
python -m pip install --requirement requirements.txt --requirement requirements-dev.txt
```

Do not put client documents, OCR output, credentials, local hostnames, profile
directories, model endpoints, or logs in the repository or test fixtures.

The supported lifecycle and scheduled-task provisioning run only on 64-bit
Windows. Most source, security, and release-contract tests also run on Linux in
CI; passing those tests does not make service installation cross-platform.

## Validation

Run the same checks as CI before opening a pull request:

```text
npm run validate
python -m pytest tests -q --basetemp ../document-reader-pytest
python -m compileall -q __init__.py cli.py dashboard engine engine_config.py install lifecycle.py mcp profile_runtime.py service tests viewer
python -m pip_audit --requirement requirements.txt
python -m pip_audit --requirement requirements-dev.txt
python -m pip_audit --requirement install/locks/windows-cpython-311-x86_64.txt --no-deps
python -m pip_audit --requirement install/locks/windows-cpython-314-x86_64.txt --no-deps
python -m pip_audit --local
```

CI additionally installs each matching service lock into a fresh Windows
CPython 3.11 or 3.14 venv with `--require-hashes --only-binary=:all:`. It
rejects runtime-tag, selected-wheel-hash, pip-report, or installed-inventory
drift. Follow `RELEASING.md` when changing a service dependency; never edit a
transitive lock entry without regenerating and reviewing both targets.

Tests must use temporary directories, loopback listeners, and mocked OCR
responses. They must not contact a production OCR model, install a scheduled
task, or read a real Hermes profile.

## Pull requests

1. Branch from `main` and keep one logical change per pull request.
2. Add tests for behavior changes and document security-relevant decisions.
3. Use a descriptive Conventional Commit subject.
4. Certify each commit with `git commit -s` under the Developer Certificate of Origin.
5. Complete the pull request template and disclose material AI assistance.
6. Wait for `Required PR checks` and resolve every review conversation.

Contributions are licensed under the repository's MIT License. Release tags
and GitHub Releases are maintainer operations described in `RELEASING.md`.
