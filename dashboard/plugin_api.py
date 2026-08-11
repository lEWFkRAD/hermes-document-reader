"""Authenticated, profile-bound dashboard proxy for Document Reader.

Mounted by Hermes at ``/api/plugins/document-reader``. Every operation resolves
the request's call-time ``HERMES_HOME``, validates the exact owned config/token,
and attests the loopback service before returning any state or content.
"""

from __future__ import annotations

import asyncio
import base64
import html
import http.client
import importlib.util
import json
import re
import sys
import threading
import types
import urllib.parse
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, File, HTTPException, UploadFile


PLUGIN_SOURCE_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PACKAGE = "_hermes_document_reader_runtime"
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_PARTIAL_CHARS = 500_000
MAX_QUEUE = 100
MAX_HISTORY = 30
JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()-]{0,179}$")
SUPPORTED_UPLOADS = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}
ASSET_TYPES = {
    ".jpg": ("image/jpeg", MAX_IMAGE_BYTES),
    ".html": ("text/html; charset=utf-8", MAX_HTML_BYTES),
    ".md": ("text/markdown; charset=utf-8", MAX_DOWNLOAD_BYTES),
    ".txt": ("text/plain; charset=utf-8", MAX_DOWNLOAD_BYTES),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        MAX_DOWNLOAD_BYTES,
    ),
}
_GATES_LOCK = threading.Lock()
_REQUEST_GATES: dict[str, threading.BoundedSemaphore] = {}
_TRANSFER_GATES: dict[str, threading.BoundedSemaphore] = {}


def _load_owned_module(name: str):
    qualified = f"{RUNTIME_PACKAGE}.{name}"
    existing = sys.modules.get(qualified)
    if existing is not None:
        return existing
    if RUNTIME_PACKAGE not in sys.modules:
        package = types.ModuleType(RUNTIME_PACKAGE)
        package.__path__ = [str(PLUGIN_SOURCE_ROOT)]
        package.__package__ = RUNTIME_PACKAGE
        sys.modules[RUNTIME_PACKAGE] = package
    source = PLUGIN_SOURCE_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(qualified, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Document Reader runtime module is missing: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(qualified, None)
        raise
    return module


_profile_runtime = _load_owned_module("profile_runtime")
_lifecycle = _load_owned_module("lifecycle")
router = APIRouter()


def _safe_error(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _context(runtime=None):
    try:
        runtime = runtime or _profile_runtime.resolve_profile_runtime()
        if runtime.transaction_journal.exists() or runtime.transaction_journal.is_symlink():
            raise RuntimeError("lifecycle recovery is required")
        raw = _profile_runtime.read_bounded_json(runtime.config_file)
        config = _lifecycle.validate_service_config(
            runtime, raw, require_current_version=True
        )
        deployment = _lifecycle._validate_deployment(
            runtime,
            _profile_runtime.read_bounded_json(runtime.deployment_receipt),
        )
        release_manifest = _profile_runtime.read_bounded_json(
            Path(str(config["release_root"])) / "release.json"
        )
        if (
            deployment["version"] != config["version"]
            or deployment["release_id"] != config["release_id"]
            or deployment["source_hash"] != release_manifest.get("source_hash")
            or deployment["service_config_sha256"]
            != _lifecycle.sha256_file(runtime.config_file)
            or deployment["task_name"] != config["task_name"]
            or deployment["port"] != config["port"]
        ):
            raise RuntimeError("deployment authority does not match the service config")
        desktop = _lifecycle._validate_desktop_receipt(
            runtime,
            _profile_runtime.read_bounded_json(runtime.desktop_receipt),
        )
        if (
            desktop["release_id"] != deployment["release_id"]
            or desktop["installed_sha256"] != deployment["desktop_sha256"]
            or runtime.desktop_plugin.is_symlink()
            or not runtime.desktop_plugin.is_file()
            or _lifecycle.sha256_file(runtime.desktop_plugin)
            != deployment["desktop_sha256"]
        ):
            raise RuntimeError("desktop ownership does not match the deployment authority")
        task_manager = _lifecycle.LifecycleManager(
            PLUGIN_SOURCE_ROOT, runtime=runtime
        )
        task = task_manager._tasks().inspect(_lifecycle._task_spec(runtime, config))
        _lifecycle._attest_task_result(task, allow_absent=False)
        token = _profile_runtime.validate_token_file(runtime.token_file)
        return runtime, config, token
    except Exception as exc:
        raise _safe_error(503, f"Document Reader profile service is not ready: {type(exc).__name__}")


def _request(
    config: Mapping[str, Any],
    token: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    maximum: int,
    content_type: str | None = None,
) -> tuple[int, Mapping[str, str], bytes]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Document-Reader-Owner": str(config["owner_id"]),
        "Connection": "close",
    }
    if body is not None:
        headers["Content-Length"] = str(len(body))
    if content_type:
        headers["Content-Type"] = content_type
    connection = http.client.HTTPConnection(
        "127.0.0.1", int(config["port"]), timeout=20
    )
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read(maximum + 1)
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        raise _safe_error(503, f"Document Reader service is unreachable: {type(exc).__name__}")
    finally:
        connection.close()
    if len(payload) > maximum:
        raise _safe_error(502, "Document Reader service returned an oversized response")
    return status, response_headers, payload


def _json_payload(status: int, payload: bytes, *, label: str) -> dict[str, Any]:
    if status != 200:
        safe_status = status if 400 <= status < 500 else 502
        raise _safe_error(safe_status, f"Document Reader {label} request failed ({status})")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _safe_error(502, f"Document Reader {label} returned invalid JSON")
    if not isinstance(value, dict):
        raise _safe_error(502, f"Document Reader {label} must return an object")
    return value


def _attested_context():
    runtime = _profile_runtime.resolve_profile_runtime()
    try:
        with _lifecycle.profile_install_lock(runtime, timeout=0.0):
            runtime, config, token = _context(runtime)
            _lifecycle.attest_health(config, token)
    except Exception as exc:
        raise _safe_error(503, f"Document Reader ownership check failed: {type(exc).__name__}")
    return runtime, config, token


@contextmanager
def _profile_gate(runtime, config, token: str, *, transfer: bool = False):
    table = _TRANSFER_GATES if transfer else _REQUEST_GATES
    with _GATES_LOCK:
        gate = table.setdefault(
            runtime.fingerprint,
            threading.BoundedSemaphore(1 if transfer else 4),
        )
    if not gate.acquire(blocking=False):
        raise _safe_error(429, "Document Reader profile is busy; retry shortly")
    try:
        try:
            with _lifecycle.profile_install_lock(runtime, timeout=0.0):
                current_runtime = _profile_runtime.resolve_profile_runtime()
                if (
                    current_runtime.home.resolve(strict=False)
                    != runtime.home.resolve(strict=False)
                    or current_runtime.fingerprint != runtime.fingerprint
                ):
                    raise _safe_error(503, "Document Reader profile changed")
                current_runtime, current_config, current_token = _context(current_runtime)
                if (
                    current_config != config
                    or current_token != token
                ):
                    raise _safe_error(503, "Document Reader profile authority changed")
                yield
                final_runtime = _profile_runtime.resolve_profile_runtime()
                if (
                    final_runtime.home.resolve(strict=False)
                    != runtime.home.resolve(strict=False)
                    or final_runtime.fingerprint != runtime.fingerprint
                ):
                    raise _safe_error(503, "Document Reader profile changed during the request")
                _, final_config, final_token = _context(final_runtime)
                if final_config != config or final_token != token:
                    raise _safe_error(
                        503, "Document Reader profile authority changed during the request"
                    )
        except HTTPException:
            raise
        except Exception as exc:
            raise _safe_error(
                503,
                f"Document Reader profile is unavailable: {type(exc).__name__}",
            )
    finally:
        gate.release()


def _stream_upload(
    config: Mapping[str, Any],
    token: str,
    path: str,
    file_object,
    size: int,
    content_type: str,
) -> tuple[int, Mapping[str, str], bytes]:
    connection = http.client.HTTPConnection(
        "127.0.0.1", int(config["port"]), timeout=180
    )
    try:
        connection.putrequest("POST", path)
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("X-Document-Reader-Owner", str(config["owner_id"]))
        connection.putheader("Content-Type", content_type)
        connection.putheader("Content-Length", str(size))
        connection.putheader("Connection", "close")
        connection.endheaders()
        remaining = size
        while remaining:
            chunk = file_object.read(min(1024 * 1024, remaining))
            if not chunk:
                raise _safe_error(400, "upload ended before its declared size")
            connection.send(chunk)
            remaining -= len(chunk)
        if file_object.read(1):
            raise _safe_error(400, "upload grew after its size was checked")
        response = connection.getresponse()
        payload = response.read(64 * 1024 + 1)
        headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
    except HTTPException:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise _safe_error(503, f"Document Reader upload transport failed: {type(exc).__name__}")
    finally:
        connection.close()
    if len(payload) > 64 * 1024:
        raise _safe_error(502, "Document Reader upload response is oversized")
    return status, headers, payload


def _bounded_string(value: Any, maximum: int = 500) -> str:
    return str(value or "")[:maximum]


def _display_name(value: Any) -> str:
    name = Path(str(value or "").replace("\\", "/")).name
    return name if ASSET_NAME_RE.fullmatch(name) else "document"


def _bounded_number(value: Any, minimum: float = 0, maximum: float = 10**12) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return minimum
    return min(maximum, max(minimum, value))


def _asset_name_from_link(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        name = Path(urllib.parse.unquote(urllib.parse.urlsplit(value).path)).name
    except (UnicodeError, ValueError):
        return None
    return name if ASSET_NAME_RE.fullmatch(name) and Path(name).suffix.lower() in ASSET_TYPES else None


def sanitize_state(runtime, value: Mapping[str, Any]) -> dict[str, Any]:
    queue = []
    for item in value.get("queue", []) if isinstance(value.get("queue"), list) else []:
        if not isinstance(item, dict) or len(queue) >= MAX_QUEUE:
            continue
        queue.append(
            {
                "name": _display_name(item.get("name")),
                "size": int(_bounded_number(item.get("size"), 0, MAX_UPLOAD_BYTES)),
            }
        )

    job_value = value.get("job")
    job = None
    if isinstance(job_value, dict) and JOB_ID_RE.fullmatch(str(job_value.get("id", ""))):
        pages = []
        raw_pages = job_value.get("pages", [])
        if isinstance(raw_pages, list):
            for page in raw_pages[:2000]:
                if not isinstance(page, dict):
                    continue
                pages.append(
                    {
                        "n": int(_bounded_number(page.get("n"), 1, 2000)),
                        "state": _bounded_string(page.get("state"), 32),
                        "secs": _bounded_number(page.get("secs"), 0, 86400),
                        "chars": int(_bounded_number(page.get("chars"), 0, 10_000_000)),
                        "error": "Processing failed" if page.get("error") else "",
                    }
                )
        regions = []
        raw_regions = job_value.get("regions", [])
        if isinstance(raw_regions, list):
            for region in raw_regions[-48:]:
                if not isinstance(region, dict):
                    continue
                regions.append(
                    {
                        "x": _bounded_number(region.get("x"), 0, 100),
                        "y": _bounded_number(region.get("y"), 0, 100),
                        "w": _bounded_number(region.get("w"), 0, 100),
                        "h": _bounded_number(region.get("h"), 0, 100),
                        "kind": _bounded_string(region.get("kind"), 20),
                    }
                )
        job = {
            "id": str(job_value["id"]),
            "name": _display_name(job_value.get("name")),
            "current_file": _display_name(job_value.get("current_file")),
            "state": _bounded_string(job_value.get("state"), 40),
            "total": int(_bounded_number(job_value.get("total"), 0, 2000)),
            "done": int(_bounded_number(job_value.get("done"), 0, 2000)),
            "current": int(_bounded_number(job_value.get("current"), 0, 2000)),
            "partial": _bounded_string(job_value.get("partial"), MAX_PARTIAL_CHARS),
            "region_page": int(_bounded_number(job_value.get("region_page"), 0, 2000)),
            "error": "Processing failed" if job_value.get("error") else "",
            "pages": pages,
            "regions": regions,
        }

    history = []
    raw_history = value.get("history", [])
    if isinstance(raw_history, list):
        for item in raw_history[:MAX_HISTORY]:
            if not isinstance(item, dict) or not JOB_ID_RE.fullmatch(str(item.get("id", ""))):
                continue
            links = item.get("links") if isinstance(item.get("links"), dict) else {}
            files = {
                key: found
                for key in ("md", "xlsx", "txt")
                if (found := _asset_name_from_link(links.get(key))) is not None
            }
            history.append(
                {
                    "id": str(item["id"]),
                    "name": _display_name(item.get("name")),
                    "when": _bounded_string(item.get("when"), 64),
                    "pages": int(_bounded_number(item.get("pages"), 0, 2000)),
                    "secs": _bounded_number(item.get("secs"), 0, 86400),
                    "chars": int(_bounded_number(item.get("chars"), 0, 100_000_000)),
                    "errors": int(_bounded_number(item.get("errors"), 0, 2000)),
                    "files": files,
                }
            )
    return {"queue": queue, "job": job, "history": history}


class _StrictHtmlSanitizer(HTMLParser):
    tags = {
        "p", "br", "strong", "em", "b", "i", "code", "pre", "h1", "h2",
        "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "thead",
        "tbody", "tr", "th", "td", "blockquote",
    }
    void = {"br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.stack: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs):
        tag = tag.casefold()
        if tag in {"script", "style", "svg", "math", "iframe", "object", "embed"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth or tag not in self.tags:
            return
        self.output.append(f"<{tag}>")
        if tag not in self.void:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str):
        tag = tag.casefold()
        if tag in {"script", "style", "svg", "math", "iframe", "object", "embed"}:
            if self.ignored_depth:
                self.ignored_depth -= 1
            return
        if self.ignored_depth or tag not in self.tags or tag in self.void:
            return
        if tag in self.stack:
            while self.stack:
                opened = self.stack.pop()
                self.output.append(f"</{opened}>")
                if opened == tag:
                    break

    def handle_data(self, data: str):
        if not self.ignored_depth:
            self.output.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.output.append(f"</{self.stack.pop()}>")
        return "".join(self.output)


def sanitize_html(raw: str) -> str:
    sanitizer = _StrictHtmlSanitizer()
    sanitizer.feed(raw)
    sanitizer.close()
    return sanitizer.result()


@router.get("/state")
async def state():
    runtime, config, token = await asyncio.to_thread(_attested_context)
    with _profile_gate(runtime, config, token):
        status, _, payload = await asyncio.to_thread(
            _request, config, token, "GET", "/api/state", maximum=MAX_STATE_BYTES
        )
    value = _json_payload(status, payload, label="state")
    if value.get("profile") != runtime.profile_name or value.get("version") != config["version"]:
        raise _safe_error(502, "Document Reader state identity does not match the profile")
    return {
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "service": sanitize_state(runtime, value),
    }


@router.post("/upload")
async def upload(
    expected_profile: str,
    expected_fingerprint: str,
    file: UploadFile = File(...),
):
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", expected_profile)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint)
    ):
        raise _safe_error(400, "profile assertion is invalid")
    name = Path(file.filename or "").name
    if not ASSET_NAME_RE.fullmatch(name):
        raise _safe_error(400, "file name is invalid")
    suffix = Path(name).suffix.casefold()
    if suffix not in SUPPORTED_UPLOADS:
        raise _safe_error(415, "supported types: PDF, PNG, JPEG, TIFF, BMP")
    size = 0
    try:
        await asyncio.to_thread(file.file.seek, 0, 2)
        size = await asyncio.to_thread(file.file.tell)
        await asyncio.to_thread(file.file.seek, 0)
        if not isinstance(size, int) or size <= 0:
            raise _safe_error(400, "file is empty")
        if size > MAX_UPLOAD_BYTES:
            raise _safe_error(413, "file exceeds the 100 MiB upload limit")
        runtime, config, token = await asyncio.to_thread(_attested_context)
        if (
            runtime.profile_name != expected_profile
            or runtime.fingerprint != expected_fingerprint
        ):
            raise _safe_error(409, "Document Reader profile changed before upload")
        target = "/api/upload?" + urllib.parse.urlencode({"name": name})
        with _profile_gate(runtime, config, token, transfer=True):
            status, _, payload = await asyncio.to_thread(
                _stream_upload,
                config,
                token,
                target,
                file.file,
                size,
                SUPPORTED_UPLOADS[suffix],
            )
            await asyncio.to_thread(_lifecycle.attest_health, config, token)
    finally:
        await file.close()
    value = _json_payload(status, payload, label="upload")
    if value.get("ok") is not True:
        raise _safe_error(502, "Document Reader did not accept the upload")
    return {
        "ok": True,
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "name": _display_name(value.get("name")),
        "bytes": size,
    }


@router.post("/cancel")
async def cancel():
    runtime, config, token = await asyncio.to_thread(_attested_context)
    with _profile_gate(runtime, config, token):
        status, _, payload = await asyncio.to_thread(
            _request, config, token, "POST", "/api/cancel", maximum=64 * 1024
        )
    return _json_payload(status, payload, label="cancel")


@router.get("/asset/{job_id}/{filename}")
async def asset(job_id: str, filename: str):
    if not JOB_ID_RE.fullmatch(job_id) or not ASSET_NAME_RE.fullmatch(filename):
        raise _safe_error(404, "asset not found")
    suffix = Path(filename).suffix.casefold()
    if suffix not in ASSET_TYPES:
        raise _safe_error(404, "asset type not available")
    _, maximum = ASSET_TYPES[suffix]
    runtime, config, token = await asyncio.to_thread(_attested_context)
    target = f"/jobs/{job_id}/{urllib.parse.quote(filename, safe='')}"
    with _profile_gate(runtime, config, token, transfer=True):
        status, headers, payload = await asyncio.to_thread(
            _request, config, token, "GET", target, maximum=maximum
        )
        await asyncio.to_thread(_lifecycle.attest_health, config, token)
    if status != 200:
        raise _safe_error(404 if status == 404 else 502, "asset not found")
    declared = headers.get("content-type", "").split(";", 1)[0].casefold()
    expected = ASSET_TYPES[suffix][0].split(";", 1)[0]
    if declared != expected:
        raise _safe_error(502, "asset content type did not match its name")
    if suffix == ".html":
        try:
            raw = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise _safe_error(502, "page HTML is not valid UTF-8")
        return {"kind": "html", "name": filename, "html": sanitize_html(raw)}
    return {
        "kind": "binary",
        "name": filename,
        "content_type": expected,
        "encoding": "base64",
        "data": base64.b64encode(payload).decode("ascii"),
    }
