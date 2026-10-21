# Changelog

All notable changes to Hermes Document Reader are recorded here. The format is
based on Keep a Changelog, and this project uses Semantic Versioning.

## [0.1.0] - Unreleased

## Recovery and extraction evidence

Completed OCR pages have atomic, digest-checked checkpoints inside the existing
retention-managed job cache. Retries reuse matching source bytes, profile, engine
settings, prompt and service revision, while regenerating previews. Changed or
corrupt checkpoints are ignored. Cache eviction means a later retry may OCR again.
Cancellation is checked again after an OCR response returns before saving it.

Processing reserves 512 MiB free space plus bounded anticipated output; low space
pauses work with the original document preserved. Retained processed files are
never automatically deleted to make room. Other applications can still consume
disk space after a check.

Excel exports append Sources and Review sheets after the existing tables. They
record source page numbers, OCR-output hashes, uneven rows and numeric-looking
text requiring inspection. Leading-zero identifiers, long integers and ambiguous
locale formats stay text. These flags are heuristics, not OCR confidence scores.

### Fixed

- Reclaim oldest completed jobs first under byte pressure while retaining the
  newest-first count policy and all directory ownership/identity checks.
- Make the Windows retention safety test independent of filesystem clock ticks.

### Added

- Windows Hermes plugin lifecycle commands for profile configuration,
  installation, ownership-aware status, interrupted-transaction recovery,
  rollback, and data-preserving uninstall.
- Independent service ports, tokens, limited current-user scheduled tasks,
  immutable runtimes, Desktop deployments, configuration, and data for every
  selected Hermes profile.
- Watched-folder and authenticated upload intake for PDF, PNG, JPEG, TIFF, and
  BMP documents, with live page previews, recognized text, bounded history,
  and downloadable `.txt` and `.xlsx` exports.
- Hermes Desktop sidebar and command-palette surfaces plus a Hermes 0.20 HUD
  attachment action that queues documents without inserting filenames into the
  chat composer.
- Verified, source-preserving legacy inbox migration into the selected
  profile's private data tree.
- Contributor CI, DCO enforcement, dependency updates, pull-request templates,
  release archive verification, SHA-256 checksum assets, and build-provenance
  attestations.

### Changed

- Migrated the source-only MCP document tools to the native MCP 2 server API
  and refreshed the coordinated runtime, service, and bootstrap dependency
  pins and hashed Windows locks.

### Security

- Loopback-only service configuration with token authentication, owner and
  profile attestation, strict Host/Origin policy, and no cross-profile or
  environment credential fallback.
- Fixed-handle and immutable-snapshot processing, path and archive containment,
  bounded upload/render/OCR/output budgets, fail-closed cancellation, safe
  disposition directories, and bounded preview/history retention.
- Sanitized OCR HTML, redacted public errors, and spreadsheet formula
  neutralization for exported text cells.
- Fully transitive, wheel-only Windows CPython 3.11 and 3.14 service locks with
  SHA-256 hashes, exact bootstrap inventory, pip-report verification, and
  release-blocking install and audit lanes.
- Immutable release-source capture, exact installed-environment attestation,
  isolated no-site service startup, and receipt-guarded update, rollback, and
  uninstall behavior.

### Source-only

- Guarded stdio MCP conversion tools and an unauthenticated loopback developer
  viewer remain available in the complete source tree. They are excluded from
  the installable plugin archive and are not provisioned by the supported
  lifecycle.

[0.1.0]: https://github.com/lEWFkRAD/hermes-document-reader/compare/923a8ed2334419e6419ff9abcceebe1e53bce4a5...HEAD
