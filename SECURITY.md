# Security Policy

Hermes Document Reader handles private documents, filenames, OCR output, local
filesystem paths, model endpoints, and profile-scoped credentials. Report
security problems privately.

## Supported versions

Version `0.1.0` is currently Unreleased. Until a dated, published GitHub Release
exists, security fixes land on `main`; no published version is currently
supported.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/lEWFkRAD/hermes-document-reader/security/advisories/new).
If that form is unavailable, contact the maintainer privately before
publishing details. Do not open a public issue containing documents,
credentials, hostnames, private paths, exploit steps, or OCR output.

Useful private reports include the affected version or commit, a minimal
sanitized reproduction, impact, and a suggested mitigation. Maintainers should
acknowledge, reproduce, and patch reports privately before publishing upgrade
guidance.

## Data flow

The supported background service and its Desktop/Dashboard bridge communicate
over loopback. The service renders source documents locally, then sends page
images and OCR instructions to the OpenAI-compatible `api_base` configured for
the selected profile. That endpoint may be local or remote. Operators are
responsible for choosing an endpoint whose transport, retention, access, and
model terms meet their document-handling requirements.

The HUD attachment action uploads selected files to the profile service and
inserts only a queued-file count into the chat composer. Filenames and OCR
output remain visible in the authenticated local reader and on the selected
profile's filesystem.

## Deployment boundaries

- Treat uploaded documents, filenames, embedded content, archives, page
  images, and OCR model output as hostile input.
- The supported lifecycle binds the service only to `127.0.0.1`. Do not
  rebind, expose, or reverse-proxy it to a LAN or the public internet.
- Keep the selected profile's state beneath
  `<selected HERMES_HOME>\document-reader`. This is separate from both the Git
  checkout and the Git-installed source at
  `<selected HERMES_HOME>\plugins\document-reader`.
- Use separate profile-scoped state. Never copy service tokens, engine tokens,
  service URLs, inboxes, history, or deployment receipts between profiles, and
  never silently fall back to another profile.
- Preserve path-containment, fixed-handle identity, upload-size, content-type,
  origin, rendering-budget, output-sanitization, retention, and quarantine
  checks.
- The lifecycle registers one exact current-user, interactive-logon, limited
  Windows task for each installed profile. The action uses the release-owned
  interpreter and entry point in isolated, no-site mode. A SYSTEM task or the
  retired `service/install-autostart.ps1` path is unsupported.
- Profile service and engine tokens are private files. The supported service
  does not accept those credentials from global environment variables or task
  arguments.

## Source-only surfaces

The MCP server and single-run viewer are present only in the source tree and
source archive; they are excluded from the installable plugin archive.

- MCP inputs must be absolute regular files beneath the selected profile's
  approved inbox or processed roots. Environment configuration can narrow
  those roots but cannot expand them. Remote MCP OCR is disabled by default and
  requires explicit selected-profile consent bound to the configured endpoint.
- The developer viewer is an unauthenticated loopback diagnostic. Do not treat
  it as a production service or use it as a substitute for the authenticated
  profile lifecycle.

For a published release, verify each archive's adjacent SHA-256 file and its
GitHub build-provenance attestation before use. Release archives are
distribution artifacts, not a security boundary.
