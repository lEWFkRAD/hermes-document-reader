# Hermes Document Reader

Watched-folder OCR for an accounting firm, with a live "watch it read" UI.
Drop a scanned PDF in a folder (or on a web page, or into the Hermes desktop
app) and watch the page image on the left while the model's reading streams
into the right pane — then collect an Excel workbook (one sheet per table,
numbers as numbers) and a clean text file next to the original.

Built on the Bearden/Onyx stack: CPU-only gateway box runs the service;
vision OCR runs on a vLLM server (RTX 5090) serving GRM.

## Components

| Path | What it is |
|---|---|
| `engine/grm_ocr.py` | Shared OCR client: streams pages to GRM over vLLM with thinking disabled, normalizes the model's output wrapper, detects mid-stream repetition loops and retries with a temperature bump. Uses chandra's prompts/parsers. |
| `service/ocr_service.py` | The firm service: watches an inbox folder, queues jobs, serves the live web UI + JSON state on the LAN, exports `.xlsx` (duplicate form copies deduplicated) and readable `.txt`. All live state in memory — no status files (Windows lock races). |
| `service/firm.html` | Staff web UI (Hermes desktop design language: flat, token-driven, light/dark). Drag-drop upload, live side-by-side view with a scanning beam, streaming output, job history with downloads. |
| `desktop-plugin/document-reader/plugin.js` | Hermes desktop plugin (plain-ESM, plugin SDK). Native page with sidebar nav + ⌘K command, drag-drop, image prefetch, history with copy-able native output paths. Opt-in (`defaultEnabled: false`). |
| `viewer/viewer.py` + `viewer/index.html` | Single-shot viewer for ad-hoc jobs: `viewer.py file1.pdf [file2.pdf ...] --port 8899`. |
| `mcp/anydoc-mcp.py` | MCP server (FastMCP, stdio) exposing `convert_document` / `convert_with_ocr` to agents. Text-based formats convert locally via anydoc; scanned PDFs go through the GRM engine. |

## Hard-won gotchas (why the engine looks the way it does)

1. **Reasoner burn** — GRM is a thinking model. Without
   `extra_body={"chat_template_kwargs": {"enable_thinking": false}}` it spends
   its whole token budget reasoning and returns empty output.
2. **Output wrapper** — GRM wraps answers in a ```` ```html ```` fence plus an
   `<html><body>` shell; chandra's parser expects bare top-level divs and
   silently returns empty. `normalize_raw()` strips both.
3. **Repetition loops** — multi-copy tax forms (a page of four identical W-2
   copies) send greedy decoding into a loop. The engine detects repetition
   mid-stream, aborts the request, and retries hotter (chandra's own policy).
4. **Windows file locks** — an HTTP thread serving a file blocks `os.replace`
   / `unlink` on it. The service keeps all live state in memory and serves it
   as JSON; the one remaining disk artifact set (page images/html) is
   write-once.

## Deployment (reference)

- Service: gateway box, `venv\Scripts\python.exe -u service/ocr_service.py --port 8899`
  (auto-start via a `schtasks` onstart task). Inbox `D:\OCR-Inbox`, shared on the LAN.
- Plugin: copy `desktop-plugin/document-reader/` into `<hermes home>\desktop-plugins\`,
  enable in Settings → Plugins.
- MCP: register `mcp/anydoc-mcp.py` under `mcp_servers` in the Hermes `config.yaml`,
  command pointing at a venv python that has the `mcp` SDK and chandra installed.
- The engine expects a vLLM endpoint serving a multimodal model; set the
  endpoint/model constants at the top of `engine/grm_ocr.py`.

## Credits / dependencies

This project is glue; the heavy lifting is done by:

| Dependency | Role | License |
|---|---|---|
| [chandra](https://github.com/datalab-to/chandra) (datalab-to) | OCR prompts, page loading, layout parsing, repeat-token detection | Apache-2.0 (separate model license for chandra-ocr weights) |
| [firecrawl-anydoc](https://github.com/firecrawl/anydoc) | Rust document→Markdown conversion (Word/Excel/ODF/RTF/EPUB/CSV/PDF) | MIT |
| GRM (`grm-2.6-plus-0628`) served by [vLLM](https://github.com/vllm-project/vllm) | Vision model doing the actual reading | model license per its distributor; vLLM Apache-2.0 |
| [openai-python](https://github.com/openai/openai-python) | OpenAI-compatible streaming client for vLLM | Apache-2.0 |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp`, FastMCP) | Agent-facing tool server | MIT |
| [openpyxl](https://openpyxl.readthedocs.io/) | Excel workbook export | MIT |
| [Beautiful Soup 4](https://www.crummy.com/software/BeautifulSoup/) | HTML parsing for exports and normalization | MIT |
| [Hermes](https://github.com/NousResearch) plugin SDK & desktop design system | The native app surface this plugs into | per Hermes |

Versions in production at time of writing: chandra 0.2.0, firecrawl-anydoc 0.1.2,
openai 2.24.0, mcp 1.28.1, openpyxl 3.1.5, beautifulsoup4 4.15.0, Python 3.11/3.14.

## License

MIT — see [LICENSE](LICENSE). The dependencies above keep their own licenses.
