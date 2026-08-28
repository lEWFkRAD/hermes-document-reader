#!/usr/bin/env python
"""Profile-scoped, fail-closed document conversion tools for Hermes."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import math
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from pathlib import Path

import anydoc
from mcp.server import MCPServer


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = PROJECT_ROOT / "engine"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(ENGINE_DIR if ENGINE_DIR.is_dir() else PROJECT_ROOT))

import grm_ocr


mcp = MCPServer("document-reader")

_LOCAL_EXTENSIONS = {
    ".csv", ".doc", ".docx", ".epub", ".html", ".md", ".odp", ".ods",
    ".odt", ".pdf", ".ppt", ".pptx", ".rtf", ".tsv", ".txt", ".xls",
    ".xlsx",
}
_OCR_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".pdf", ".png", ".tif", ".tiff", ".webp"}
_REMOTE_OCR_LOCK = threading.Lock()
_ZIP_EXTENSIONS = {".docx", ".epub", ".odp", ".ods", ".odt", ".pptx", ".xlsx"}


class _ProfileBoundaryError(PermissionError):
    pass


class _SafetyLimitError(ValueError):
    pass


class _RemoteDisabled(PermissionError):
    pass


class _NeedsOcr(anydoc.ConvertError):
    pass


def _fatal_unquiesced_remote() -> None:
    """Never leave a cancellation-resistant credentialed request running."""

    os._exit(70)


@dataclasses.dataclass(frozen=True)
class ApprovedInput:
    path: Path
    root: Path
    suffix: str
    file_info: os.stat_result
    root_info: os.stat_result
    profile_home: Path
    profile_fingerprint: str


@dataclasses.dataclass(frozen=True)
class PagePlan:
    kind: str
    pages: tuple[tuple[int, int, float], ...]
    decoded_bytes: int


@dataclasses.dataclass(frozen=True)
class SnapshotIdentity:
    info: os.stat_result
    size: int
    sha256: str


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    if not raw.isascii() or not raw.isdecimal():
        raise RuntimeError(f"{name} must be a decimal integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _max_input_bytes() -> int:
    return _bounded_int(
        "HERMES_DOCUMENT_READER_MAX_INPUT_BYTES",
        100 * 1024 * 1024,
        1,
        100 * 1024 * 1024,
    )


def _selected_runtime():
    from profile_runtime import resolve_profile_runtime

    return resolve_profile_runtime()


def _literal_absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_info(path: Path, label: str) -> os.stat_result:
    path = _literal_absolute(path)
    grm_ocr._reject_reparse_chain(path, label)
    try:
        info = path.lstat()
    except OSError as exc:
        raise _ProfileBoundaryError(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or grm_ocr._is_link_or_reparse_info(info):
        raise _ProfileBoundaryError(f"{label} must be a regular non-reparse directory")
    return info


def allowed_roots(runtime=None) -> tuple[Path, ...]:
    """Return existing non-reparse roots owned by the selected profile."""

    runtime = runtime or _selected_runtime()
    data_root = _literal_absolute(runtime.data_root)
    _directory_info(data_root, "profile data root")
    base_roots = (_literal_absolute(runtime.inbox), _literal_absolute(runtime.processed))
    configured = os.environ.get("HERMES_DOCUMENT_READER_ALLOWED_ROOTS", "").strip()
    configured_parts = tuple(part.strip() for part in configured.split(os.pathsep) if part.strip())
    if any(not Path(part).is_absolute() for part in configured_parts):
        raise _ProfileBoundaryError("configured document roots must be absolute")
    roots = tuple(_literal_absolute(part) for part in configured_parts)
    if not roots:
        roots = base_roots
    unique: list[Path] = []
    for root in roots:
        if not Path(root).is_absolute():
            raise _ProfileBoundaryError("configured document roots must be absolute")
        if not root.is_relative_to(data_root) or not any(
            root == base or root.is_relative_to(base) for base in base_roots
        ):
            raise _ProfileBoundaryError("configured document roots must narrow this profile's document roots")
        _directory_info(root, "document root")
        if root not in unique:
            unique.append(root)
    return tuple(unique)


def _same_profile(runtime, approved: ApprovedInput | None = None):
    current = _selected_runtime()
    if (
        current.fingerprint != runtime.fingerprint
        or _literal_absolute(current.home) != _literal_absolute(runtime.home)
    ):
        raise _ProfileBoundaryError("selected profile changed")
    roots = allowed_roots(current)
    if approved is not None:
        if approved.root not in roots:
            raise _ProfileBoundaryError("document root authorization changed")
        current_root = _directory_info(approved.root, "document root")
        if not os.path.samestat(approved.root_info, current_root):
            raise _ProfileBoundaryError("document root identity changed")
    return current


def resolve_input(file_path: str) -> ApprovedInput:
    """Approve one literal regular file for the call-time selected profile."""

    supplied = Path(file_path)
    if not supplied.is_absolute():
        raise _ProfileBoundaryError("document path must be absolute")
    runtime = _selected_runtime()
    roots = allowed_roots(runtime)
    candidate = _literal_absolute(supplied)
    root = next(
        (value for value in roots if candidate != value and candidate.is_relative_to(value)),
        None,
    )
    if root is None:
        raise _ProfileBoundaryError("document is outside the selected profile")
    grm_ocr._reject_reparse_chain(candidate.parent, "document path")
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise _ProfileBoundaryError("document is missing or inaccessible") from exc
    if not stat.S_ISREG(info.st_mode) or grm_ocr._is_link_or_reparse_info(info):
        raise _ProfileBoundaryError("document must be a regular non-reparse file")
    maximum = _max_input_bytes()
    if info.st_size <= 0 or info.st_size > maximum:
        raise _SafetyLimitError("document size is outside the configured limit")
    suffix = candidate.suffix.lower()
    if suffix not in _LOCAL_EXTENSIONS | _OCR_EXTENSIONS:
        raise _SafetyLimitError("unsupported document type")
    return ApprovedInput(
        path=candidate,
        root=root,
        suffix=suffix,
        file_info=info,
        root_info=_directory_info(root, "document root"),
        profile_home=_literal_absolute(runtime.home),
        profile_fingerprint=runtime.fingerprint,
    )


def _approved_runtime(approved: ApprovedInput):
    runtime = _selected_runtime()
    if (
        runtime.fingerprint != approved.profile_fingerprint
        or _literal_absolute(runtime.home) != approved.profile_home
    ):
        raise _ProfileBoundaryError("selected profile changed")
    _same_profile(runtime, approved)
    return runtime


def _read_input(approved: ApprovedInput) -> bytes:
    """Read the exact approved identity through a bounded no-follow handle."""

    if not isinstance(approved, ApprovedInput):
        raise TypeError("an approved document handle is required")
    _approved_runtime(approved)
    grm_ocr._reject_reparse_chain(approved.path.parent, "document path")
    try:
        descriptor = grm_ocr._open_no_follow(approved.path)
    except OSError as exc:
        raise _ProfileBoundaryError("document is missing or inaccessible") from exc
    maximum = _max_input_bytes()
    try:
        opened = os.fstat(descriptor)
        if (
            not grm_ocr._same_path_handle_identity(approved.file_info, opened)
            or not stat.S_ISREG(opened.st_mode)
            or grm_ocr._is_link_or_reparse_info(opened)
            or opened.st_size <= 0
            or opened.st_size > maximum
        ):
            raise _ProfileBoundaryError("document identity changed before reading")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            after_path = approved.path.lstat()
        except OSError as exc:
            raise _ProfileBoundaryError("document identity changed while reading") from exc
        grm_ocr._reject_reparse_chain(approved.path.parent, "document path")
        if (
            not data
            or len(data) > maximum
            or len(data) != opened.st_size
            or grm_ocr._stat_signature(opened) != grm_ocr._stat_signature(after)
            or grm_ocr._is_link_or_reparse_info(after_path)
            or not os.path.samestat(after, after_path)
        ):
            raise _ProfileBoundaryError("document changed while reading")
    finally:
        os.close(descriptor)
    _approved_runtime(approved)
    return data


def _bounded_output(markdown: str) -> str:
    maximum = _bounded_int("HERMES_DOCUMENT_READER_MAX_OUTPUT_CHARS", 2_000_000, 1_000, 2_000_000)
    if len(markdown) > maximum:
        raise _SafetyLimitError("converted output exceeds the configured limit")
    return markdown


def _preflight_local_container(data: bytes, suffix: str) -> None:
    if suffix not in _ZIP_EXTENSIONS:
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 4096:
                raise _SafetyLimitError("document archive has too many members")
            total = 0
            for member in members:
                member_name = str(member.filename)
                directory_name = member_name.endswith("/")
                path_body = member_name[:-1] if directory_name else member_name
                parts = path_body.split("/")
                unix_bits = (int(getattr(member, "external_attr", 0)) >> 16) & 0xFFFF
                unix_type = stat.S_IFMT(unix_bits)
                reserved = {
                    "CON", "PRN", "AUX", "NUL",
                    *(f"COM{number}" for number in range(1, 10)),
                    *(f"LPT{number}" for number in range(1, 10)),
                }
                if (
                    not member_name
                    or "\x00" in member_name
                    or "\\" in member_name
                    or ":" in member_name
                    or member_name.startswith("/")
                    or len(member_name) > 4096
                    or not path_body
                    or any(
                        not part
                        or part in {".", ".."}
                        or len(part) > 255
                        or part.rstrip(" .") != part
                        or part.split(".", 1)[0].upper() in reserved
                        for part in parts
                    )
                    or unix_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or (unix_type == stat.S_IFDIR) != directory_name
                    and unix_type != 0
                ):
                    raise _SafetyLimitError("document archive member path is unsafe")
                if member.flag_bits & 0x1 or member.file_size < 0 or member.compress_size < 0:
                    raise _SafetyLimitError("document archive is unsafe")
                total += member.file_size
                if member.file_size > 64 * 1024 * 1024 or total > 128 * 1024 * 1024:
                    raise _SafetyLimitError("document archive expands beyond its limit")
                if member.compress_size == 0 and member.file_size:
                    raise _SafetyLimitError("document archive compression is invalid")
                if member.compress_size and member.file_size > member.compress_size * 1000:
                    raise _SafetyLimitError("document archive compression ratio is unsafe")
    except zipfile.BadZipFile as exc:
        raise anydoc.ConvertError("invalid document container") from exc


def _snapshot_identity(path: Path, data: bytes) -> SnapshotIdentity:
    path = _literal_absolute(path)
    grm_ocr._reject_reparse_chain(path.parent, "private conversion snapshot")
    try:
        info = path.lstat()
    except OSError as exc:
        raise _ProfileBoundaryError("private conversion snapshot is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or grm_ocr._is_link_or_reparse_info(info)
        or info.st_size != len(data)
        or not data
    ):
        raise _ProfileBoundaryError("private conversion snapshot is invalid")
    return SnapshotIdentity(info, len(data), hashlib.sha256(data).hexdigest())


def _read_snapshot(path: Path, expected: SnapshotIdentity) -> bytes:
    """Read and attest one immutable private snapshot through a no-follow handle."""

    path = _literal_absolute(path)
    grm_ocr._reject_reparse_chain(path.parent, "private conversion snapshot")
    try:
        before = path.lstat()
    except OSError as exc:
        raise _ProfileBoundaryError("private conversion snapshot is unavailable") from exc
    if (
        not os.path.samestat(expected.info, before)
        or grm_ocr._stat_signature(expected.info) != grm_ocr._stat_signature(before)
        or before.st_size != expected.size
    ):
        raise _ProfileBoundaryError("private conversion snapshot identity changed")
    payload = grm_ocr.read_regular_bytes(
        path,
        maximum=expected.size,
        label="private conversion snapshot",
    )
    try:
        after = path.lstat()
    except OSError as exc:
        raise _ProfileBoundaryError("private conversion snapshot changed") from exc
    if (
        not os.path.samestat(expected.info, after)
        or grm_ocr._stat_signature(expected.info) != grm_ocr._stat_signature(after)
        or len(payload) != expected.size
        or hashlib.sha256(payload).hexdigest() != expected.sha256
    ):
        raise _ProfileBoundaryError("private conversion snapshot changed")
    return payload


def _local_worker(
    input_path: str,
    suffix: str,
    output_path: str,
    maximum_chars: int,
    memory_limit: int,
    expected_size: int,
    expected_sha256: str,
) -> int:
    if os.name != "nt":
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
        except (ImportError, OSError, ValueError):
            return 70
    try:
        input_file = _literal_absolute(input_path)
        initial = input_file.lstat()
        expected = SnapshotIdentity(initial, expected_size, expected_sha256)
        data = _read_snapshot(input_file, expected)
    except BaseException:
        return 68
    try:
        _preflight_local_container(data, suffix)
        if suffix in {".md", ".txt"}:
            markdown = data.decode("utf-8", errors="strict")
        else:
            safe_name = f"input{suffix}"
            fmt = anydoc.format_from_path(safe_name) or anydoc.format_from_bytes(data)
            if fmt is None:
                return 65
            try:
                rendered = anydoc.to_markdown_bytes(data, format=fmt)
            except BaseException:
                return 67 if suffix == ".pdf" else 65
            markdown = (
                rendered
                if isinstance(rendered, str)
                else rendered.decode("utf-8", errors="strict")
            )
        if not markdown:
            return 67 if suffix == ".pdf" else 66
        if len(markdown) > maximum_chars:
            return 66
        payload = markdown.encode("utf-8")
        if len(payload) > maximum_chars * 4:
            return 66
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return 0
    except BaseException:
        return 65


def _assign_windows_worker_job(process: subprocess.Popen, memory_limit: int):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise RuntimeError("could not create bounded conversion worker")
    limits = EXTENDED_LIMIT()
    limits.BasicLimitInformation.LimitFlags = 0x00000100 | 0x00002000
    limits.ProcessMemoryLimit = memory_limit
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(job)
        raise RuntimeError("could not bound conversion worker memory")
    if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
        kernel.CloseHandle(job)
        raise RuntimeError("could not isolate conversion worker")
    return job, kernel.CloseHandle


def _worker_environment() -> dict[str, str]:
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "LANG", "LC_ALL",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _to_markdown(data: bytes, suffix: str) -> str:
    _preflight_local_container(data, suffix)
    maximum = _bounded_int("HERMES_DOCUMENT_READER_MAX_OUTPUT_CHARS", 2_000_000, 1_000, 2_000_000)
    timeout = _bounded_int("HERMES_DOCUMENT_READER_LOCAL_TIMEOUT", 60, 5, 120)
    memory_limit = _bounded_int(
        "HERMES_DOCUMENT_READER_LOCAL_MEMORY_BYTES",
        1024 * 1024 * 1024,
        256 * 1024 * 1024,
        2 * 1024 * 1024 * 1024,
    )
    with tempfile.TemporaryDirectory(prefix="hermes-document-reader-local-") as temporary:
        root = Path(temporary)
        source = root / f"input{suffix}"
        output = root / "output.md"
        descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        source_identity = _snapshot_identity(source, data)
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--local-worker",
                str(source),
                suffix,
                str(output),
                str(maximum),
                str(memory_limit),
                str(source_identity.size),
                source_identity.sha256,
            ],
            cwd=str(root),
            env=_worker_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        job = None
        try:
            job = _assign_windows_worker_job(process, memory_limit)
            try:
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise _SafetyLimitError("local conversion exceeded its time limit") from None
            _read_snapshot(source, source_identity)
            if return_code == 67:
                raise _NeedsOcr("local PDF requires OCR")
            if return_code != 0:
                raise anydoc.ConvertError("local conversion worker rejected the document")
            payload = grm_ocr.read_regular_bytes(
                output,
                maximum=maximum * 4,
                label="local conversion output",
            )
            markdown = payload.decode("utf-8", errors="strict")
            return _bounded_output(markdown)
        finally:
            if process.poll() is None:
                process.kill()
                with contextlib.suppress(Exception):
                    process.wait(timeout=5)
            if job is not None:
                handle, close_handle = job
                close_handle(handle)


@contextlib.contextmanager
def _private_snapshot(data: bytes, suffix: str):
    with tempfile.TemporaryDirectory(prefix="hermes-document-reader-mcp-") as temporary:
        snapshot = Path(temporary) / f"input{suffix}"
        descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            raise
        identity = _snapshot_identity(snapshot, data)
        try:
            yield snapshot, identity
        finally:
            _read_snapshot(snapshot, identity)


def _page_limits() -> tuple[int, int, int, int]:
    return (
        _bounded_int("HERMES_DOCUMENT_READER_MAX_PAGES", 50, 1, 100),
        _bounded_int("HERMES_DOCUMENT_READER_MAX_PAGE_DIMENSION", 12_000, 512, 16_000),
        _bounded_int("HERMES_DOCUMENT_READER_MAX_PAGE_PIXELS", 25_000_000, 262_144, 40_000_000),
        _bounded_int(
            "HERMES_DOCUMENT_READER_MAX_DECODED_BYTES",
            256 * 1024 * 1024,
            1024 * 1024,
            512 * 1024 * 1024,
        ),
    )


def _check_geometry(width: int, height: int, limits, total: int) -> int:
    _, max_dimension, max_pixels, max_decoded = limits
    if width <= 0 or height <= 0 or max(width, height) > max_dimension:
        raise _SafetyLimitError("page dimensions exceed the configured limit")
    pixels = width * height
    if pixels > max_pixels:
        raise _SafetyLimitError("page pixels exceed the configured limit")
    total += pixels * 3
    if total > max_decoded:
        raise _SafetyLimitError("decoded document exceeds the configured limit")
    return total


def _plan_pages(snapshot: Path, identity: SnapshotIdentity | None = None) -> PagePlan:
    limits = _page_limits()
    maximum_pages = limits[0]
    payload = _read_snapshot(snapshot, identity) if identity is not None else None
    if snapshot.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        from chandra.settings import settings

        document = pdfium.PdfDocument(payload if payload is not None else str(snapshot))
        try:
            count = len(document)
            if not 1 <= count <= maximum_pages:
                raise _SafetyLimitError("PDF page count exceeds the configured limit")
            planned: list[tuple[int, int, float]] = []
            total = 0
            for index in range(count):
                page = document[index]
                try:
                    width_points = float(page.get_width())
                    height_points = float(page.get_height())
                finally:
                    page.close()
                minimum = min(width_points, height_points)
                if not math.isfinite(minimum) or minimum <= 0:
                    raise _SafetyLimitError("PDF page geometry is invalid")
                scale_dpi = max(
                    (float(settings.MIN_PDF_IMAGE_DIM) / minimum) * 72.0,
                    float(settings.IMAGE_DPI),
                )
                width = math.ceil(width_points * scale_dpi / 72.0)
                height = math.ceil(height_points * scale_dpi / 72.0)
                total = _check_geometry(width, height, limits, total)
                planned.append((width, height, scale_dpi / 72.0))
            return PagePlan("pdf", tuple(planned), total)
        finally:
            document.close()

    from PIL import Image

    image_input = io.BytesIO(payload) if payload is not None else snapshot
    with Image.open(image_input) as source:
        count = int(getattr(source, "n_frames", 1))
        if not 1 <= count <= maximum_pages:
            raise _SafetyLimitError("image frame count exceeds the configured limit")
        planned = []
        total = 0
        for index in range(count):
            source.seek(index)
            width, height = map(int, source.size)
            total = _check_geometry(width, height, limits, total)
            planned.append((width, height, 1.0))
    return PagePlan("image", tuple(planned), total)


def _iter_pages(
    snapshot: Path,
    plan: PagePlan,
    identity: SnapshotIdentity | None = None,
):
    payload = _read_snapshot(snapshot, identity) if identity is not None else None
    if plan.kind == "pdf":
        import pypdfium2 as pdfium
        from chandra.input import flatten

        document = pdfium.PdfDocument(payload if payload is not None else str(snapshot))
        try:
            document.init_forms()
            for index, (width, height, scale) in enumerate(plan.pages):
                page = document[index]
                try:
                    flatten(page)
                    image = page.render(scale=scale).to_pil().convert("RGB")
                    image.load()
                finally:
                    page.close()
                if image.size != (width, height):
                    image.close()
                    raise _SafetyLimitError("rendered page geometry changed")
                yield image
        finally:
            document.close()
        return

    from PIL import Image

    image_input = io.BytesIO(payload) if payload is not None else snapshot
    with Image.open(image_input) as source:
        for index, (width, height, _) in enumerate(plan.pages):
            source.seek(index)
            image = source.convert("RGB")
            image.load()
            if image.size != (width, height):
                image.close()
                raise _SafetyLimitError("decoded image geometry changed")
            yield image


def _same_engine(left, right) -> bool:
    import hmac

    public_fields = (
        "api_base", "model", "max_tokens", "request_timeout", "transport_retries",
        "ca_bundle", "allow_insecure_http", "allow_remote_mcp_ocr", "ca_bundle_pem",
    )
    return all(getattr(left, name) == getattr(right, name) for name in public_fields) and hmac.compare_digest(
        left.api_key, right.api_key
    )


def _load_engine_config(runtime):
    from lifecycle import profile_install_lock

    with profile_install_lock(runtime, timeout=5):
        return grm_ocr.load_profile_config(
            runtime.engine_config_file, runtime.engine_token_file
        )


def _assert_engine_current(approved: ApprovedInput, expected) -> None:
    runtime = _approved_runtime(approved)
    current = _load_engine_config(runtime)
    if not current.allow_remote_mcp_ocr or not _same_engine(expected, current):
        raise _ProfileBoundaryError("profile OCR configuration changed")


def _ocr_pages(
    snapshot: Path,
    plan: PagePlan,
    approved: ApprovedInput,
    config,
    *,
    request_timeout: int = 30,
    snapshot_identity: SnapshotIdentity | None = None,
) -> str:
    count = len(plan.pages)
    attempt_budget = _bounded_int("HERMES_DOCUMENT_READER_MAX_REMOTE_ATTEMPTS", 100, 1, 100)
    if count > attempt_budget:
        raise _SafetyLimitError("document exceeds the remote request budget")
    attempts_per_page = min(2, max(1, attempt_budget // count))
    concurrency = _bounded_int("HERMES_DOCUMENT_READER_OCR_CONCURRENCY", 3, 1, 4)
    workers = min(concurrency, count)
    output_limit = _bounded_int(
        "HERMES_DOCUMENT_READER_MAX_OUTPUT_CHARS", 2_000_000, 1_000, 2_000_000
    )
    page_limit = min(250_000, output_limit)

    def run_page(image):
        started = time.monotonic()
        previous = ""
        streamed = 0

        def on_delta(value: str):
            nonlocal previous, streamed
            if value.startswith(previous):
                streamed = len(value)
            else:
                streamed += len(value)
            previous = value
            if streamed > page_limit:
                raise _SafetyLimitError("OCR page output exceeds the configured limit")
            if time.monotonic() - started > request_timeout * attempts_per_page:
                raise _SafetyLimitError("OCR page exceeded its wall-clock limit")

        raw = grm_ocr.ocr_page_raw(
            image, on_delta=on_delta, max_retries=attempts_per_page - 1
        )
        markdown = grm_ocr.raw_to_markdown(raw)
        if len(markdown) > page_limit:
            raise _SafetyLimitError("OCR page output exceeds the configured limit")
        return markdown

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="document-reader-mcp")
    pending = deque()
    in_flight = {}
    rendered: list[str] = []
    size = 0

    def collect_one():
        nonlocal size
        future, image, deadline = pending.popleft()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FutureTimeoutError()
            page = future.result(timeout=remaining)
        finally:
            if future.done():
                image.close()
                in_flight.pop(future, None)
        separator = 2 if rendered else 0
        if size + separator + len(page) > output_limit:
            raise _SafetyLimitError("converted output exceeds the configured limit")
        rendered.append(page)
        size += separator + len(page)
        _approved_runtime(approved)

    page_iterator = iter(_iter_pages(snapshot, plan, snapshot_identity))
    try:
        for image in page_iterator:
            try:
                _assert_engine_current(approved, config)
            except BaseException:
                image.close()
                raise
            future = pool.submit(run_page, image)
            in_flight[future] = image
            pending.append(
                (
                    future,
                    image,
                    time.monotonic() + request_timeout * attempts_per_page,
                )
            )
            if len(pending) >= workers:
                collect_one()
        while pending:
            collect_one()
    except BaseException:
        for future in in_flight:
            future.cancel()
        raise
    finally:
        for future in in_flight:
            future.cancel()
        if in_flight:
            _, unfinished = wait(
                tuple(in_flight), timeout=min(5, max(1, request_timeout))
            )
            if unfinished:
                _fatal_unquiesced_remote()
                _, unfinished = wait(tuple(in_flight), timeout=1)
                if unfinished:
                    raise _SafetyLimitError(
                        "OCR worker did not stop within its cancellation budget"
                    )
        pool.shutdown(wait=True, cancel_futures=True)
        for image in in_flight.values():
            image.close()
        in_flight.clear()
        pending.clear()
        close_pages = getattr(page_iterator, "close", None)
        if callable(close_pages):
            close_pages()
    return "\n\n".join(rendered)


def _public_failure(error: Exception):
    if isinstance(error, _RemoteDisabled):
        raise PermissionError("remote OCR is not enabled for the selected profile") from None
    if isinstance(error, _ProfileBoundaryError):
        raise PermissionError("document access was denied for the selected profile") from None
    if isinstance(error, _SafetyLimitError):
        raise ValueError("document exceeds a safety limit") from None
    module = type(error).__module__.lower()
    name = type(error).__name__.lower()
    if module.startswith(("httpx", "httpcore", "openai")) or any(
        value in name for value in ("timeout", "connection", "transport", "http")
    ):
        raise RuntimeError("OCR service request failed") from None
    if isinstance(error, (ValueError, UnicodeError, anydoc.ConvertError)):
        raise ValueError("document conversion failed safety validation") from None
    raise RuntimeError("document conversion failed") from None


@mcp.tool()
def convert_document(file_path: str) -> str:
    """Convert an approved local document to Markdown without remote OCR."""

    try:
        approved = resolve_input(file_path)
        if approved.suffix not in _LOCAL_EXTENSIONS:
            raise _SafetyLimitError("document requires OCR")
        data = _read_input(approved)
        result = _to_markdown(data, approved.suffix)
        _approved_runtime(approved)
        return result
    except Exception as exc:
        _public_failure(exc)


@mcp.tool()
def convert_with_ocr(file_path: str) -> str:
    """Convert a document; remote OCR requires selected-profile endpoint consent."""

    try:
        approved = resolve_input(file_path)
        data = _read_input(approved)
        if approved.suffix in _LOCAL_EXTENSIONS:
            try:
                result = _to_markdown(data, approved.suffix)
                _approved_runtime(approved)
                return result
            except _NeedsOcr:
                pass
        if approved.suffix not in _OCR_EXTENSIONS:
            raise _SafetyLimitError("document type cannot use OCR")

        with _REMOTE_OCR_LOCK:
            runtime = _approved_runtime(approved)
            config = _load_engine_config(runtime)
            if not config.allow_remote_mcp_ocr:
                raise _RemoteDisabled("profile consent is disabled")
            transport_config = dataclasses.replace(
                config,
                request_timeout=min(config.request_timeout, 30),
                transport_retries=0,
            )
            with _private_snapshot(data, approved.suffix) as (snapshot, snapshot_identity):
                plan = _plan_pages(snapshot, snapshot_identity)
                grm_ocr.configure(transport_config)
                try:
                    result = _ocr_pages(
                        snapshot,
                        plan,
                        approved,
                        config,
                        request_timeout=transport_config.request_timeout,
                        snapshot_identity=snapshot_identity,
                    )
                finally:
                    grm_ocr.configure(None)
            _approved_runtime(approved)
            ending = _load_engine_config(runtime)
            if not ending.allow_remote_mcp_ocr or not _same_engine(config, ending):
                raise _ProfileBoundaryError("profile OCR configuration changed")
            return _bounded_output(result)
    except Exception as exc:
        _public_failure(exc)


if __name__ == "__main__":
    if len(sys.argv) == 9 and sys.argv[1] == "--local-worker":
        raise SystemExit(
            _local_worker(
                sys.argv[2],
                sys.argv[3],
                sys.argv[4],
                int(sys.argv[5]),
                int(sys.argv[6]),
                int(sys.argv[7]),
                sys.argv[8],
            )
        )
    mcp.run()
