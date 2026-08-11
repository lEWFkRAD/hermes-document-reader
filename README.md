# Hermes Document Reader

Hermes Document Reader is a Windows-only Hermes plugin for profile-scoped
watched-folder OCR. It adds a Hermes Desktop reader, a HUD attachment action,
and an owned background service for each selected Hermes profile.

The service listens only on loopback. Documents, previews, exports, and history
stay under the selected profile, but page images are sent to the
OpenAI-compatible OCR endpoint configured for that profile. Choose and secure
that endpoint according to your document-handling requirements.

> **Status:** `0.1.0` is Unreleased. No versioned GitHub Release has been
> published yet. The installation commands below install the current `main`
> branch for testing.

## Requirements

- Hermes Agent 0.20.0 or newer
- 64-bit Windows
- Hermes running on 64-bit CPython 3.11 or 3.14
- Git, for `hermes plugins install` and `hermes plugins update`
- An OpenAI-compatible multimodal OCR endpoint, model identifier, and API token

HTTPS is required for the OCR endpoint unless you deliberately pass
`--allow-insecure-http`. Do not use plaintext HTTP for a non-loopback endpoint.

## Install for a profile

Run every command with the same profile selector. This example installs the
plugin for a profile named `work`:

```powershell
hermes -p work plugins install lEWFkRAD/hermes-document-reader --enable
hermes -p work document-reader configure --api-base https://ocr.example.com/v1 --model model-id
hermes -p work document-reader install
hermes -p work document-reader status
hermes -p work gateway restart
```

The `configure` command prompts for the API token without placing it in shell
history or process arguments. Omit `-p work` for the default profile. Repeat
the complete sequence for every additional profile that should run Document
Reader.

After the first lifecycle installation, enable the Document Reader entry in
Hermes Desktop if it is disabled, then reload Desktop plugins. Do not run
`service/install-autostart.ps1`, `service/ocr_service.py`, or a hand-written
scheduled task; those paths bypass the supported ownership checks.

## Use Document Reader

- Open **Document Reader** from the Desktop sidebar or command palette.
- Drag files onto the reader or choose **Scan documents**.
- In the HUD composer, choose **Scan with Document Reader**. The action queues
  the selected files to the current profile and inserts only a count marker,
  such as `[Document Reader queued 2 files]`, into the composer. It does not
  attach document names or source files to the chat message.
- To use the watched folder, place documents in
  `<selected HERMES_HOME>\document-reader\data\inbox`.

The supported service inputs are PDF, PNG, JPEG, TIFF, and BMP. Each document
must be between 1 byte and 100 MiB. Desktop and HUD intake accept at most ten
files per action.

The reader shows live page previews and recognized text. A completed export
includes a readable `.txt` file and, when workbook generation succeeds, an
`.xlsx` workbook with a Text sheet plus one sheet per unique nontrivial table.
Duplicate table copies are omitted.

## Profile ownership and storage

Hermes resolves the selected `HERMES_HOME` at command and request time. That
path is already the profile root; do not append another `profiles\<name>`
segment. Each profile receives an independent loopback port, service token,
engine token, limited current-user scheduled task, immutable runtime, Desktop
receipt, and data tree.

Runtime state lives beneath `<selected HERMES_HOME>\document-reader`:

| Path | Purpose |
| --- | --- |
| `config` | Private service and OCR endpoint configuration and tokens |
| `runtime` | Owned immutable service releases and runtime lock |
| `install` | Deployment receipts, lifecycle journal, and rollback backups |
| `data\inbox` | Watched input documents |
| `data\processed` | Completed exports and successfully processed source documents |
| `data\on-hold` | Source documents from cancelled jobs |
| `data\needs-review` | Source documents with page-level OCR failures |
| `data\quarantine` | Repeated failures or documents that fail integrity checks |
| `data\jobs` | Bounded preview and download cache |
| `data\state` | Bounded job history |
| `data\logs` | Local service log |

The preview/download cache is bounded by age, count, and total size. Completed
files in `data\processed` are not part of that cache-retention cleanup.

## Update, recover, rollback, and uninstall

Apply a plugin source update to one profile with:

```powershell
hermes -p work plugins update document-reader
hermes -p work document-reader install
hermes -p work document-reader status
```

Rerun `document-reader install` after changing the endpoint, model, token, or
CA bundle so the service restarts under the new configuration.

- `document-reader status` verifies deployment receipts, the exact scheduled
  task action, runtime ownership, and authenticated service health.
- `document-reader recover` completes or rolls back an interrupted lifecycle
  transaction. Use the same profile selector that reported the interruption.
- `document-reader rollback` restores the immediately previous owned
  deployment when one is available.
- `document-reader uninstall` removes the owned scheduled task and Desktop
  deployment while preserving configuration, releases, logs, documents, and
  history. It does not remove the Git-installed plugin source.

After lifecycle uninstall, use
`hermes -p work plugins remove document-reader` only if the profile should also
lose the installed plugin source. The retained
`<selected HERMES_HOME>\document-reader` state is intentionally not deleted.

### Copy a legacy inbox

To migrate an older inbox without deleting or moving the source tree:

```powershell
hermes -p work document-reader install --legacy-inbox "C:\path\to\old-inbox"
```

The lifecycle copies and verifies supported legacy documents before publishing
them into the selected profile. Legacy input documents use the same 100 MiB
per-file processing limit as new service inputs. Existing legacy processed
outputs are copied to the new processed tree. The source tree remains intact.

## Security and privacy

- The supported service binds to `127.0.0.1`; it is not a LAN or public web
  service.
- Every service request requires the selected profile's private token, and API
  and job requests also require the expected owner identity.
- Profile service and engine tokens are private files. Supported lifecycle
  tasks do not receive credentials through environment variables or command
  arguments.
- Filenames and OCR output are visible in the authenticated local reader and
  stored in the selected profile. HUD composer text contains only the queued
  file count.
- OCR page images are transmitted to the configured `api_base`. A local
  loopback service does not imply that the OCR endpoint is local.

See [SECURITY.md](SECURITY.md) for reporting and deployment boundaries.

## Source-only developer tools

The source tree contains two additional tools that are intentionally absent
from the installable plugin archive and are not registered or provisioned by
`document-reader install`:

- `mcp/anydoc-mcp.py` provides guarded stdio MCP conversion tools. It accepts
  absolute regular files only beneath the selected profile's inbox or
  processed roots. `convert_document` converts supported formats locally.
  `convert_with_ocr` may send page images to the profile endpoint only when the
  profile was configured with `--allow-remote-mcp-ocr`.
- `viewer/` is an unauthenticated, loopback-only, single-run developer
  diagnostic. It is not a production or profile-authenticated service.

Root `requirements.txt` supports source development and these source-only
tools. The installable service ignores it and provisions exclusively from
`install/service-requirements.txt` plus the matching fully hashed Windows lock.

## Development and releases

See [CONTRIBUTING.md](CONTRIBUTING.md) for the test and audit workflow. See
[RELEASING.md](RELEASING.md) for clean-tree archive, checksum, provenance, and
tag requirements.

The installed service uses chandra-ocr, pypdfium2, openai-python, httpx,
openpyxl, Beautiful Soup, and filetype. Source-only conversion additionally
uses firecrawl-anydoc and the MCP Python SDK. Dependency versions are pinned in
the repository; each project and model keeps its own license terms.

## License

Hermes Document Reader is MIT licensed. See [LICENSE](LICENSE).
