# Document Reader installed

This package is inert until you enable it and install the service for the
Hermes profile that should own it.

1. Enable `document-reader` in the profile's plugin settings.
2. Configure that profile's OCR endpoint/model. The command prompts for its
   API token without putting it in shell history or process arguments:
   `hermes -p work document-reader configure --api-base https://ocr.example.com/v1 --model model-id`
   Page images are sent to that endpoint. Remote OCR from the source-only MCP
   tool remains disabled for the profile unless this command also includes
   `--allow-remote-mcp-ocr`; Desktop/service OCR is unaffected.
3. Run `hermes -p <profile> document-reader install` (omit `-p` for default).
4. Verify exact task/service ownership with
   `hermes -p <profile> document-reader status`.
5. Restart that profile's gateway and enable/reload the Desktop plugin if the
   Document Reader entry does not appear yet.

Each selected `HERMES_HOME` gets its own deterministic loopback port, token,
unprivileged scheduled task, runtime, and retained `document-reader/data`
directory. Engine/service tokens are profile-owned private files and never
come from global environment variables or appear in task arguments. Rollback
and uninstall keep documents and history. If an interrupted update is reported, run
`hermes -p <profile> document-reader recover` before any other lifecycle action.

Requires Hermes Agent 0.20.0 or newer. The HUD attach action relies on the
0.20 composer attachment provider contract. Service provisioning is supported
only on 64-bit Windows CPython 3.11 or 3.14; each lane installs its fully hashed
lock and refuses a mismatched interpreter, package inventory, or wheel artifact.
