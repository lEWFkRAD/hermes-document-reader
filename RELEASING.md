# Releasing Hermes Document Reader

Releases publish two archives from one reviewed Git tree:

- `hermes-document-reader-source-vX.Y.Z.zip` contains the complete reviewed,
  tracked source tree beneath a versioned top-level directory. Git metadata,
  ignored files, caches, and local state are intentionally absent.
- `hermes-document-reader-vX.Y.Z.zip` contains the complete installable Hermes
  plugin at the archive root. Its explicit allowlist includes the manifest,
  Python entry point, profile runtime, dashboard, desktop surface, service,
  OCR engine, installer, both hashed Windows service locks, license, and
  required install guidance. The MCP server, viewer, tests, repository
  governance, root source/development `requirements.txt`, and retired legacy
  autostart path remain source-only. The installable service runtime is
  provisioned exclusively from `install/service-requirements.txt` and the
  selected hashed Windows lock; dashboard API imports are supplied by the
  Hermes host.

Each ZIP has a sibling `.sha256` file. GitHub records build-provenance
attestations for both ZIPs.

## Prepare

1. Start from current `main`, use a pull request, and build only from a clean
   committed tree. The archive builder rejects staged, unstaged, or unignored
   untracked files.
2. Set the same SemVer in `package.json`, both root entries in
   `package-lock.json`, `plugin.yaml`, `dashboard/manifest.json`, and the
   desktop plugin export, plus `profile_runtime.py::PLUGIN_VERSION` and
   `service/ocr_service.py::VERSION`. Keep
   `profile_runtime.py::SERVICE_API_VERSION` equal to
   `service/ocr_service.py::API_VERSION`.
3. Replace the matching `CHANGELOG.md` state `Unreleased` with the UTC release
   date (`YYYY-MM-DD`). Never rewrite an already released section.
4. Remove executable deployment placeholders and verify profile-aware defaults.
5. If a service dependency changes, regenerate both Windows locks with the
   reviewed resolver and real 64-bit Windows CPython interpreters:

   ```powershell
   .\scripts\regenerate-service-locks.ps1 `
     -Python311 C:\path\to\python311.exe `
     -Python314 C:\path\to\python314.exe
   ```

   `install/service-requirements.txt` is the runtime input.
   `scripts/lock-inputs/service-bootstrap.txt` explicitly accounts for the
   `pip` and `setuptools` distributions created or retained by `venv` and
   lifecycle bootstrap. The generator requires `uv 0.12.3`, targets
   `x86_64-pc-windows-msvc`, permits wheels only, and writes every transitive
   package with SHA-256 hashes. Review the complete lock diff; never hand-add
   an unexplained package.
6. Run:

   ```text
   npm ci --ignore-scripts
   npm run validate
   python -m pip install --requirement requirements.txt --requirement requirements-dev.txt
   python -m pytest tests -q --basetemp ../document-reader-pytest
   python -m compileall -q __init__.py cli.py dashboard engine engine_config.py install lifecycle.py mcp profile_runtime.py service tests viewer
   npm audit --omit=dev --audit-level=high
   python -m pip_audit --requirement requirements.txt
   python -m pip_audit --requirement requirements-dev.txt
   python -m pip_audit --requirement install/locks/windows-cpython-311-x86_64.txt --no-deps
   python -m pip_audit --requirement install/locks/windows-cpython-314-x86_64.txt --no-deps
   python -m pip_audit --local
   ```

7. Let hosted CI provision a fresh isolated venv on real Windows CPython 3.11
   and 3.14 for the matching lock. Each lane installs with `--require-hashes`
   and wheels-only resolution, records the pip JSON report, then verifies the
   runtime tags, selected wheel hashes, exact installed inventory, imports,
   `pip check`, and dependency audit. Each Windows lane also exercises the real
   lifecycle twice from a stable source snapshot, requires one reproducible
   release identity, proves hostile Python hooks do not execute, and rejects
   modified deterministic bytecode or a RECORD-hashed package file. This proves
   each target is installable and reusable; byte-identical lock bodies alone do
   not.
8. Let hosted CI build and smoke-test both archives. Merge only after
   `Required PR checks` succeeds and review conversations are resolved.

## Publish

1. Confirm the release commit is on `main` and the hosted main run is green.
2. Create an annotated `vX.Y.Z` tag on that exact commit and push only the tag.
3. The Release workflow verifies the tag is reachable from `origin/main`, the
   tag and version sources agree, and the annotated local and remote tag
   identities have not changed.
4. Before the publishing job can start, the workflow independently repeats
   the exact lock install/report/inventory/import/audit gate on real Windows
   CPython 3.11 and 3.14. The publishing job then repeats source validation and
   strict fully resolved environment audits without vulnerability ignores,
   builds both archives, verifies checksums and contents, attests both ZIPs,
   and re-checks the remote tag identity immediately before creating the
   release.
5. Inspect all four assets and both attestations before announcing the release.

Never release while the changelog says `Unreleased`, retag a published
version, replace existing assets, or publish from a side branch. Correct a bad
release with a new patch version.
