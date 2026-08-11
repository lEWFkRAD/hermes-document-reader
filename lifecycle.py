"""Transactional profile-scoped installation and service lifecycle.

The module never mutates machine state at import time.  Every public operation
resolves the selected Hermes profile at call time and verifies both filesystem
receipts and the authenticated service identity before stopping or restarting a
process.  Documents under ``<HERMES_HOME>/document-reader/data`` are never part
of rollback or uninstall cleanup.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import time
import venv
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

try:
    from .engine_config import recover_engine_configuration, validate_engine_config
    from .profile_runtime import (
        PLUGIN_ID,
        PLUGIN_VERSION,
        SERVICE_API_VERSION,
        ProfileRuntime,
        ProfileRuntimeError,
        atomic_write_bytes,
        atomic_write_json,
        create_profile_directories,
        ensure_profile_token,
        loopback_port_open,
        read_bounded_json,
        resolve_profile_runtime,
        validate_token_file,
    )
except ImportError:  # direct ``python cli.py`` execution
    from engine_config import recover_engine_configuration, validate_engine_config  # type: ignore
    from profile_runtime import (  # type: ignore
        PLUGIN_ID,
        PLUGIN_VERSION,
        SERVICE_API_VERSION,
        ProfileRuntime,
        ProfileRuntimeError,
        atomic_write_bytes,
        atomic_write_json,
        create_profile_directories,
        ensure_profile_token,
        loopback_port_open,
        read_bounded_json,
        resolve_profile_runtime,
        validate_token_file,
    )


SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 256 * 1024
MAX_HEALTH_BYTES = 64 * 1024
HEALTH_TIMEOUT_SECONDS = 3.0
START_TIMEOUT_SECONDS = 45.0
STOP_TIMEOUT_SECONDS = 20.0
INSTALL_LOCK_TIMEOUT_SECONDS = 30.0
MAX_LEGACY_FILES = 1000
MAX_LEGACY_BYTES = 20 * 1024 * 1024 * 1024
MAX_LEGACY_INPUT_BYTES = 100 * 1024 * 1024
MAX_LEGACY_PROCESSED_BYTES = 500 * 1024 * 1024
LEGACY_INPUT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
LEGACY_OUTPUT_SUFFIXES = {".md", ".txt", ".xlsx"}
LEGACY_DESKTOP_HASHES = {
    "7624d2497b1a8031a87b6ca2d6ececfff31ae46adefcf084fb8b9a1af7374251",
}
LOCK_FILES = (
    "install/locks/windows-cpython-311-x86_64.txt",
    "install/locks/windows-cpython-314-x86_64.txt",
)
RELEASE_FILES = (
    "engine/grm_ocr.py",
    "service/ocr_service.py",
    "service/firm.html",
    "install/profile_service.py",
    "install/service-requirements.txt",
) + LOCK_FILES
DESKTOP_RELEASE_FILE = "desktop-plugin/document-reader/plugin.js"
RELEASE_SOURCE_FILES = RELEASE_FILES + (DESKTOP_RELEASE_FILE,)
MAX_RELEASE_SOURCE_FILE_BYTES = 32 * 1024 * 1024
MAX_RELEASE_SOURCE_BYTES = 64 * 1024 * 1024
RELEASE_MANIFEST_KEYS = {
    "schema",
    "plugin",
    "version",
    "release_id",
    "source_hash",
    "source_files",
    "runtime_attestation",
    "provisioned",
}
RUNTIME_CONTRACT_KEYS = {
    "implementation",
    "python_version",
    "cache_tag",
    "platform",
    "machine",
    "pointer_bits",
}
RUNTIME_ATTESTATION_KEYS = {
    "contract",
    "lock_file",
    "lock_sha256",
    "pip_version",
    "dependency_set_sha256",
    "artifact_set_sha256",
    "installed_content_sha256",
    "identity_sha256",
}
STAGE_MARKER_KEYS = {
    "schema",
    "plugin",
    "profile",
    "profile_fingerprint",
    "owner_id",
    "source_hash",
    "stage_path",
    "started_at",
}
MAX_STAGE_ENTRIES = 200_000
SERVICE_CONFIG_KEYS = {
    "schema",
    "plugin",
    "version",
    "api_version",
    "profile",
    "profile_fingerprint",
    "owner_id",
    "instance_id",
    "hermes_home",
    "plugin_root",
    "data_root",
    "inbox",
    "processed",
    "jobs",
    "state",
    "logs",
    "bind",
    "port",
    "token_file",
    "release_id",
    "release_root",
    "service_entry",
    "runtime_python",
    "task_name",
}
HEALTH_KEYS = {
    "status",
    "service",
    "version",
    "api_version",
    "profile_name",
    "owner_fingerprint",
    "instance_id",
    "port",
    "pid",
    "started_at",
}
DEPLOYMENT_KEYS = {
    "schema",
    "plugin",
    "version",
    "profile",
    "profile_fingerprint",
    "owner_id",
    "release_id",
    "source_hash",
    "service_config_sha256",
    "desktop_sha256",
    "task_name",
    "port",
    "installed_at",
    "previous_deployment",
    "previous_config",
}
DESKTOP_RECEIPT_KEYS = {
    "schema",
    "plugin",
    "version",
    "profile",
    "profile_fingerprint",
    "owner_id",
    "release_id",
    "installed_sha256",
    "source_sha256",
    "installed_at",
    "previous_plugin",
    "previous_receipt",
}
TRANSACTION_KEYS = {
    "schema",
    "plugin",
    "profile",
    "profile_fingerprint",
    "owner_id",
    "operation",
    "phase",
    "new_release_id",
    "new_config_sha256",
    "new_deployment_sha256",
    "previous_config",
    "previous_config_sha256",
    "previous_deployment",
    "previous_deployment_sha256",
    "previous_desktop_plugin",
    "previous_desktop_plugin_sha256",
    "previous_desktop_receipt",
    "previous_desktop_receipt_sha256",
    "new_desktop_plugin_sha256",
    "previous_task_exists",
    "previous_service_running",
    "started_at",
}
MAINTENANCE_TRANSACTION_KEYS = {
    "schema",
    "plugin",
    "profile",
    "profile_fingerprint",
    "owner_id",
    "operation",
    "phase",
    "snapshot_config",
    "snapshot_config_sha256",
    "snapshot_deployment",
    "snapshot_deployment_sha256",
    "snapshot_desktop_plugin",
    "snapshot_desktop_plugin_sha256",
    "snapshot_desktop_receipt",
    "snapshot_desktop_receipt_sha256",
    "target_config_sha256",
    "target_deployment_sha256",
    "target_desktop_plugin_sha256",
    "target_desktop_receipt_sha256",
    "snapshot_task_exists",
    "snapshot_service_running",
    "started_at",
}


class LifecycleError(RuntimeError):
    """An install/update operation cannot proceed without risking other state."""


@contextmanager
def profile_install_lock(
    runtime: ProfileRuntime,
    *,
    timeout: float = INSTALL_LOCK_TIMEOUT_SECONDS,
):
    """Serialize every mutation for exactly one selected profile."""

    runtime.install_dir.mkdir(parents=True, exist_ok=True)
    handle = runtime.lifecycle_lock.open("a+b")
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.read(1) == b"":
                        handle.seek(0)
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise LifecycleError(
                        "another Document Reader lifecycle operation owns this profile"
                    )
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        else:
            handle.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(value), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    return sha256_bytes(payload)


def _canonical(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise LifecycleError(f"cannot resolve path {path!s}: {exc}") from exc


def _under(base: Path, candidate: Path, label: str) -> Path:
    base = _canonical(base)
    candidate = _canonical(candidate)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise LifecycleError(f"{label} escapes {base}: {candidate}") from exc
    return candidate


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise LifecycleError(f"{label} schema mismatch (missing={missing}, extra={extra})")


def _validate_sha_or_none(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise LifecycleError(f"{label} must be a lowercase SHA-256 or null")
    return value


@dataclass(frozen=True)
class Release:
    release_id: str
    source_hash: str
    root: Path
    entry: Path
    python: Path
    desktop_data: bytes
    desktop_sha256: str
    runtime_attestation: Mapping[str, Any]


@dataclass(frozen=True)
class ReleaseSourceSnapshot:
    """One immutable read of every byte that defines a release."""

    files: tuple[tuple[str, bytes], ...]
    source_hash: str

    def data(self, relative: str) -> bytes:
        for name, value in self.files:
            if name == relative:
                return value
        raise LifecycleError(f"release snapshot is missing: {relative}")

    @property
    def hashes(self) -> dict[str, str]:
        return {name: sha256_bytes(value) for name, value in self.files}


@dataclass(frozen=True)
class TaskSpec:
    name: str
    python: Path
    entry: Path
    config: Path
    working_directory: Path

    @property
    def arguments(self) -> str:
        return f'-B -I -S -u "{self.entry}" --config "{self.config}"'


class TaskBackend(Protocol):
    def probe_name(self, task_name: str) -> dict[str, Any]: ...
    def inspect(self, spec: TaskSpec) -> dict[str, Any]: ...
    def install(self, spec: TaskSpec) -> None: ...
    def start(self, spec: TaskSpec) -> None: ...
    def remove(self, spec: TaskSpec) -> None: ...


class WindowsTaskBackend:
    """Low-level Task Scheduler adapter; the PowerShell script verifies action ownership."""

    def __init__(self, script: Path):
        self.script = _canonical(script)
        if os.name != "nt":
            raise LifecycleError("Document Reader scheduled service is currently supported on Windows only")

    @staticmethod
    def _parse_result(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Task Scheduler command failed").strip()
            raise LifecycleError(message[-2000:])
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return {"ok": True}
        try:
            value = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise LifecycleError("Task Scheduler helper returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise LifecycleError(str(value.get("error", "Task Scheduler helper failed")))
        return value

    def _run(self, action: str, spec: TaskSpec) -> dict[str, Any]:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
            "-Action",
            action,
            "-TaskName",
            spec.name,
            "-Python",
            str(spec.python),
            "-ServiceEntry",
            str(spec.entry),
            "-ConfigPath",
            str(spec.config),
            "-WorkingDirectory",
            str(spec.working_directory),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
        return self._parse_result(result)

    def probe_name(self, task_name: str) -> dict[str, Any]:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
            "-Action",
            "Probe",
            "-TaskName",
            task_name,
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=45, check=False
        )
        return self._parse_result(result)

    def inspect(self, spec: TaskSpec) -> dict[str, Any]:
        return self._run("Status", spec)

    def install(self, spec: TaskSpec) -> None:
        self._run("Install", spec)

    def start(self, spec: TaskSpec) -> None:
        self._run("Start", spec)

    def remove(self, spec: TaskSpec) -> None:
        self._run("Remove", spec)


def _release_source_directories(
    source_root: Path,
) -> tuple[tuple[Path, os.stat_result], ...]:
    literal = Path(source_root).expanduser()
    if not literal.is_absolute() or ".." in literal.parts:
        raise LifecycleError("release source root must be an absolute normalized path")
    root = Path(os.path.abspath(literal))
    directories: set[Path] = set()
    cursor = root
    while True:
        directories.add(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for relative in RELEASE_SOURCE_FILES:
        cursor = (root / Path(relative)).parent
        while True:
            directories.add(cursor)
            if cursor == root:
                break
            if root not in cursor.parents:
                raise LifecycleError(f"release input path escapes the source root: {relative}")
            cursor = cursor.parent
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    identities: list[tuple[Path, os.stat_result]] = []
    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item).lower())):
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise LifecycleError(f"release source directory is missing: {directory}") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse_flag)
        ):
            raise LifecycleError(
                f"release source directory must not be a link/reparse point: {directory}"
            )
        identities.append((directory, info))
    return tuple(identities)


def _attest_release_source_directories(
    identities: tuple[tuple[Path, os.stat_result], ...]
) -> None:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for directory, expected in identities:
        try:
            current = os.lstat(directory)
        except OSError as exc:
            raise LifecycleError(f"release source directory changed: {directory}") from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or bool(getattr(current, "st_file_attributes", 0) & reparse_flag)
            or not os.path.samestat(expected, current)
        ):
            raise LifecycleError(f"release source directory changed: {directory}")


def _release_source_bytes(
    path: Path,
    relative: str,
    directory_identities: tuple[tuple[Path, os.stat_result], ...],
) -> bytes:
    """Read a bounded regular source file twice through one fixed handle."""

    _attest_release_source_directories(directory_identities)
    try:
        literal_before = os.lstat(path)
    except FileNotFoundError as exc:
        raise LifecycleError(f"release input is missing: {relative}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(literal_before.st_mode)
        or stat.S_ISLNK(literal_before.st_mode)
        or bool(getattr(literal_before, "st_file_attributes", 0) & reparse_flag)
    ):
        raise LifecycleError(f"release input must be a regular non-reparse file: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LifecycleError(f"release input could not be opened safely: {relative}") from exc
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode) or not os.path.samestat(
            literal_before, opened_before
        ):
            raise LifecycleError(f"release input changed before it was opened: {relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            first = handle.read(MAX_RELEASE_SOURCE_FILE_BYTES + 1)
            if len(first) > MAX_RELEASE_SOURCE_FILE_BYTES:
                raise LifecycleError(f"release input is too large: {relative}")
            handle.seek(0)
            second = handle.read(MAX_RELEASE_SOURCE_FILE_BYTES + 1)
        opened_after = os.fstat(descriptor)
        literal_after = os.lstat(path)
        _attest_release_source_directories(directory_identities)
        if (
            first != second
            or not os.path.samestat(opened_before, opened_after)
            or not os.path.samestat(opened_after, literal_after)
            or opened_before.st_size != opened_after.st_size
            or opened_before.st_mtime_ns != opened_after.st_mtime_ns
        ):
            raise LifecycleError(f"release input changed while it was read: {relative}")
        return first
    except FileNotFoundError as exc:
        raise LifecycleError(f"release input changed while it was read: {relative}") from exc
    finally:
        os.close(descriptor)


def capture_release_source(source_root: Path) -> ReleaseSourceSnapshot:
    directory_identities = _release_source_directories(source_root)
    source_root = Path(os.path.abspath(Path(source_root).expanduser()))
    _attest_release_source_directories(directory_identities)
    captured: list[tuple[str, bytes]] = []
    total = 0
    digest = hashlib.sha256()
    for relative in RELEASE_SOURCE_FILES:
        data = _release_source_bytes(
            source_root / Path(relative), relative, directory_identities
        )
        total += len(data)
        if total > MAX_RELEASE_SOURCE_BYTES:
            raise LifecycleError("release inputs exceed the aggregate size limit")
        captured.append((relative, data))
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
        _attest_release_source_directories(directory_identities)
    _attest_release_source_directories(directory_identities)
    return ReleaseSourceSnapshot(files=tuple(captured), source_hash=digest.hexdigest())


def source_hash(source_root: Path) -> str:
    return capture_release_source(source_root).source_hash


def _release_python(release_root: Path) -> Path:
    if os.name == "nt":
        return release_root / ".venv" / "Scripts" / "python.exe"
    return release_root / ".venv" / "bin" / "python"


def _machine_from_build_platform(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    return {
        "win-amd64": "x86_64",
        "win-x86-64": "x86_64",
        "win-arm64": "arm64",
        "win32": "x86",
    }.get(normalized, normalized)


def _validate_runtime_contract(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(raw)
    _exact_keys(value, RUNTIME_CONTRACT_KEYS, "runtime contract")
    version = value.get("python_version")
    if (
        value.get("implementation") != "cpython"
        or not isinstance(version, str)
        or not re.fullmatch(r"3\.(?:11|14)\.[0-9]+", version)
        or value.get("cache_tag") != f"cpython-{version.split('.')[0]}{version.split('.')[1]}"
        or value.get("platform") != "win32"
        or value.get("machine") != "x86_64"
        or value.get("pointer_bits") != 64
    ):
        raise LifecycleError(
            "Document Reader requires 64-bit Windows CPython 3.11 or 3.14 with a matching cache tag"
        )
    return value


def _current_runtime_contract() -> dict[str, Any]:
    return _validate_runtime_contract(
        {
            "implementation": sys.implementation.name,
            "python_version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag,
            "platform": sys.platform,
            "machine": _machine_from_build_platform(sysconfig.get_platform()),
            "pointer_bits": struct.calcsize("P") * 8,
        }
    )


def _interpreter_contract(python: Path) -> dict[str, Any]:
    script = (
        "import json,platform,struct,sys,sysconfig;"
        "b=sysconfig.get_platform().strip().lower().replace('_','-');"
        "m={'win-amd64':'x86_64','win-x86-64':'x86_64','win-arm64':'arm64','win32':'x86'}.get(b,b);"
        "print(json.dumps({'implementation':sys.implementation.name,"
        "'python_version':platform.python_version(),'cache_tag':sys.implementation.cache_tag,"
        "'platform':sys.platform,'machine':m,'pointer_bits':struct.calcsize('P')*8},"
        "sort_keys=True,separators=(',',':')))"
    )
    result = subprocess.run(
        [str(python), "-B", "-I", "-S", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if result.returncode != 0:
        raise LifecycleError("service interpreter contract could not be inspected")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleError("service interpreter contract was not valid JSON") from exc
    if not isinstance(value, dict):
        raise LifecycleError("service interpreter contract must be an object")
    return _validate_runtime_contract(value)


def _lock_for_contract(contract: Mapping[str, Any]) -> str:
    minor = str(contract["python_version"]).split(".")[1]
    relative = f"install/locks/windows-cpython-3{minor}-x86_64.txt"
    if relative not in LOCK_FILES:
        raise LifecycleError("no hashed dependency lock exists for this runtime")
    return relative


def _isolated_subprocess_env() -> dict[str, str]:
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "PATH",
        "PATHEXT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "PIP_CERT",
        "PIP_INDEX_URL",
    }
    value = {key: item for key, item in os.environ.items() if key.upper() in allowed}
    value.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
    )
    return value


PRIVATE_RUNTIME_RUNNER_SCRIPT = r'''
import os, runpy, stat, sys
from pathlib import Path
if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
    raise SystemExit("private runtime runner requires -B -I -S")
runtime_root = Path(sys.executable).resolve(strict=True).parents[1]
site_packages = runtime_root / "Lib" / "site-packages"
reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
for candidate in (runtime_root, runtime_root / "Lib", site_packages):
    info = os.lstat(candidate)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse:
        raise SystemExit("private runtime contains a link/reparse directory")
sys.path.append(str(site_packages))
if len(sys.argv) < 3 or sys.argv[1] not in {"module", "code"}:
    raise SystemExit("invalid private runtime operation")
mode, payload, arguments = sys.argv[1], sys.argv[2], sys.argv[3:]
if mode == "module":
    sys.argv = [payload, *arguments]
    runpy.run_module(payload, run_name="__main__", alter_sys=False)
else:
    if arguments:
        raise SystemExit("private runtime code operation does not accept arguments")
    exec(compile(payload, "<document-reader-private-probe>", "exec"), {"__name__": "__main__"})
'''


BOOTSTRAP_PIP_RUNNER_SCRIPT = r'''
import os, stat, sys
from pathlib import Path
if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
    raise SystemExit("pip bootstrap requires -B -I -S")
executable = Path(sys.executable)
runtime_root = executable.parent.parent
site_packages = runtime_root / "Lib" / "site-packages"
configuration = runtime_root / "pyvenv.cfg"
reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
for candidate in (runtime_root, runtime_root / "Scripts", runtime_root / "Lib", site_packages):
    info = os.lstat(candidate)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & reparse
    ):
        raise SystemExit("private bootstrap runtime contains a link/reparse directory")
for candidate in (executable, configuration):
    info = os.lstat(candidate)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & reparse
    ):
        raise SystemExit("private bootstrap runtime identity is invalid")
if configuration.stat().st_size > 16 * 1024:
    raise SystemExit("private bootstrap pyvenv configuration is oversized")
if "include-system-site-packages = false" not in configuration.read_text(
    encoding="utf-8", errors="strict"
).casefold():
    raise SystemExit("private bootstrap runtime does not disable system site packages")
# Python 3.11 applies pyvenv.cfg from site.py, which -S intentionally disables.
# Bind pip to the already-attested owned venv explicitly before importing pip or
# sysconfig; base_prefix remains unchanged, so pip recognizes a real venv and
# cannot fall back to the externally managed base interpreter.
sys.prefix = str(runtime_root)
sys.exec_prefix = str(runtime_root)
import ensurepip, runpy
bundle = Path(ensurepip.__file__).parent / "_bundled"
version = ensurepip.version()
wheel = bundle / ("pip-" + version + "-py3-none-any.whl")
bundle_info = os.lstat(bundle)
wheel_info = os.lstat(wheel)
if (
    not stat.S_ISDIR(bundle_info.st_mode)
    or stat.S_ISLNK(bundle_info.st_mode)
    or getattr(bundle_info, "st_file_attributes", 0) & reparse
):
    raise SystemExit("Python ensurepip bundle is not a regular directory")
if (
    not stat.S_ISREG(wheel_info.st_mode)
    or stat.S_ISLNK(wheel_info.st_mode)
    or getattr(wheel_info, "st_file_attributes", 0) & reparse
    or not 1024 <= wheel_info.st_size <= 32 * 1024 * 1024
):
    raise SystemExit("Python ensurepip pip wheel is invalid")
sys.path.insert(0, str(wheel))
sys.argv = ["pip", *sys.argv[1:]]
runpy.run_module("pip", run_name="__main__", alter_sys=True)
'''


def _private_runtime_command(
    python: Path,
    *,
    module: str | None = None,
    code: str | None = None,
    arguments: Iterable[str] = (),
) -> list[str]:
    if (module is None) == (code is None):
        raise LifecycleError("private runtime command requires exactly one operation")
    if module is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", module):
        raise LifecycleError("private runtime module name is invalid")
    mode = "module" if module is not None else "code"
    payload = module if module is not None else str(code)
    return [
        str(python),
        "-B",
        "-I",
        "-S",
        "-c",
        PRIVATE_RUNTIME_RUNNER_SCRIPT,
        mode,
        str(payload),
        *[str(item) for item in arguments],
    ]


def _bootstrap_pip_command(python: Path, arguments: Iterable[str]) -> list[str]:
    return [
        str(python),
        "-B",
        "-I",
        "-S",
        "-c",
        BOOTSTRAP_PIP_RUNNER_SCRIPT,
        *[str(item) for item in arguments],
    ]


def _lock_inventory(
    lock_path: Path,
) -> tuple[dict[str, str], dict[str, frozenset[str]], str]:
    try:
        text = lock_path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise LifecycleError("hashed dependency lock is unreadable") from exc
    logical: list[str] = []
    current = ""
    for physical in text.splitlines():
        stripped = physical.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current = f"{current} {stripped}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        logical.append(current)
        current = ""
    if current:
        raise LifecycleError("hashed dependency lock has an unterminated continuation")
    inventory: dict[str, str] = {}
    allowed_hashes: dict[str, frozenset[str]] = {}
    for line in logical:
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9._-]{0,199})==([^\s;\\]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)",
            line,
        )
        if match is None:
            raise LifecycleError("hashed dependency lock contains an unsupported requirement")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        version = match.group(2)
        if name in inventory:
            raise LifecycleError("hashed dependency lock contains a duplicate distribution")
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", match.group(3))
        if not hashes or len(set(hashes)) != len(hashes):
            raise LifecycleError("hashed dependency lock contains invalid artifact hashes")
        inventory[name] = version
        allowed_hashes[name] = frozenset(hashes)
    if not inventory or inventory.get("pip") != "26.1.2" or "setuptools" not in inventory:
        raise LifecycleError("hashed dependency lock must pin pip 26.1.2 and setuptools")
    payload = json.dumps(
        [{"name": name, "version": inventory[name]} for name in sorted(inventory)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return inventory, allowed_hashes, sha256_bytes(payload)


def _generated_bytecode_policy(runtime_root: Path, *, remove: bool) -> None:
    root = Path(runtime_root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise LifecycleError("service runtime root is not a regular directory")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    cache_directories: list[Path] = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directory_info = os.lstat(directory_path)
        if stat.S_ISLNK(directory_info.st_mode) or bool(
            getattr(directory_info, "st_file_attributes", 0) & reparse_flag
        ):
            raise LifecycleError("service runtime contains a link/reparse directory")
        for name in names:
            candidate = directory_path / name
            info = os.lstat(candidate)
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                raise LifecycleError("service runtime contains a link/reparse directory")
            if name == "__pycache__":
                cache_directories.append(candidate)
        for name in files:
            candidate = directory_path / name
            info = os.lstat(candidate)
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                raise LifecycleError("service runtime contains a link/reparse file")
            if candidate.suffix.casefold() == ".pyc":
                if not remove:
                    raise LifecycleError("service runtime contains unattested executable bytecode")
                candidate.unlink()
    if remove:
        for directory in sorted(cache_directories, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError as exc:
                raise LifecycleError(
                    "service runtime bytecode directory contains an unexpected file"
                ) from exc


def _prepare_deterministic_bytecode(python: Path, runtime_root: Path) -> None:
    _generated_bytecode_policy(runtime_root, remove=True)
    site_packages = runtime_root / "Lib" / "site-packages"
    if not site_packages.is_dir() or site_packages.is_symlink():
        raise LifecycleError("service runtime site-packages is missing")
    result = subprocess.run(
        [
            str(python),
            "-B",
            "-I",
            "-S",
            "-m",
            "compileall",
            "-q",
            "-f",
            "--invalidation-mode",
            "checked-hash",
            "-s",
            str(site_packages),
            "-p",
            "/document-reader/site-packages",
            str(site_packages),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if result.returncode != 0:
        raise LifecycleError("deterministic service bytecode compilation failed")


REMOVE_DISTRIBUTION_ENTRYPOINTS_SCRIPT = r'''
import base64, hashlib, importlib.metadata as metadata, json, os, pathlib, re, stat, sys
if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
    raise RuntimeError("entrypoint removal requires -B -I -S")
prefix = pathlib.Path(sys.executable).resolve(strict=True).parents[1]
site_root = (prefix / "Lib" / "site-packages").resolve(strict=True)
scripts_root = prefix / "Scripts"
reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
scripts_info = os.lstat(scripts_root)
if (
    not stat.S_ISDIR(scripts_info.st_mode)
    or stat.S_ISLNK(scripts_info.st_mode)
    or getattr(scripts_info, "st_file_attributes", 0) & reparse
):
    raise RuntimeError("service Scripts directory is not regular")
venv_generated = {"activate", "activate.bat", "Activate.ps1", "deactivate.bat", "python.exe", "pythonw.exe"}
if sys.version_info[:2] >= (3, 14):
    venv_generated.add("activate.fish")
preserved = {"python.exe"}
removed = set()
distributions = sorted(
    metadata.distributions(path=[str(site_root)]),
    key=lambda item: (
        re.sub(r"[-_.]+", "-", str(item.metadata.get("Name", ""))).lower(),
        str(item.version),
    ),
)
for dist in distributions:
    entries = dist.files
    if entries is None:
        raise RuntimeError("distribution RECORD is missing")
    for entry in entries:
        record_path = str(entry).replace("\\", "/")
        literal = pathlib.Path(dist.locate_file(entry))
        absolute = pathlib.Path(os.path.abspath(literal))
        try:
            relative = absolute.relative_to(prefix)
        except ValueError:
            continue
        if not relative.parts or relative.parts[0].casefold() != "scripts":
            continue
        if (
            len(relative.parts) != 2
            or not record_path.startswith("../../Scripts/")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", relative.name)
            or relative.name in venv_generated
        ):
            raise RuntimeError("distribution declares an unsafe Scripts entry")
        key = os.path.normcase(str(absolute))
        if key in removed:
            raise RuntimeError("multiple distributions declare the same Scripts entry")
        declared_hash = getattr(entry, "hash", None)
        declared_size = getattr(entry, "size", None)
        if (
            declared_hash is None
            or declared_hash.mode != "sha256"
            or declared_size is None
        ):
            raise RuntimeError("distribution Scripts entry lacks SHA256 RECORD identity")
        expected = base64.urlsafe_b64decode(
            declared_hash.value + "=" * (-len(declared_hash.value) % 4)
        )
        if len(expected) != 32:
            raise RuntimeError("distribution Scripts RECORD hash is malformed")
        info = os.lstat(literal)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & reparse
            or info.st_size != int(declared_size)
        ):
            raise RuntimeError("distribution Scripts entry differs from RECORD")
        digest = hashlib.sha256()
        with literal.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.digest() != expected:
            raise RuntimeError("distribution Scripts entry differs from RECORD")
        literal.unlink()
        if os.path.lexists(literal):
            raise RuntimeError("distribution Scripts entry could not be removed")
        removed.add(key)
remaining = set()
for entry in os.scandir(scripts_root):
    info = entry.stat(follow_symlinks=False)
    if (
        entry.is_symlink()
        or not entry.is_file(follow_symlinks=False)
        or getattr(info, "st_file_attributes", 0) & reparse
    ):
        raise RuntimeError("service Scripts directory contains a non-regular entry")
    if entry.name in preserved:
        remaining.add(entry.name)
        continue
    if entry.name not in venv_generated:
        raise RuntimeError("service Scripts directory contains an unexpected entry")
    pathlib.Path(entry.path).unlink()
    if os.path.lexists(entry.path):
        raise RuntimeError("service venv launcher could not be removed")
if remaining != preserved:
    raise RuntimeError("service Scripts directory contains an unexpected entry")
print(json.dumps({"removed": sorted(removed)}, sort_keys=True, separators=(",", ":")))
'''


def _remove_distribution_entrypoints(python: Path) -> tuple[str, ...]:
    result = subprocess.run(
        _private_runtime_command(
            python,
            code=REMOVE_DISTRIBUTION_ENTRYPOINTS_SCRIPT,
        ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if result.returncode != 0:
        raise LifecycleError(
            "service console entrypoint removal failed: "
            + (result.stderr or result.stdout or "entrypoint verifier failed")[-2000:]
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleError("service console entrypoint removal result was invalid") from exc
    removed = value.get("removed") if isinstance(value, dict) and set(value) == {"removed"} else None
    if (
        not isinstance(removed, list)
        or not removed
        or len(removed) > 256
        or len(set(removed)) != len(removed)
        or any(not isinstance(item, str) or not item for item in removed)
    ):
        raise LifecycleError("service console entrypoint removal set was invalid")
    return tuple(removed)


INSTALLED_ENVIRONMENT_ATTESTATION_SCRIPT = r'''
import base64, hashlib, importlib.metadata as metadata, json, os, pathlib, re, stat, sys
if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
    raise RuntimeError("environment attestation requires -B -I -S")
prefix = pathlib.Path(sys.executable).resolve(strict=True).parents[1]
site_root = (prefix / "Lib" / "site-packages").resolve(strict=True)
scripts_root = prefix / "Scripts"
packages = {}
content = hashlib.sha256()
declared_files = set()
files_seen = 0
bytes_seen = 0
reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
baseline_scripts = {"python.exe"}
distributions = sorted(
    metadata.distributions(path=[str(site_root)]),
    key=lambda item: (
        re.sub(r"[-_.]+", "-", str(item.metadata.get("Name", ""))).lower(),
        str(item.version),
    ),
)
for dist in distributions:
    raw_name = dist.metadata.get("Name")
    version = dist.version
    if not isinstance(raw_name, str) or not isinstance(version, str):
        raise RuntimeError("distribution identity is incomplete")
    name = re.sub(r"[-_.]+", "-", raw_name).lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,199}", name) or name in packages:
        raise RuntimeError("distribution identity is malformed or duplicated")
    packages[name] = version
    entries = dist.files
    if entries is None:
        raise RuntimeError("distribution RECORD is missing")
    for entry in sorted(entries, key=lambda item: str(item).lower()):
        record_path = str(entry).replace("\\", "/")
        if record_path.endswith(".dist-info/direct_url.json"):
            raise RuntimeError("direct/local distribution metadata is not allowed")
        bytecode = "/__pycache__/" in f"/{record_path}" or record_path.endswith(".pyc")
        declared_hash = getattr(entry, "hash", None)
        declared_size = getattr(entry, "size", None)
        literal = pathlib.Path(dist.locate_file(entry))
        absolute = pathlib.Path(os.path.abspath(literal))
        try:
            intended_relative = absolute.relative_to(prefix)
        except ValueError:
            intended_relative = None
        removed_script = bool(
            intended_relative is not None
            and len(intended_relative.parts) == 2
            and intended_relative.parts[0].casefold() == "scripts"
        )
        if removed_script:
            if (
                not record_path.startswith("../../Scripts/")
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", intended_relative.name
                )
                or intended_relative.name in baseline_scripts
                or declared_hash is None
                or declared_hash.mode != "sha256"
                or declared_size is None
                or os.path.lexists(literal)
            ):
                raise RuntimeError("distribution console entrypoint absence is invalid")
            expected = base64.urlsafe_b64decode(
                declared_hash.value + "=" * (-len(declared_hash.value) % 4)
            )
            if len(expected) != 32:
                raise RuntimeError("distribution console entrypoint hash is malformed")
            declared_files.add(os.path.normcase(str(absolute)))
            content.update(
                name.encode("utf-8") + b"\0" + version.encode("utf-8") + b"\0"
                + b"removed-script\0" + record_path.encode("utf-8") + b"\0"
            )
            continue
        info = os.lstat(literal)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse:
            raise RuntimeError("installed distribution contains a link/reparse point")
        resolved = literal.resolve(strict=True)
        relative = resolved.relative_to(prefix)
        declared_files.add(os.path.normcase(str(resolved)))
        cursor = literal.parent
        while True:
            parent_info = os.lstat(cursor)
            if stat.S_ISLNK(parent_info.st_mode) or getattr(parent_info, "st_file_attributes", 0) & reparse:
                raise RuntimeError("installed distribution traverses a link/reparse point")
            if cursor.resolve(strict=True) == prefix:
                break
            if cursor.parent == cursor:
                raise RuntimeError("installed distribution escapes the runtime")
            cursor = cursor.parent
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024 * 1024:
            raise RuntimeError("installed distribution file is invalid")
        files_seen += 1
        bytes_seen += info.st_size
        if files_seen > 300000 or bytes_seen > 64 * 1024 * 1024 * 1024:
            raise RuntimeError("installed distribution set exceeds attestation limits")
        if declared_size is not None and info.st_size != int(declared_size):
            raise RuntimeError("installed distribution file size differs from RECORD")
        if declared_hash is None:
            if record_path.endswith(".dist-info/RECORD"):
                continue
            if not bytecode:
                raise RuntimeError("installed distribution file lacks a RECORD hash")
            digest = hashlib.sha256()
            with literal.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            content.update(
                name.encode("utf-8") + b"\0" + version.encode("utf-8") + b"\0"
                + record_path.encode("utf-8") + b"\0" + digest.digest()
                + b"\0" + str(info.st_size).encode("ascii") + b"\0"
            )
            continue
        if declared_size is None:
            raise RuntimeError("installed distribution file lacks a RECORD size")
        if declared_hash.mode != "sha256":
            raise RuntimeError("installed distribution uses a non-SHA256 RECORD hash")
        expected = base64.urlsafe_b64decode(declared_hash.value + "=" * (-len(declared_hash.value) % 4))
        digest = hashlib.sha256()
        with literal.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.digest() != expected:
            raise RuntimeError("installed distribution file differs from RECORD")
        content.update(
            name.encode("utf-8") + b"\0" + version.encode("utf-8") + b"\0"
            + record_path.encode("utf-8") + b"\0" + expected
            + b"\0" + str(info.st_size).encode("ascii") + b"\0"
        )
for directory, names, files in os.walk(site_root, topdown=True, followlinks=False):
    directory_path = pathlib.Path(directory)
    names.sort(key=lambda value: (value.casefold(), value))
    files.sort(key=lambda value: (value.casefold(), value))
    for name in names:
        candidate = directory_path / name
        info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse:
            raise RuntimeError("site-packages contains an untracked link/reparse directory")
    for name in files:
        candidate = directory_path / name
        info = os.lstat(candidate)
        resolved = candidate.resolve(strict=True)
        key = os.path.normcase(str(resolved))
        if key in declared_files:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse:
            raise RuntimeError("site-packages contains an untracked link/reparse file")
        relative = resolved.relative_to(site_root).as_posix()
        if candidate.suffix.casefold() != ".pyc":
            raise RuntimeError("site-packages contains an untracked non-bytecode file")
        digest = hashlib.sha256(candidate.read_bytes()).digest()
        content.update(
            b"unrecorded-bytecode\0" + relative.encode("utf-8") + b"\0"
            + digest + b"\0" + str(info.st_size).encode("ascii") + b"\0"
        )
actual_scripts = set()
for entry in os.scandir(scripts_root):
    info = entry.stat(follow_symlinks=False)
    if (
        entry.is_symlink()
        or not entry.is_file(follow_symlinks=False)
        or getattr(info, "st_file_attributes", 0) & reparse
    ):
        raise RuntimeError("service Scripts directory contains a non-regular entry")
    actual_scripts.add(entry.name)
if actual_scripts != baseline_scripts:
    raise RuntimeError("service Scripts directory contains an unexpected entry")
python_entry = scripts_root / "python.exe"
python_info = os.lstat(python_entry)
if (
    not stat.S_ISREG(python_info.st_mode)
    or stat.S_ISLNK(python_info.st_mode)
    or getattr(python_info, "st_file_attributes", 0) & reparse
    or not 64 * 1024 <= python_info.st_size <= 64 * 1024 * 1024
    or not os.path.samefile(python_entry, sys.executable)
):
    raise RuntimeError("service interpreter identity is invalid")
python_digest = hashlib.sha256()
with python_entry.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        python_digest.update(chunk)
content.update(
    b"runtime-python\0" + python_digest.digest() + b"\0"
    + str(python_info.st_size).encode("ascii") + b"\0"
)
payload = json.dumps([{"name": name, "version": packages[name]} for name in sorted(packages)], separators=(",", ":")).encode("ascii")
print(json.dumps({"dependency_set_sha256": hashlib.sha256(payload).hexdigest(), "installed_content_sha256": content.hexdigest(), "pip_version": packages.get("pip")}, sort_keys=True, separators=(",", ":")))
'''


def _installed_environment_attestation(python: Path) -> dict[str, str]:
    script = INSTALLED_ENVIRONMENT_ATTESTATION_SCRIPT
    result = subprocess.run(
        [str(python), "-B", "-I", "-S", "-c", script],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if result.returncode != 0:
        raise LifecycleError(
            "installed service environment attestation failed: "
            + (result.stderr or result.stdout or "environment probe failed")[-2000:]
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleError("installed service environment attestation was invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "dependency_set_sha256",
        "installed_content_sha256",
        "pip_version",
    }:
        raise LifecycleError("installed service environment attestation schema is invalid")
    for key in ("dependency_set_sha256", "installed_content_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(value.get(key, ""))):
            raise LifecycleError("installed service environment attestation hash is invalid")
    if value.get("pip_version") != "26.1.2":
        raise LifecycleError("service runtime did not activate supported pip 26.1.2")
    return {key: str(value[key]) for key in value}


def _artifact_set_hash(
    report_path: Path,
    expected_inventory: Mapping[str, str],
    expected_hashes: Mapping[str, frozenset[str]],
) -> str:
    raw = read_bounded_json(report_path, maximum=16 * 1024 * 1024)
    installed = raw.get("install")
    if not isinstance(installed, list) or not installed:
        raise LifecycleError("pip artifact report is empty")
    artifacts: list[dict[str, str]] = []
    for item in installed:
        if not isinstance(item, dict):
            raise LifecycleError("pip artifact report entry is invalid")
        metadata = item.get("metadata")
        download = item.get("download_info")
        if not isinstance(metadata, dict) or not isinstance(download, dict):
            raise LifecycleError("pip artifact report entry is incomplete")
        archive = download.get("archive_info")
        hashes = archive.get("hashes") if isinstance(archive, dict) else None
        url = download.get("url")
        name = re.sub(r"[-_.]+", "-", str(metadata.get("name", ""))).lower()
        version = metadata.get("version")
        wheel_hash = hashes.get("sha256") if isinstance(hashes, dict) else None
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,199}", name)
            or not isinstance(version, str)
            or not version
            or not isinstance(wheel_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", wheel_hash)
            or not isinstance(url, str)
            or not 1 <= len(url) <= 4096
            or any(character.isspace() or ord(character) < 33 for character in url)
            or not url.split("?", 1)[0].split("#", 1)[0].casefold().endswith(".whl")
        ):
            raise LifecycleError("pip artifact report identity is malformed")
        if name not in expected_hashes or wheel_hash not in expected_hashes[name]:
            raise LifecycleError("pip artifact report hash is not selected by the lock")
        artifacts.append({"name": name, "version": version, "sha256": wheel_hash})
    artifacts.sort(key=lambda item: (item["name"], item["version"], item["sha256"]))
    if len({item["name"] for item in artifacts}) != len(artifacts):
        raise LifecycleError("pip artifact report contains duplicate distributions")
    reported_inventory = {item["name"]: item["version"] for item in artifacts}
    if reported_inventory != dict(expected_inventory):
        raise LifecycleError("pip artifact report does not exactly match the selected lock")
    return sha256_bytes(
        json.dumps(artifacts, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    )


def _validate_runtime_attestation(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(raw)
    _exact_keys(value, RUNTIME_ATTESTATION_KEYS, "runtime attestation")
    value["contract"] = _validate_runtime_contract(value.get("contract", {}))
    if value.get("lock_file") != _lock_for_contract(value["contract"]):
        raise LifecycleError("runtime attestation lock does not match its interpreter")
    if value.get("pip_version") != "26.1.2":
        raise LifecycleError("runtime attestation pip version is unsupported")
    for key in (
        "lock_sha256",
        "dependency_set_sha256",
        "artifact_set_sha256",
        "installed_content_sha256",
        "identity_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(value.get(key, ""))):
            raise LifecycleError(f"runtime attestation {key} is invalid")
    identity_payload = {key: value[key] for key in value if key != "identity_sha256"}
    if sha256_json(identity_payload) != value["identity_sha256"]:
        raise LifecycleError("runtime attestation identity hash is inconsistent")
    return value


def _provision_release(
    temporary: Path,
    expected_contract: Mapping[str, Any],
    lock_relative: str,
    lock_sha256: str,
) -> dict[str, Any]:
    temporary_python = _release_python(temporary)
    try:
        venv.EnvBuilder(with_pip=False, clear=False, symlinks=False).create(
            temporary / ".venv"
        )
    except Exception as exc:
        raise LifecycleError(
            "private service environment creation failed: " + str(exc)[-2000:]
        ) from exc
    contract = _interpreter_contract(temporary_python)
    if contract != dict(expected_contract):
        raise LifecycleError("provisioned interpreter does not match the selected lock runtime")
    lock_path = temporary / Path(lock_relative)
    if sha256_file(lock_path) != lock_sha256:
        raise LifecycleError("staged dependency lock changed before installation")
    locked_inventory, locked_hashes, locked_inventory_hash = _lock_inventory(lock_path)
    report_path = temporary / "install" / ".pip-report.json"
    result = subprocess.run(
        _bootstrap_pip_command(
            temporary_python,
            (
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-compile",
            "--require-hashes",
            "--only-binary=:all:",
            "--upgrade",
            "--force-reinstall",
            "--report",
            str(report_path),
            "--requirement",
            str(lock_path),
            ),
        ),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if result.returncode != 0:
        raise LifecycleError(
            f"hashed service dependency installation failed (exit {result.returncode}): "
            + (result.stderr or result.stdout or "pip bootstrap failed")[-2000:]
        )
    pip_probe = subprocess.run(
        _private_runtime_command(
            temporary_python, module="pip", arguments=("--version",)
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if pip_probe.returncode != 0 or not pip_probe.stdout.startswith("pip 26.1.2 "):
        raise LifecycleError("service runtime did not activate supported pip 26.1.2")
    pip_check = subprocess.run(
        _private_runtime_command(
            temporary_python,
            module="pip",
            arguments=("check", "--disable-pip-version-check"),
        ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if pip_check.returncode != 0:
        raise LifecycleError(
            "service dependency consistency check failed: "
            + (pip_check.stderr or pip_check.stdout or "pip check failed")[-2000:]
        )
    probe = subprocess.run(
        _private_runtime_command(
            temporary_python,
            code="import bs4, filetype, openpyxl, pypdfium2; import chandra",
        ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if probe.returncode != 0:
        raise LifecycleError(
            "service runtime verification failed: "
            + (probe.stderr or probe.stdout or "dependency probe failed")[-2000:]
        )
    _remove_distribution_entrypoints(temporary_python)
    _prepare_deterministic_bytecode(temporary_python, temporary / ".venv")
    try:
        artifact_hash = _artifact_set_hash(report_path, locked_inventory, locked_hashes)
    finally:
        report_path.unlink(missing_ok=True)
    environment = _installed_environment_attestation(temporary_python)
    if environment["dependency_set_sha256"] != locked_inventory_hash:
        raise LifecycleError("installed service inventory does not exactly match the selected lock")
    attestation: dict[str, Any] = {
        "contract": contract,
        "lock_file": lock_relative,
        "lock_sha256": lock_sha256,
        "pip_version": environment["pip_version"],
        "dependency_set_sha256": environment["dependency_set_sha256"],
        "artifact_set_sha256": artifact_hash,
        "installed_content_sha256": environment["installed_content_sha256"],
    }
    attestation["identity_sha256"] = sha256_json(attestation)
    return _validate_runtime_attestation(attestation)


def _stage_paths(
    runtime: ProfileRuntime, snapshot: ReleaseSourceSnapshot
) -> tuple[Path, Path]:
    # Keep provisioning paths short for Windows installations where the machine
    # has not enabled the optional long-path registry policy.  The stage remains
    # profile-owned and on the same volume as ``releases`` so final publication
    # is still one atomic directory rename.
    name = f".s-{snapshot.source_hash[:12]}"
    stage = _under(runtime.plugin_root, runtime.plugin_root / name, "release stage")
    marker = _under(
        runtime.plugin_root,
        runtime.plugin_root / f"{name}.json",
        "release stage marker",
    )
    return stage, marker


def _stage_marker(
    runtime: ProfileRuntime, snapshot: ReleaseSourceSnapshot, stage: Path
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "plugin": PLUGIN_ID,
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "source_hash": snapshot.source_hash,
        "stage_path": str(stage),
        "started_at": _utc_now(),
    }


def _validate_stage_marker(
    runtime: ProfileRuntime,
    snapshot: ReleaseSourceSnapshot,
    stage: Path,
    marker: Mapping[str, Any],
) -> None:
    _exact_keys(marker, STAGE_MARKER_KEYS, "release stage marker")
    for key, expected in (
        ("schema", SCHEMA_VERSION),
        ("plugin", PLUGIN_ID),
        ("profile", runtime.profile_name),
        ("profile_fingerprint", runtime.fingerprint),
        ("owner_id", runtime.owner_id),
        ("source_hash", snapshot.source_hash),
        ("stage_path", str(stage)),
    ):
        if marker.get(key) != expected:
            raise LifecycleError(f"release stage marker {key} is not owned by this install")
    if not isinstance(marker.get("started_at"), str) or not str(marker["started_at"]).endswith("Z"):
        raise LifecycleError("release stage marker timestamp is invalid")


def _remove_owned_stage(stage: Path) -> None:
    if not os.path.lexists(stage):
        return
    info = os.lstat(stage)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse_flag)
    ):
        raise LifecycleError("owned release stage is not a regular directory; manual recovery required")
    entries = 0
    for directory, names, files in os.walk(stage, topdown=True, followlinks=False):
        for name in [*names, *files]:
            entries += 1
            if entries > MAX_STAGE_ENTRIES:
                raise LifecycleError("owned release stage is unexpectedly broad; manual recovery required")
            candidate = Path(directory) / name
            candidate_info = os.lstat(candidate)
            if stat.S_ISLNK(candidate_info.st_mode) or bool(
                getattr(candidate_info, "st_file_attributes", 0) & reparse_flag
            ):
                raise LifecycleError(
                    "owned release stage contains a link/reparse point; manual recovery required"
                )
    shutil.rmtree(stage)


def _recover_owned_stage(
    runtime: ProfileRuntime,
    snapshot: ReleaseSourceSnapshot,
    stage: Path,
    marker_path: Path,
) -> None:
    marker_exists = os.path.lexists(marker_path)
    stage_exists = os.path.lexists(stage)
    if stage_exists and not marker_exists:
        raise LifecycleError(
            f"unreceipted release stage requires manual inspection: {stage}"
        )
    if not marker_exists:
        return
    marker = read_bounded_json(marker_path, maximum=MAX_RECEIPT_BYTES)
    _validate_stage_marker(runtime, snapshot, stage, marker)
    _remove_owned_stage(stage)
    marker_path.unlink()


def stage_release(runtime: ProfileRuntime, source_root: Path, *, provision: bool) -> Release:
    if not provision:
        raise LifecycleError("an installable release requires an isolated provisioned runtime")
    snapshot = capture_release_source(source_root)
    expected_contract = _current_runtime_contract()
    lock_relative = _lock_for_contract(expected_contract)
    lock_sha256 = snapshot.hashes[lock_relative]
    runtime.releases_dir.mkdir(parents=True, exist_ok=True)
    temporary, marker_path = _stage_paths(runtime, snapshot)
    _recover_owned_stage(runtime, snapshot, temporary, marker_path)
    marker = _stage_marker(runtime, snapshot, temporary)
    atomic_write_json(marker_path, marker)
    try:
        temporary.mkdir()
    except Exception:
        marker_path.unlink(missing_ok=True)
        raise
    try:
        for relative in RELEASE_FILES:
            destination = temporary / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(destination, snapshot.data(relative), mode=0o644)
            if sha256_file(destination) != snapshot.hashes[relative]:
                raise LifecycleError(f"staged release content verification failed: {relative}")
        runtime_attestation = _provision_release(
            temporary,
            expected_contract,
            lock_relative,
            lock_sha256,
        )
        runtime_identity = str(runtime_attestation["identity_sha256"])
        release_id = (
            f"{PLUGIN_VERSION}-{snapshot.source_hash[:12]}-{runtime_identity[:12]}"
        )
        root = _under(runtime.releases_dir, runtime.releases_dir / release_id, "release directory")
        manifest = {
            "schema": SCHEMA_VERSION,
            "plugin": PLUGIN_ID,
            "version": PLUGIN_VERSION,
            "release_id": release_id,
            "source_hash": snapshot.source_hash,
            "source_files": snapshot.hashes,
            "runtime_attestation": runtime_attestation,
            "provisioned": True,
        }
        if root.exists():
            existing = read_bounded_json(root / "release.json", maximum=MAX_RECEIPT_BYTES)
            if existing != manifest:
                raise LifecycleError(f"existing release receipt is inconsistent: {root}")
            for relative in RELEASE_FILES:
                installed = root / Path(relative)
                if (
                    not installed.is_file()
                    or installed.is_symlink()
                    or sha256_file(installed) != snapshot.hashes[relative]
                ):
                    raise LifecycleError(f"existing release content is inconsistent: {relative}")
            existing_python = _release_python(root)
            if not existing_python.is_file():
                raise LifecycleError(f"existing release runtime is incomplete: {root}")
            if _interpreter_contract(existing_python) != runtime_attestation["contract"]:
                raise LifecycleError(f"existing release interpreter is inconsistent: {root}")
            existing_environment = _installed_environment_attestation(existing_python)
            for key in ("pip_version", "dependency_set_sha256", "installed_content_sha256"):
                if existing_environment[key] != runtime_attestation[key]:
                    raise LifecycleError(f"existing release environment is inconsistent: {root}")
        else:
            for relative in RELEASE_FILES:
                if sha256_file(temporary / Path(relative)) != snapshot.hashes[relative]:
                    raise LifecycleError(f"staged release changed before publication: {relative}")
            if _interpreter_contract(_release_python(temporary)) != runtime_attestation["contract"]:
                raise LifecycleError("staged service interpreter changed before publication")
            final_environment = _installed_environment_attestation(_release_python(temporary))
            for key in ("pip_version", "dependency_set_sha256", "installed_content_sha256"):
                if final_environment[key] != runtime_attestation[key]:
                    raise LifecycleError("staged service environment changed before publication")
            atomic_write_json(temporary / "release.json", manifest)
            if read_bounded_json(temporary / "release.json", maximum=MAX_RECEIPT_BYTES) != manifest:
                raise LifecycleError("staged release manifest verification failed")
            os.replace(temporary, root)
        entry = root / "install" / "profile_service.py"
        python = _release_python(root)
        return Release(
            release_id=release_id,
            source_hash=snapshot.source_hash,
            root=root,
            entry=entry,
            python=python,
            desktop_data=snapshot.data(DESKTOP_RELEASE_FILE),
            desktop_sha256=snapshot.hashes[DESKTOP_RELEASE_FILE],
            runtime_attestation=runtime_attestation,
        )
    finally:
        if os.path.lexists(temporary) or os.path.lexists(marker_path):
            _recover_owned_stage(runtime, snapshot, temporary, marker_path)


def _instance_id(runtime: ProfileRuntime, release: Release) -> str:
    return hashlib.sha256(
        f"{runtime.owner_id}\0{release.release_id}".encode("utf-8")
    ).hexdigest()[:32]


def build_service_config(runtime: ProfileRuntime, release: Release) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "plugin": PLUGIN_ID,
        "version": PLUGIN_VERSION,
        "api_version": SERVICE_API_VERSION,
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "instance_id": _instance_id(runtime, release),
        "hermes_home": str(runtime.home),
        "plugin_root": str(runtime.plugin_root),
        "data_root": str(runtime.data_root),
        "inbox": str(runtime.inbox),
        "processed": str(runtime.processed),
        "jobs": str(runtime.jobs),
        "state": str(runtime.state),
        "logs": str(runtime.logs),
        "bind": "127.0.0.1",
        "port": runtime.port,
        "token_file": str(runtime.token_file),
        "release_id": release.release_id,
        "release_root": str(release.root),
        "service_entry": str(release.entry),
        "runtime_python": str(release.python),
        "task_name": runtime.task_name,
    }


def validate_service_config(
    runtime: ProfileRuntime,
    config: Mapping[str, Any],
    *,
    require_current_version: bool = False,
) -> dict[str, Any]:
    _exact_keys(config, SERVICE_CONFIG_KEYS, "service config")
    expected_identity = {
        "schema": SCHEMA_VERSION,
        "plugin": PLUGIN_ID,
        "api_version": SERVICE_API_VERSION,
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "hermes_home": str(runtime.home),
        "plugin_root": str(runtime.plugin_root),
        "data_root": str(runtime.data_root),
        "inbox": str(runtime.inbox),
        "processed": str(runtime.processed),
        "jobs": str(runtime.jobs),
        "state": str(runtime.state),
        "logs": str(runtime.logs),
        "bind": "127.0.0.1",
        "port": runtime.port,
        "token_file": str(runtime.token_file),
        "task_name": runtime.task_name,
    }
    for key, expected in expected_identity.items():
        if config.get(key) != expected:
            raise LifecycleError(f"service config {key} does not belong to the selected profile")
    if not isinstance(config.get("version"), str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
        str(config.get("version", "")),
    ):
        raise LifecycleError("service config version is invalid")
    if require_current_version and config.get("version") != PLUGIN_VERSION:
        raise LifecycleError("service config version is not the installer's version")
    release_id = str(config.get("release_id", ""))
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,96}", release_id):
        raise LifecycleError("service config release_id is invalid")
    release_root = _under(runtime.releases_dir, Path(str(config["release_root"])), "release_root")
    entry = _under(release_root, Path(str(config["service_entry"])), "service_entry")
    runtime_python = _under(release_root, Path(str(config["runtime_python"])), "runtime_python")
    if release_root.name != release_id:
        raise LifecycleError("release directory and release_id disagree")
    if not entry.is_file() or not runtime_python.is_file():
        raise LifecycleError("configured service release is incomplete")
    manifest = read_bounded_json(
        _under(release_root, release_root / "release.json", "release manifest"),
        maximum=MAX_RECEIPT_BYTES,
    )
    _exact_keys(
        manifest,
        RELEASE_MANIFEST_KEYS,
        "release manifest",
    )
    source_files = manifest.get("source_files")
    runtime_raw = manifest.get("runtime_attestation")
    if not isinstance(runtime_raw, Mapping):
        raise LifecycleError("release manifest runtime attestation is invalid")
    runtime_attestation = _validate_runtime_attestation(runtime_raw)
    if (
        manifest["schema"] != SCHEMA_VERSION
        or manifest["plugin"] != PLUGIN_ID
        or manifest["version"] != config["version"]
        or manifest["release_id"] != release_id
        or manifest["provisioned"] is not True
        or not re.fullmatch(r"[0-9a-f]{64}", str(manifest["source_hash"]))
        or not isinstance(source_files, dict)
        or set(source_files) != set(RELEASE_SOURCE_FILES)
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in source_files.values()
        )
    ):
        raise LifecycleError("release manifest does not attest the configured service")
    if source_files[runtime_attestation["lock_file"]] != runtime_attestation["lock_sha256"]:
        raise LifecycleError("release manifest lock bytes do not match runtime attestation")
    instance = str(config.get("instance_id", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", instance):
        raise LifecycleError("service config instance_id is invalid")
    return dict(config)


def _task_spec(runtime: ProfileRuntime, config: Mapping[str, Any]) -> TaskSpec:
    return TaskSpec(
        name=runtime.task_name,
        python=Path(str(config["runtime_python"])),
        entry=Path(str(config["service_entry"])),
        config=runtime.config_file,
        working_directory=Path(str(config["release_root"])),
    )


def _health_request(
    config: Mapping[str, Any],
    token: str,
    *,
    method: str = "GET",
    path: str = "/api/health",
    timeout: float = HEALTH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", int(config["port"]), timeout=timeout)
    try:
        connection.request(
            method,
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Document-Reader-Owner": str(config["owner_id"]),
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_HEALTH_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise LifecycleError(f"Document Reader service is unreachable: {exc}") from exc
    finally:
        connection.close()
    if len(body) > MAX_HEALTH_BYTES:
        raise LifecycleError("Document Reader health response is oversized")
    if response.status != 200:
        raise LifecycleError(f"Document Reader health rejected the request ({response.status})")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("Document Reader health returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise LifecycleError("Document Reader health must return a JSON object")
    return value


def attest_health(config: Mapping[str, Any], token: str) -> dict[str, Any]:
    health = _health_request(config, token)
    _exact_keys(health, HEALTH_KEYS, "service health")
    expected = {
        "status": "ok",
        "service": "hermes-document-reader",
        "version": config["version"],
        "api_version": config["api_version"],
        "profile_name": config["profile"],
        "owner_fingerprint": config["owner_id"],
        "instance_id": config["instance_id"],
        "port": config["port"],
    }
    for key, expected_value in expected.items():
        if health.get(key) != expected_value:
            raise LifecycleError(f"service health {key} does not match the selected profile")
    if not isinstance(health.get("pid"), int) or int(health["pid"]) <= 0:
        raise LifecycleError("service health PID is invalid")
    if not isinstance(health.get("started_at"), str) or not health["started_at"]:
        raise LifecycleError("service health start identity is invalid")
    return health


def _wait_for_health(config: Mapping[str, Any], token: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return attest_health(config, token)
        except LifecycleError as exc:
            last = exc
            time.sleep(0.25)
    raise LifecycleError(f"service did not become healthy: {last}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _service_lock_available(config: Mapping[str, Any]) -> bool:
    lock_path = Path(str(config["plugin_root"])) / "runtime" / "service.lock"
    if not lock_path.is_file() or lock_path.is_symlink():
        return False
    handle = lock_path.open("r+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        return True
    except (BlockingIOError, OSError):
        return False
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _shutdown_attested(config: Mapping[str, Any], token: str) -> None:
    health = attest_health(config, token)
    old_pid = int(health["pid"])
    if _service_lock_available(config):
        raise LifecycleError("healthy service does not hold the expected profile owner lock")
    _health_request(config, token, method="POST", path="/api/shutdown")
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    port = int(config["port"])
    while time.monotonic() < deadline:
        if (
            not loopback_port_open(port)
            and not _pid_alive(old_pid)
            and _service_lock_available(config)
        ):
            return
        time.sleep(0.2)
    raise LifecycleError(
        f"attested service PID {old_pid} did not exit cleanly; refusing a forced termination"
    )


def _backup_file(source: Path, backup_dir: Path, name: str) -> str | None:
    if not os.path.lexists(source):
        return None
    info = source.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        source.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or (reparse and getattr(info, "st_file_attributes", 0) & reparse)
    ):
        raise LifecycleError(f"backup source must be a regular non-reparse file: {source}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / name
    shutil.copy2(source, destination)
    if sha256_file(source) != sha256_file(destination):
        destination.unlink(missing_ok=True)
        raise LifecycleError(f"backup verification failed for {source}")
    return str(destination)


def _file_hash_or_none(path: Path, label: str) -> str | None:
    if not os.path.lexists(path):
        return None
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or (reparse and getattr(info, "st_file_attributes", 0) & reparse)
    ):
        raise LifecycleError(f"{label} must be a regular non-reparse file")
    return sha256_file(path)


def _require_allowed_hash(
    path: Path,
    allowed: Iterable[str | None],
    label: str,
) -> str | None:
    actual = _file_hash_or_none(path, label)
    if actual not in set(allowed):
        raise LifecycleError(f"{label} changed outside the interrupted transaction")
    return actual


def _restore_snapshot_file(
    runtime: ProfileRuntime,
    destination: Path,
    snapshot: str | None,
    expected_sha: str | None,
    *,
    mode: int,
    label: str,
) -> None:
    if snapshot is None:
        destination.unlink(missing_ok=True)
        return
    source = _under(runtime.install_dir, Path(snapshot), label)
    if source.is_symlink() or not source.is_file() or sha256_file(source) != expected_sha:
        raise LifecycleError(f"{label} snapshot is invalid")
    atomic_write_bytes(destination, source.read_bytes(), mode=mode)
    if sha256_file(destination) != expected_sha:
        raise LifecycleError(f"{label} snapshot restore verification failed")


def _read_optional_json(path: Path, *, maximum: int = MAX_RECEIPT_BYTES) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    try:
        return read_bounded_json(path, maximum=maximum)
    except ProfileRuntimeError as exc:
        raise LifecycleError(str(exc)) from exc


def _validate_desktop_receipt(runtime: ProfileRuntime, receipt: Mapping[str, Any]) -> dict[str, Any]:
    _exact_keys(receipt, DESKTOP_RECEIPT_KEYS, "desktop receipt")
    for key, expected in (
        ("schema", SCHEMA_VERSION),
        ("plugin", PLUGIN_ID),
        ("profile", runtime.profile_name),
        ("profile_fingerprint", runtime.fingerprint),
        ("owner_id", runtime.owner_id),
    ):
        if receipt.get(key) != expected:
            raise LifecycleError(f"desktop receipt {key} does not belong to this profile")
    for key in ("installed_sha256", "source_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(key, ""))):
            raise LifecycleError(f"desktop receipt {key} is invalid")
    if not isinstance(receipt.get("version"), str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
        str(receipt.get("version", "")),
    ):
        raise LifecycleError("desktop receipt version is invalid")
    if not re.fullmatch(
        r"[0-9A-Za-z._-]{1,96}", str(receipt.get("release_id", ""))
    ):
        raise LifecycleError("desktop receipt release is invalid")
    if not isinstance(receipt.get("installed_at"), str) or not receipt["installed_at"].endswith("Z"):
        raise LifecycleError("desktop receipt timestamp is invalid")
    for key in ("previous_plugin", "previous_receipt"):
        value = receipt.get(key)
        if value is not None:
            previous = _under(runtime.install_dir, Path(str(value)), key)
            if previous.is_symlink() or not previous.is_file():
                raise LifecycleError(f"desktop receipt {key} backup is missing")
    return dict(receipt)


def deploy_desktop_plugin(
    runtime: ProfileRuntime,
    data: bytes,
    source_sha: str,
    release_id: str,
    backup_dir: Path,
) -> dict[str, Any]:
    if not isinstance(data, bytes) or sha256_bytes(data) != source_sha:
        raise LifecycleError("desktop release snapshot does not match its source hash")
    prior_receipt = _read_optional_json(runtime.desktop_receipt)
    if prior_receipt is not None:
        _validate_desktop_receipt(runtime, prior_receipt)

    previous_plugin: str | None = None
    previous_receipt: str | None = None
    if runtime.desktop_plugin.exists():
        current_sha = sha256_file(runtime.desktop_plugin)
        if prior_receipt is None:
            if current_sha not in LEGACY_DESKTOP_HASHES and current_sha != source_sha:
                raise LifecycleError(
                    "desktop-plugin/document-reader is unowned; refusing to overwrite foreign bytes"
                )
        elif current_sha != prior_receipt["installed_sha256"]:
            raise LifecycleError("installed desktop plugin changed after its ownership receipt")
        previous_plugin = _backup_file(runtime.desktop_plugin, backup_dir, "desktop-plugin.js")
    if runtime.desktop_receipt.exists():
        previous_receipt = _backup_file(runtime.desktop_receipt, backup_dir, "desktop-receipt.json")

    atomic_write_bytes(runtime.desktop_plugin, data, mode=0o644)
    installed_sha = sha256_file(runtime.desktop_plugin)
    if installed_sha != source_sha:
        raise LifecycleError("desktop plugin atomic deployment verification failed")
    receipt = {
        "schema": SCHEMA_VERSION,
        "plugin": PLUGIN_ID,
        "version": PLUGIN_VERSION,
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "release_id": release_id,
        "installed_sha256": installed_sha,
        "source_sha256": source_sha,
        "installed_at": _utc_now(),
        "previous_plugin": previous_plugin,
        "previous_receipt": previous_receipt,
    }
    atomic_write_json(runtime.desktop_receipt, receipt)
    return receipt


def rollback_desktop_plugin(runtime: ProfileRuntime) -> None:
    receipt = _read_optional_json(runtime.desktop_receipt)
    if receipt is None:
        raise LifecycleError("desktop plugin has no ownership receipt")
    receipt = _validate_desktop_receipt(runtime, receipt)
    if (
        runtime.desktop_plugin.is_symlink()
        or not runtime.desktop_plugin.is_file()
        or sha256_file(runtime.desktop_plugin) != receipt["installed_sha256"]
    ):
        raise LifecycleError("desktop plugin changed after install; refusing rollback")

    previous_plugin = receipt["previous_plugin"]
    if previous_plugin is None:
        runtime.desktop_plugin.unlink()
    else:
        previous_path = _under(runtime.install_dir, Path(previous_plugin), "previous desktop plugin")
        atomic_write_bytes(runtime.desktop_plugin, previous_path.read_bytes(), mode=0o644)
    previous_receipt = receipt["previous_receipt"]
    if previous_receipt is None:
        runtime.desktop_receipt.unlink(missing_ok=True)
    else:
        previous_path = _under(runtime.install_dir, Path(previous_receipt), "previous desktop receipt")
        atomic_write_bytes(runtime.desktop_receipt, previous_path.read_bytes(), mode=0o600)


def _legacy_files(legacy_root: Path, runtime: ProfileRuntime) -> list[Path]:
    source_root = _canonical(legacy_root)
    if not source_root.is_dir():
        raise LifecycleError(f"legacy inbox does not exist: {source_root}")
    data_root = _canonical(runtime.data_root)
    try:
        source_root.relative_to(data_root)
        nested = True
    except ValueError:
        nested = False
    try:
        data_root.relative_to(source_root)
        contains_data = True
    except ValueError:
        contains_data = False
    if nested or contains_data or source_root == Path(source_root.anchor):
        raise LifecycleError("legacy inbox must be a narrow tree outside profile data")
    root_info = source_root.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if source_root.is_symlink() or (
        reparse_flag and getattr(root_info, "st_file_attributes", 0) & reparse_flag
    ):
        raise LifecycleError("legacy inbox must not be a link or reparse point")

    found: list[Path] = []
    total = 0
    pending = [source_root]
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            attributes = getattr(info, "st_file_attributes", 0)
            if entry.is_symlink() or (reparse_flag and attributes & reparse_flag):
                raise LifecycleError(f"legacy adoption refuses link/reparse traversal: {path}")
            if entry.is_dir(follow_symlinks=False):
                relative = path.relative_to(source_root)
                if len(relative.parts) > 32:
                    raise LifecycleError("legacy inbox nesting exceeds 32 directories")
                pending.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise LifecycleError(f"legacy adoption accepts regular files only: {path}")
            relative = path.relative_to(source_root)
            processed = bool(relative.parts and relative.parts[0].casefold() == "processed")
            allowed = LEGACY_INPUT_SUFFIXES | (LEGACY_OUTPUT_SUFFIXES if processed else set())
            if path.suffix.casefold() not in allowed:
                raise LifecycleError(f"legacy adoption refuses unsupported file: {relative}")
            maximum = (
                MAX_LEGACY_PROCESSED_BYTES if processed else MAX_LEGACY_INPUT_BYTES
            )
            if info.st_size <= 0 or info.st_size > maximum:
                raise LifecycleError(f"legacy file size is unsafe: {relative}")
            found.append(path)
            total += info.st_size
            if len(found) > MAX_LEGACY_FILES or total > MAX_LEGACY_BYTES:
                raise LifecycleError("legacy inbox exceeds the bounded adoption budget")
    return sorted(found)


def stage_legacy_documents(
    legacy_root: Path,
    runtime: ProfileRuntime,
    stage_root: Path,
) -> list[dict[str, Any]]:
    """Copy/verify a bounded legacy tree without publishing into the live inbox."""

    source_root = _canonical(legacy_root)
    stage_root = _under(runtime.install_dir, stage_root, "legacy staging directory")
    if stage_root.exists():
        raise LifecycleError("legacy staging directory already exists")
    stage_root.mkdir(parents=True)
    plan: list[dict[str, Any]] = []
    for index, source in enumerate(_legacy_files(source_root, runtime)):
        relative = source.relative_to(source_root)
        processed = bool(relative.parts and relative.parts[0].casefold() == "processed")
        destination_relative = (
            Path("processed").joinpath(*relative.parts[1:])
            if processed
            else Path("inbox") / relative
        )
        destination = _under(
            runtime.data_root,
            runtime.data_root / destination_relative,
            "legacy destination",
        )
        source_sha = sha256_file(source)
        if destination.exists():
            if destination.is_symlink() or not destination.is_file() or sha256_file(destination) != source_sha:
                raise LifecycleError(f"legacy destination collision: {destination}")
            plan.append({"source": source, "staged": None, "destination": destination, "sha256": source_sha})
            continue
        staged = stage_root / f"{index:04d}-{source_sha}"
        shutil.copy2(source, staged)
        if sha256_file(staged) != source_sha:
            raise LifecycleError(f"legacy copy verification failed: {source}")
        plan.append({"source": source, "staged": staged, "destination": destination, "sha256": source_sha})
    return plan


def publish_legacy_documents(plan: Iterable[Mapping[str, Any]], runtime: ProfileRuntime) -> dict[str, int]:
    entries = [dict(item) for item in plan]
    # Complete collision preflight before publishing the first file.
    for item in entries:
        destination = _under(runtime.data_root, Path(item["destination"]), "legacy destination")
        if destination.exists() and (
            destination.is_symlink()
            or not destination.is_file()
            or sha256_file(destination) != item["sha256"]
        ):
            raise LifecycleError(f"legacy destination collision: {destination}")
    copied = skipped = 0
    for item in entries:
        staged = item["staged"]
        destination = Path(item["destination"])
        if staged is None or destination.exists():
            skipped += 1
            continue
        staged_path = Path(staged)
        if not staged_path.is_file() or sha256_file(staged_path) != item["sha256"]:
            raise LifecycleError("legacy staged file changed before publication")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(staged_path, destination)
        except FileExistsError:
            if not destination.is_file() or sha256_file(destination) != item["sha256"]:
                raise LifecycleError(f"legacy destination raced with publication: {destination}")
            skipped += 1
            continue
        if sha256_file(destination) != item["sha256"]:
            destination.unlink(missing_ok=True)
            raise LifecycleError("legacy atomic publication verification failed")
        staged_path.unlink()
        copied += 1
    return {"copied": copied, "skipped": skipped}


def adopt_legacy_documents(legacy_root: Path, runtime: ProfileRuntime) -> dict[str, int]:
    stage = runtime.install_dir / "legacy-adoption-stage"
    plan = stage_legacy_documents(legacy_root, runtime, stage)
    return publish_legacy_documents(plan, runtime)


def _validate_deployment(runtime: ProfileRuntime, receipt: Mapping[str, Any]) -> dict[str, Any]:
    _exact_keys(receipt, DEPLOYMENT_KEYS, "deployment receipt")
    for key, expected in (
        ("schema", SCHEMA_VERSION),
        ("plugin", PLUGIN_ID),
        ("profile", runtime.profile_name),
        ("profile_fingerprint", runtime.fingerprint),
        ("owner_id", runtime.owner_id),
        ("task_name", runtime.task_name),
        ("port", runtime.port),
    ):
        if receipt.get(key) != expected:
            raise LifecycleError(f"deployment receipt {key} does not belong to this profile")
    for key in ("source_hash", "service_config_sha256", "desktop_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(key, ""))):
            raise LifecycleError(f"deployment receipt {key} is invalid")
    if not isinstance(receipt.get("version"), str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
        str(receipt.get("version", "")),
    ):
        raise LifecycleError("deployment receipt version is invalid")
    if not re.fullmatch(
        r"[0-9A-Za-z._-]{1,96}", str(receipt.get("release_id", ""))
    ):
        raise LifecycleError("deployment receipt release is invalid")
    if not isinstance(receipt.get("installed_at"), str) or not receipt["installed_at"].endswith("Z"):
        raise LifecycleError("deployment receipt timestamp is invalid")
    if (receipt.get("previous_deployment") is None) != (
        receipt.get("previous_config") is None
    ):
        raise LifecycleError("deployment receipt previous authority is incomplete")
    for key in ("previous_deployment", "previous_config"):
        value = receipt.get(key)
        if value is not None:
            previous = _under(runtime.install_dir, Path(str(value)), key)
            if previous.is_symlink() or not previous.is_file():
                raise LifecycleError(f"deployment receipt {key} backup is missing")
    return dict(receipt)


def _validate_transaction(runtime: ProfileRuntime, value: Mapping[str, Any]) -> dict[str, Any]:
    operation = value.get("operation")
    if operation in {"rollback", "uninstall"}:
        _exact_keys(value, MAINTENANCE_TRANSACTION_KEYS, "lifecycle transaction")
        for key, expected in (
            ("schema", SCHEMA_VERSION),
            ("plugin", PLUGIN_ID),
            ("profile", runtime.profile_name),
            ("profile_fingerprint", runtime.fingerprint),
            ("owner_id", runtime.owner_id),
        ):
            if value.get(key) != expected:
                raise LifecycleError(
                    f"lifecycle transaction {key} does not belong to this profile"
                )
        if value.get("phase") not in {
            "prepared",
            "service_stopped",
            "task_removed",
            "desktop_changed",
            "authority_changed",
            "task_deployed",
            "started",
        }:
            raise LifecycleError("lifecycle transaction phase is invalid")
        for boolean_key in ("snapshot_task_exists", "snapshot_service_running"):
            if not isinstance(value.get(boolean_key), bool):
                raise LifecycleError(
                    f"lifecycle transaction {boolean_key} must be boolean"
                )
        if value["snapshot_service_running"] and not value["snapshot_task_exists"]:
            raise LifecycleError(
                "maintenance transaction cannot restore an unowned running service"
            )
        for path_key, sha_key in (
            ("snapshot_config", "snapshot_config_sha256"),
            ("snapshot_deployment", "snapshot_deployment_sha256"),
            ("snapshot_desktop_plugin", "snapshot_desktop_plugin_sha256"),
            ("snapshot_desktop_receipt", "snapshot_desktop_receipt_sha256"),
        ):
            path_value = value.get(path_key)
            sha_value = _validate_sha_or_none(value.get(sha_key), sha_key)
            if (path_value is None) != (sha_value is None):
                raise LifecycleError(
                    f"lifecycle transaction {path_key} backup identity is incomplete"
                )
            if path_value is not None:
                backup = _under(runtime.install_dir, Path(str(path_value)), path_key)
                if (
                    backup.is_symlink()
                    or not backup.is_file()
                    or sha256_file(backup) != sha_value
                ):
                    raise LifecycleError(
                        f"lifecycle transaction {path_key} backup is invalid"
                    )
        for sha_key in (
            "target_config_sha256",
            "target_deployment_sha256",
            "target_desktop_plugin_sha256",
            "target_desktop_receipt_sha256",
        ):
            _validate_sha_or_none(value.get(sha_key), sha_key)
        if value["snapshot_config"] is None:
            raise LifecycleError("maintenance transaction requires a config snapshot")
        return dict(value)

    _exact_keys(value, TRANSACTION_KEYS, "lifecycle transaction")
    for key, expected in (
        ("schema", SCHEMA_VERSION),
        ("plugin", PLUGIN_ID),
        ("profile", runtime.profile_name),
        ("profile_fingerprint", runtime.fingerprint),
        ("owner_id", runtime.owner_id),
        ("operation", "install"),
    ):
        if value.get(key) != expected:
            raise LifecycleError(f"lifecycle transaction {key} does not belong to this profile")
    if value.get("phase") not in {
        "prepared",
        "service_stopped",
        "desktop_deployed",
        "config_deployed",
        "task_deployed",
        "receipt_committed",
        "started",
    }:
        raise LifecycleError("lifecycle transaction phase is invalid")
    for boolean_key in ("previous_task_exists", "previous_service_running"):
        if not isinstance(value.get(boolean_key), bool):
            raise LifecycleError(f"lifecycle transaction {boolean_key} must be boolean")
    if value["previous_service_running"] and not value["previous_task_exists"]:
        raise LifecycleError(
            "install transaction cannot restore an unowned running service"
        )
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,96}", str(value.get("new_release_id", ""))):
        raise LifecycleError("lifecycle transaction release is invalid")
    _validate_sha_or_none(value.get("new_config_sha256"), "new config hash")
    _validate_sha_or_none(value.get("new_deployment_sha256"), "new deployment hash")
    if _validate_sha_or_none(
        value.get("new_desktop_plugin_sha256"), "new desktop plugin hash"
    ) is None:
        raise LifecycleError("install transaction requires a new desktop plugin hash")
    for path_key, sha_key in (
        ("previous_config", "previous_config_sha256"),
        ("previous_deployment", "previous_deployment_sha256"),
        ("previous_desktop_plugin", "previous_desktop_plugin_sha256"),
        ("previous_desktop_receipt", "previous_desktop_receipt_sha256"),
    ):
        path_value = value.get(path_key)
        sha_value = _validate_sha_or_none(value.get(sha_key), sha_key)
        if (path_value is None) != (sha_value is None):
            raise LifecycleError(f"lifecycle transaction {path_key} backup identity is incomplete")
        if path_value is not None:
            backup = _under(runtime.install_dir, Path(str(path_value)), path_key)
            if not backup.is_file() or sha256_file(backup) != sha_value:
                raise LifecycleError(f"lifecycle transaction {path_key} backup is invalid")
    return dict(value)


def _write_transaction(runtime: ProfileRuntime, value: Mapping[str, Any], phase: str) -> dict[str, Any]:
    updated = dict(value)
    updated["phase"] = phase
    atomic_write_json(runtime.transaction_journal, updated)
    return _validate_transaction(runtime, updated)


def _attest_task_result(result: Mapping[str, Any], *, allow_absent: bool) -> bool:
    exists = result.get("exists")
    matches = result.get("action_matches")
    if not isinstance(exists, bool) or not isinstance(matches, bool):
        raise LifecycleError("Task Scheduler status did not return ownership attestation")
    if exists and not matches:
        raise LifecycleError("scheduled task action is foreign; refusing lifecycle mutation")
    if not exists and not allow_absent:
        raise LifecycleError("owned scheduled task is missing")
    return exists


def _task_name_exists(result: Mapping[str, Any]) -> bool:
    exists = result.get("exists")
    if not isinstance(exists, bool):
        raise LifecycleError("Task Scheduler name probe did not return existence")
    return exists


def _remove_owned_task(
    tasks: TaskBackend,
    candidates: Iterable[TaskSpec],
) -> bool:
    unique: list[TaskSpec] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for candidate in candidates:
        identity = (
            candidate.name,
            str(candidate.python),
            str(candidate.entry),
            str(candidate.config),
            str(candidate.working_directory),
        )
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    observed_existing = False
    for candidate in unique:
        state = tasks.inspect(candidate)
        exists = state.get("exists")
        matches = state.get("action_matches")
        if not isinstance(exists, bool) or not isinstance(matches, bool):
            raise LifecycleError("Task Scheduler status did not return ownership attestation")
        if not exists:
            continue
        observed_existing = True
        if matches:
            tasks.remove(candidate)
            if _attest_task_result(tasks.inspect(candidate), allow_absent=True):
                raise LifecycleError("owned scheduled task remained after removal")
            return True
    if observed_existing:
        raise LifecycleError("scheduled task is foreign to every journalled action")
    return False


def _desktop_rollback_target(
    runtime: ProfileRuntime,
    *,
    require_receipt: bool,
) -> tuple[str | None, str | None]:
    current_plugin_sha = _file_hash_or_none(runtime.desktop_plugin, "desktop plugin")
    current_receipt_sha = _file_hash_or_none(runtime.desktop_receipt, "desktop receipt")
    raw = _read_optional_json(runtime.desktop_receipt)
    if raw is None:
        if require_receipt:
            raise LifecycleError("desktop plugin has no ownership receipt")
        return current_plugin_sha, current_receipt_sha
    receipt = _validate_desktop_receipt(runtime, raw)
    if current_plugin_sha != receipt["installed_sha256"]:
        raise LifecycleError("desktop plugin changed after its ownership receipt")
    previous_plugin_sha = None
    if receipt["previous_plugin"] is not None:
        previous_plugin = _under(
            runtime.install_dir,
            Path(receipt["previous_plugin"]),
            "previous desktop plugin",
        )
        previous_plugin_sha = _file_hash_or_none(
            previous_plugin, "previous desktop plugin"
        )
    previous_receipt_sha = None
    if receipt["previous_receipt"] is not None:
        previous_receipt = _under(
            runtime.install_dir,
            Path(receipt["previous_receipt"]),
            "previous desktop receipt",
        )
        previous_receipt_sha = _file_hash_or_none(
            previous_receipt, "previous desktop receipt"
        )
        previous = _validate_desktop_receipt(
            runtime, read_bounded_json(previous_receipt)
        )
        if previous["installed_sha256"] != previous_plugin_sha:
            raise LifecycleError(
                "previous desktop receipt does not attest the previous plugin bytes"
            )
    return previous_plugin_sha, previous_receipt_sha


def _desktop_uninstall_target(
    runtime: ProfileRuntime,
) -> tuple[Path | None, str | None, Path | None, str | None]:
    raw = _read_optional_json(runtime.desktop_receipt)
    if raw is None:
        return (
            runtime.desktop_plugin if runtime.desktop_plugin.exists() else None,
            _file_hash_or_none(runtime.desktop_plugin, "desktop plugin"),
            None,
            None,
        )
    receipt = _validate_desktop_receipt(runtime, raw)
    if _file_hash_or_none(runtime.desktop_plugin, "desktop plugin") != receipt["installed_sha256"]:
        raise LifecycleError("desktop plugin changed after its ownership receipt")
    visited: set[str] = set()
    for _ in range(64):
        previous_plugin = (
            None
            if receipt["previous_plugin"] is None
            else _under(
                runtime.install_dir,
                Path(receipt["previous_plugin"]),
                "previous desktop plugin",
            )
        )
        previous_plugin_sha = (
            None
            if previous_plugin is None
            else _file_hash_or_none(previous_plugin, "previous desktop plugin")
        )
        if receipt["previous_receipt"] is None:
            return previous_plugin, previous_plugin_sha, None, None
        previous_receipt = _under(
            runtime.install_dir,
            Path(receipt["previous_receipt"]),
            "previous desktop receipt",
        )
        identity = str(previous_receipt)
        if identity in visited:
            raise LifecycleError("desktop receipt history contains a cycle")
        visited.add(identity)
        previous_receipt_sha = _file_hash_or_none(
            previous_receipt, "previous desktop receipt"
        )
        receipt = _validate_desktop_receipt(
            runtime, read_bounded_json(previous_receipt)
        )
        if receipt["installed_sha256"] != previous_plugin_sha:
            raise LifecycleError(
                "desktop receipt history does not attest its plugin bytes"
            )
    raise LifecycleError("desktop receipt history exceeds 64 owned releases")


def _write_desktop_target(
    runtime: ProfileRuntime,
    plugin_source: Path | None,
    receipt_source: Path | None,
) -> None:
    if plugin_source is None:
        runtime.desktop_plugin.unlink(missing_ok=True)
    else:
        atomic_write_bytes(runtime.desktop_plugin, plugin_source.read_bytes(), mode=0o644)
    if receipt_source is None:
        runtime.desktop_receipt.unlink(missing_ok=True)
    else:
        atomic_write_bytes(runtime.desktop_receipt, receipt_source.read_bytes(), mode=0o600)


def _restore_install_desktop_snapshot(
    runtime: ProfileRuntime,
    transaction: Mapping[str, Any],
) -> None:
    current_plugin_sha = _file_hash_or_none(runtime.desktop_plugin, "desktop plugin")
    if current_plugin_sha not in {
        transaction["previous_desktop_plugin_sha256"],
        transaction["new_desktop_plugin_sha256"],
    }:
        raise LifecycleError("desktop plugin changed outside the install transaction")
    current_receipt_sha = _file_hash_or_none(runtime.desktop_receipt, "desktop receipt")
    if current_receipt_sha != transaction["previous_desktop_receipt_sha256"]:
        if current_receipt_sha is None:
            raise LifecycleError("desktop receipt disappeared outside the install transaction")
        receipt = _validate_desktop_receipt(
            runtime, read_bounded_json(runtime.desktop_receipt)
        )
        if (
            receipt["release_id"] != transaction["new_release_id"]
            or receipt["installed_sha256"]
            != transaction["new_desktop_plugin_sha256"]
        ):
            raise LifecycleError("desktop receipt is foreign to the install transaction")
        for receipt_key, transaction_key, label in (
            (
                "previous_plugin",
                "previous_desktop_plugin_sha256",
                "previous desktop plugin",
            ),
            (
                "previous_receipt",
                "previous_desktop_receipt_sha256",
                "previous desktop receipt",
            ),
        ):
            previous_path = receipt[receipt_key]
            previous_hash = (
                None
                if previous_path is None
                else _file_hash_or_none(
                    _under(runtime.install_dir, Path(previous_path), label), label
                )
            )
            if previous_hash != transaction[transaction_key]:
                raise LifecycleError(
                    "new desktop receipt does not attest the journalled prior bytes"
                )
    _restore_snapshot_file(
        runtime,
        runtime.desktop_plugin,
        transaction["previous_desktop_plugin"],
        transaction["previous_desktop_plugin_sha256"],
        mode=0o644,
        label="desktop plugin",
    )
    _restore_snapshot_file(
        runtime,
        runtime.desktop_receipt,
        transaction["previous_desktop_receipt"],
        transaction["previous_desktop_receipt_sha256"],
        mode=0o600,
        label="desktop receipt",
    )


def _maintenance_transaction(
    runtime: ProfileRuntime,
    *,
    operation: str,
    backup_dir: Path,
    target_config_sha256: str | None,
    target_deployment_sha256: str | None,
    target_desktop_plugin_sha256: str | None,
    target_desktop_receipt_sha256: str | None,
    task_exists: bool,
    service_running: bool,
) -> dict[str, Any]:
    snapshots = {
        "snapshot_config": _backup_file(
            runtime.config_file, backup_dir, "service.json"
        ),
        "snapshot_deployment": _backup_file(
            runtime.deployment_receipt, backup_dir, "deployment.json"
        ),
        "snapshot_desktop_plugin": _backup_file(
            runtime.desktop_plugin, backup_dir, "desktop-plugin.js"
        ),
        "snapshot_desktop_receipt": _backup_file(
            runtime.desktop_receipt, backup_dir, "desktop-receipt.json"
        ),
    }
    value: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "plugin": PLUGIN_ID,
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "operation": operation,
        "phase": "prepared",
        **snapshots,
        "target_config_sha256": target_config_sha256,
        "target_deployment_sha256": target_deployment_sha256,
        "target_desktop_plugin_sha256": target_desktop_plugin_sha256,
        "target_desktop_receipt_sha256": target_desktop_receipt_sha256,
        "snapshot_task_exists": task_exists,
        "snapshot_service_running": service_running,
        "started_at": _utc_now(),
    }
    for path_key in (
        "snapshot_config",
        "snapshot_deployment",
        "snapshot_desktop_plugin",
        "snapshot_desktop_receipt",
    ):
        snapshot = value[path_key]
        value[f"{path_key}_sha256"] = (
            sha256_file(Path(snapshot)) if snapshot is not None else None
        )
    atomic_write_json(runtime.transaction_journal, value)
    return _validate_transaction(runtime, value)


class LifecycleManager:
    def __init__(
        self,
        source_root: Path,
        *,
        task_backend: TaskBackend | None = None,
        runtime: ProfileRuntime | None = None,
    ):
        literal_source = Path(source_root).expanduser()
        if not literal_source.is_absolute() or ".." in literal_source.parts:
            raise LifecycleError("plugin source root must be an absolute normalized path")
        self.source_root = Path(os.path.abspath(literal_source))
        self._runtime = runtime
        self.task_backend = task_backend

    def runtime(self) -> ProfileRuntime:
        return self._runtime or resolve_profile_runtime()

    def _tasks(self) -> TaskBackend:
        if self.task_backend is not None:
            return self.task_backend
        return WindowsTaskBackend(self.source_root / "install" / "windows-task.ps1")

    def _load_config(self, runtime: ProfileRuntime, *, current: bool = False) -> dict[str, Any] | None:
        value = _read_optional_json(runtime.config_file)
        return None if value is None else validate_service_config(runtime, value, require_current_version=current)

    def install(
        self,
        *,
        provision: bool = True,
        start: bool = True,
        legacy_inbox: Path | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime()
        create_profile_directories(runtime)
        with profile_install_lock(runtime):
            if os.path.lexists(runtime.transaction_journal):
                raise LifecycleError(
                    "an interrupted lifecycle transaction needs `hermes document-reader recover`"
                )
            return self._install_locked(
                runtime,
                provision=provision,
                start=start,
                legacy_inbox=legacy_inbox,
            )

    def _install_locked(
        self,
        runtime: ProfileRuntime,
        *,
        provision: bool = True,
        start: bool = True,
        legacy_inbox: Path | None = None,
    ) -> dict[str, Any]:
        recover_engine_configuration(runtime)
        validate_engine_config(runtime)
        token = ensure_profile_token(runtime)
        release = stage_release(runtime, self.source_root, provision=provision)
        new_config = build_service_config(runtime, release)
        validate_service_config(runtime, new_config, require_current_version=True)
        previous_config = self._load_config(runtime)
        previous_deployment = _read_optional_json(runtime.deployment_receipt)
        if previous_deployment is not None:
            previous_deployment = _validate_deployment(runtime, previous_deployment)
            if previous_config is None:
                raise LifecycleError("deployment receipt exists without a service config")
            previous_desktop = _validate_desktop_receipt(
                runtime, read_bounded_json(runtime.desktop_receipt)
            )
            if (
                previous_deployment["release_id"] != previous_config["release_id"]
                or previous_deployment["service_config_sha256"]
                != sha256_file(runtime.config_file)
                or previous_deployment["desktop_sha256"]
                != previous_desktop["installed_sha256"]
                or previous_desktop["release_id"]
                != previous_deployment["release_id"]
                or runtime.desktop_plugin.is_symlink()
                or not runtime.desktop_plugin.is_file()
                or sha256_file(runtime.desktop_plugin)
                != previous_deployment["desktop_sha256"]
            ):
                raise LifecycleError(
                    "existing deployment authority is inconsistent; refusing update"
                )
        tasks = self._tasks()
        spec = _task_spec(runtime, new_config)
        previous_spec = _task_spec(runtime, previous_config) if previous_config else None
        if previous_spec is not None:
            previous_task_exists = _attest_task_result(
                tasks.inspect(previous_spec), allow_absent=True
            )
            if previous_deployment is None and previous_task_exists:
                raise LifecycleError(
                    "scheduled task exists without a deployment receipt; refusing update"
                )
        else:
            unexpected_task = _attest_task_result(tasks.inspect(spec), allow_absent=True)
            if unexpected_task:
                raise LifecycleError(
                    "scheduled task exists without an owned profile config; refusing adoption"
                )
            previous_task_exists = False

        listener_open = loopback_port_open(runtime.port)
        if listener_open:
            if previous_config is None or previous_deployment is None:
                raise LifecycleError(
                    f"port {runtime.port} is already in use without complete deployment authority"
                )
            attest_health(previous_config, token)
            if not previous_task_exists:
                raise LifecycleError("running service has no owned scheduled task")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_dir = _under(runtime.install_dir, runtime.install_dir / "backups" / stamp, "backup directory")
        previous_config_path = _backup_file(runtime.config_file, backup_dir, "service.json")
        previous_deployment_path = _backup_file(runtime.deployment_receipt, backup_dir, "deployment.json")
        previous_desktop_plugin_path = _backup_file(
            runtime.desktop_plugin, backup_dir, "transaction-desktop-plugin.js"
        )
        previous_desktop_receipt_path = _backup_file(
            runtime.desktop_receipt, backup_dir, "transaction-desktop-receipt.json"
        )
        new_desktop_plugin_sha = release.desktop_sha256
        transaction: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "plugin": PLUGIN_ID,
            "profile": runtime.profile_name,
            "profile_fingerprint": runtime.fingerprint,
            "owner_id": runtime.owner_id,
            "operation": "install",
            "phase": "prepared",
            "new_release_id": release.release_id,
            "new_config_sha256": sha256_json(new_config),
            "new_deployment_sha256": None,
            "previous_config": previous_config_path,
            "previous_config_sha256": (
                sha256_file(Path(previous_config_path)) if previous_config_path else None
            ),
            "previous_deployment": previous_deployment_path,
            "previous_deployment_sha256": (
                sha256_file(Path(previous_deployment_path))
                if previous_deployment_path
                else None
            ),
            "previous_desktop_plugin": previous_desktop_plugin_path,
            "previous_desktop_plugin_sha256": (
                sha256_file(Path(previous_desktop_plugin_path))
                if previous_desktop_plugin_path
                else None
            ),
            "previous_desktop_receipt": previous_desktop_receipt_path,
            "previous_desktop_receipt_sha256": (
                sha256_file(Path(previous_desktop_receipt_path))
                if previous_desktop_receipt_path
                else None
            ),
            "new_desktop_plugin_sha256": new_desktop_plugin_sha,
            "previous_task_exists": previous_task_exists,
            "previous_service_running": listener_open,
            "started_at": _utc_now(),
        }
        atomic_write_json(runtime.transaction_journal, transaction)
        _validate_transaction(runtime, transaction)

        try:
            legacy_plan: list[dict[str, Any]] | None = None
            if legacy_inbox is not None:
                legacy_plan = stage_legacy_documents(
                    legacy_inbox, runtime, backup_dir / "legacy-stage"
                )
            if listener_open:
                assert previous_config is not None
                _shutdown_attested(previous_config, token)
            transaction = _write_transaction(runtime, transaction, "service_stopped")
            if legacy_plan is not None:
                publish_legacy_documents(legacy_plan, runtime)
            if previous_task_exists:
                assert previous_spec is not None
                tasks.remove(previous_spec)
                if _attest_task_result(tasks.inspect(previous_spec), allow_absent=True):
                    raise LifecycleError("owned scheduled task remained after removal")
            desktop_receipt = deploy_desktop_plugin(
                runtime,
                release.desktop_data,
                release.desktop_sha256,
                release.release_id,
                backup_dir,
            )
            transaction = _write_transaction(runtime, transaction, "desktop_deployed")
            atomic_write_json(runtime.config_file, new_config)
            if sha256_file(runtime.config_file) != transaction["new_config_sha256"]:
                raise LifecycleError("new service config hash did not match the transaction")
            transaction = _write_transaction(runtime, transaction, "config_deployed")
            tasks.install(spec)
            _attest_task_result(tasks.inspect(spec), allow_absent=False)
            transaction = _write_transaction(runtime, transaction, "task_deployed")
            receipt = {
                "schema": SCHEMA_VERSION,
                "plugin": PLUGIN_ID,
                "version": PLUGIN_VERSION,
                "profile": runtime.profile_name,
                "profile_fingerprint": runtime.fingerprint,
                "owner_id": runtime.owner_id,
                "release_id": release.release_id,
                "source_hash": release.source_hash,
                "service_config_sha256": sha256_file(runtime.config_file),
                "desktop_sha256": str(desktop_receipt["installed_sha256"]),
                "task_name": runtime.task_name,
                "port": runtime.port,
                "installed_at": _utc_now(),
                "previous_deployment": (
                    previous_deployment_path if previous_deployment is not None else None
                ),
                "previous_config": (
                    previous_config_path if previous_deployment is not None else None
                ),
            }
            transaction["new_deployment_sha256"] = sha256_json(receipt)
            atomic_write_json(runtime.transaction_journal, transaction)
            atomic_write_json(runtime.deployment_receipt, receipt)
            if sha256_file(runtime.deployment_receipt) != transaction["new_deployment_sha256"]:
                raise LifecycleError("new deployment receipt hash did not match the transaction")
            transaction = _write_transaction(runtime, transaction, "receipt_committed")
            health: dict[str, Any] | None = None
            if start:
                tasks.start(spec)
                health = _wait_for_health(new_config, token, START_TIMEOUT_SECONDS)
                transaction = _write_transaction(runtime, transaction, "started")
            runtime.transaction_journal.unlink()
            return {"runtime": runtime.public_identity(), "release": release.release_id, "health": health}
        except Exception as original:
            try:
                self._recover_locked(runtime, start=True, prefer_forward=False)
            except Exception as rollback_error:
                raise LifecycleError(
                    f"install failed ({original}); automatic rollback also failed "
                    f"({rollback_error}); run `hermes document-reader recover`"
                ) from original
            raise LifecycleError(f"install failed and was rolled back: {original}") from original

    def recover(self, *, start: bool = True) -> dict[str, Any]:
        runtime = self.runtime()
        create_profile_directories(runtime)
        with profile_install_lock(runtime):
            engine = recover_engine_configuration(runtime)
            service = self._recover_locked(runtime, start=start, prefer_forward=True)
            return {"engine": engine, "service": service}

    def _recover_locked(
        self,
        runtime: ProfileRuntime,
        *,
        start: bool,
        prefer_forward: bool,
    ) -> dict[str, Any]:
        raw = _read_optional_json(runtime.transaction_journal)
        if raw is None:
            return {"recovered": False, "reason": "no interrupted transaction"}
        transaction = _validate_transaction(runtime, raw)
        if transaction["operation"] in {"rollback", "uninstall"}:
            return self._recover_maintenance_locked(
                runtime,
                transaction,
                start=start,
            )
        tasks = self._tasks()
        token = validate_token_file(runtime.token_file)

        current_config: dict[str, Any] | None = None
        if runtime.config_file.exists():
            current_config = validate_service_config(
                runtime, read_bounded_json(runtime.config_file)
            )
        previous_config_candidate: dict[str, Any] | None = None
        if transaction["previous_config"] is not None:
            previous_config_candidate = validate_service_config(
                runtime,
                read_bounded_json(
                    _under(
                        runtime.install_dir,
                        Path(transaction["previous_config"]),
                        "previous config",
                    )
                ),
            )

        config_is_new = bool(
            current_config is not None
            and current_config.get("release_id") == transaction["new_release_id"]
            and sha256_file(runtime.config_file) == transaction["new_config_sha256"]
        )
        deployment_is_new = bool(
            transaction["new_deployment_sha256"] is not None
            and runtime.deployment_receipt.is_file()
            and sha256_file(runtime.deployment_receipt)
            == transaction["new_deployment_sha256"]
        )
        desktop_is_new = False
        try:
            desktop_candidate = _read_optional_json(runtime.desktop_receipt)
            if desktop_candidate is not None:
                desktop_candidate = _validate_desktop_receipt(
                    runtime, desktop_candidate
                )
                desktop_is_new = bool(
                    desktop_candidate["release_id"]
                    == transaction["new_release_id"]
                    and desktop_candidate["installed_sha256"]
                    == transaction["new_desktop_plugin_sha256"]
                    and _file_hash_or_none(runtime.desktop_plugin, "desktop plugin")
                    == transaction["new_desktop_plugin_sha256"]
                )
        except LifecycleError:
            desktop_is_new = False

        if prefer_forward and config_is_new and deployment_is_new and desktop_is_new:
            assert current_config is not None
            receipt = _validate_deployment(
                runtime, read_bounded_json(runtime.deployment_receipt)
            )
            if (
                receipt["release_id"] != transaction["new_release_id"]
                or receipt["service_config_sha256"]
                != transaction["new_config_sha256"]
            ):
                raise LifecycleError("committed receipt release does not match the transaction")
            desktop = _validate_desktop_receipt(
                runtime, read_bounded_json(runtime.desktop_receipt)
            )
            if (
                desktop["release_id"] != receipt["release_id"]
                or desktop["installed_sha256"] != receipt["desktop_sha256"]
                or runtime.desktop_plugin.is_symlink()
                or not runtime.desktop_plugin.is_file()
                or sha256_file(runtime.desktop_plugin)
                != receipt["desktop_sha256"]
            ):
                raise LifecycleError(
                    "committed Desktop authority does not match the transaction"
                )
            spec = _task_spec(runtime, current_config)
            task_state = tasks.inspect(spec)
            if not _attest_task_result(task_state, allow_absent=True):
                tasks.install(spec)
                _attest_task_result(tasks.inspect(spec), allow_absent=False)
            health = None
            if start:
                if loopback_port_open(runtime.port):
                    health = attest_health(current_config, token)
                else:
                    tasks.start(spec)
                    health = _wait_for_health(current_config, token, START_TIMEOUT_SECONDS)
            runtime.transaction_journal.unlink()
            return {
                "recovered": True,
                "direction": "forward",
                "release": receipt["release_id"],
                "health": health,
            }

        # Roll back only exact current ownership. Any ambiguity leaves the
        # journal intact for operator inspection instead of guessing.
        if loopback_port_open(runtime.port):
            if current_config is None:
                raise LifecycleError("listener is active but the transaction config is missing")
            _shutdown_attested(current_config, token)
        if current_config is not None:
            current_spec = _task_spec(runtime, current_config)
            current_task = tasks.inspect(current_spec)
            if current_task.get("exists") is True and current_task.get("action_matches") is True:
                tasks.remove(current_spec)
                if _attest_task_result(tasks.inspect(current_spec), allow_absent=True):
                    raise LifecycleError("owned scheduled task remained after removal")
            elif current_task.get("exists") is True:
                if previous_config_candidate is None:
                    raise LifecycleError("scheduled task is foreign to both transaction states")
                previous_spec = _task_spec(runtime, previous_config_candidate)
                _attest_task_result(tasks.inspect(previous_spec), allow_absent=False)
            else:
                _attest_task_result(current_task, allow_absent=True)

        _restore_install_desktop_snapshot(runtime, transaction)

        previous_config_path = transaction["previous_config"]
        previous_deployment_path = transaction["previous_deployment"]
        restored_health = None
        if previous_config_path is not None:
            previous_path = _under(
                runtime.install_dir, Path(previous_config_path), "previous config"
            )
            atomic_write_bytes(runtime.config_file, previous_path.read_bytes(), mode=0o600)
            restored_config = validate_service_config(
                runtime, read_bounded_json(runtime.config_file)
            )
        else:
            restored_config = None
            runtime.config_file.unlink(missing_ok=True)

        if previous_deployment_path is not None:
            previous_path = _under(
                runtime.install_dir,
                Path(previous_deployment_path),
                "previous deployment",
            )
            atomic_write_bytes(
                runtime.deployment_receipt, previous_path.read_bytes(), mode=0o600
            )
            _validate_deployment(
                runtime, read_bounded_json(runtime.deployment_receipt)
            )
        else:
            runtime.deployment_receipt.unlink(missing_ok=True)

        if transaction["previous_task_exists"]:
            if restored_config is None:
                raise LifecycleError(
                    "install transaction claims a prior task without a prior config"
                )
            restored_spec = _task_spec(runtime, restored_config)
            tasks.install(restored_spec)
            _attest_task_result(tasks.inspect(restored_spec), allow_absent=False)
            if start and transaction["previous_service_running"]:
                tasks.start(restored_spec)
                restored_health = _wait_for_health(
                    restored_config, token, START_TIMEOUT_SECONDS
                )

        runtime.transaction_journal.unlink()
        return {
            "recovered": True,
            "direction": "rollback",
            "health": restored_health,
            "data_preserved": True,
        }

    def _recover_maintenance_locked(
        self,
        runtime: ProfileRuntime,
        transaction: Mapping[str, Any],
        *,
        start: bool,
    ) -> dict[str, Any]:
        token = validate_token_file(runtime.token_file)
        snapshot_config_path = _under(
            runtime.install_dir,
            Path(str(transaction["snapshot_config"])),
            "snapshot config",
        )
        snapshot_config = validate_service_config(
            runtime, read_bounded_json(snapshot_config_path)
        )
        snapshot_deployment = None
        if transaction["snapshot_deployment"] is not None:
            snapshot_deployment = _validate_deployment(
                runtime,
                read_bounded_json(
                    _under(
                        runtime.install_dir,
                        Path(str(transaction["snapshot_deployment"])),
                        "snapshot deployment",
                    )
                ),
            )
            if (
                snapshot_deployment["service_config_sha256"]
                != transaction["snapshot_config_sha256"]
                or snapshot_deployment["release_id"]
                != snapshot_config["release_id"]
            ):
                raise LifecycleError(
                    "snapshot deployment does not attest the snapshot config"
                )

        current_config_sha = _require_allowed_hash(
            runtime.config_file,
            {
                transaction["snapshot_config_sha256"],
                transaction["target_config_sha256"],
            },
            "service config",
        )
        _require_allowed_hash(
            runtime.deployment_receipt,
            {
                transaction["snapshot_deployment_sha256"],
                transaction["target_deployment_sha256"],
            },
            "deployment receipt",
        )
        _require_allowed_hash(
            runtime.desktop_plugin,
            {
                transaction["snapshot_desktop_plugin_sha256"],
                transaction["target_desktop_plugin_sha256"],
            },
            "desktop plugin",
        )
        _require_allowed_hash(
            runtime.desktop_receipt,
            {
                transaction["snapshot_desktop_receipt_sha256"],
                transaction["target_desktop_receipt_sha256"],
            },
            "desktop receipt",
        )

        current_config = validate_service_config(
            runtime, read_bounded_json(runtime.config_file)
        )
        if sha256_file(runtime.config_file) != current_config_sha:
            raise LifecycleError("service config changed while recovery was validating it")
        if loopback_port_open(runtime.port):
            _shutdown_attested(current_config, token)

        tasks = self._tasks()
        _remove_owned_task(
            tasks,
            (
                _task_spec(runtime, current_config),
                _task_spec(runtime, snapshot_config),
            ),
        )

        # Restore every authority receipt before recreating or starting the task.
        _restore_snapshot_file(
            runtime,
            runtime.config_file,
            transaction["snapshot_config"],
            transaction["snapshot_config_sha256"],
            mode=0o600,
            label="service config",
        )
        _restore_snapshot_file(
            runtime,
            runtime.deployment_receipt,
            transaction["snapshot_deployment"],
            transaction["snapshot_deployment_sha256"],
            mode=0o600,
            label="deployment receipt",
        )
        _restore_snapshot_file(
            runtime,
            runtime.desktop_plugin,
            transaction["snapshot_desktop_plugin"],
            transaction["snapshot_desktop_plugin_sha256"],
            mode=0o644,
            label="desktop plugin",
        )
        _restore_snapshot_file(
            runtime,
            runtime.desktop_receipt,
            transaction["snapshot_desktop_receipt"],
            transaction["snapshot_desktop_receipt_sha256"],
            mode=0o600,
            label="desktop receipt",
        )
        restored_config = validate_service_config(
            runtime, read_bounded_json(runtime.config_file)
        )
        if snapshot_deployment is not None:
            _validate_deployment(
                runtime, read_bounded_json(runtime.deployment_receipt)
            )
        if transaction["snapshot_desktop_receipt"] is not None:
            desktop = _validate_desktop_receipt(
                runtime, read_bounded_json(runtime.desktop_receipt)
            )
            if (
                not runtime.desktop_plugin.is_file()
                or runtime.desktop_plugin.is_symlink()
                or sha256_file(runtime.desktop_plugin)
                != desktop["installed_sha256"]
            ):
                raise LifecycleError("restored desktop authority is inconsistent")

        health = None
        if transaction["snapshot_task_exists"]:
            restored_spec = _task_spec(runtime, restored_config)
            tasks.install(restored_spec)
            _attest_task_result(tasks.inspect(restored_spec), allow_absent=False)
            if start and transaction["snapshot_service_running"]:
                tasks.start(restored_spec)
                health = _wait_for_health(
                    restored_config, token, START_TIMEOUT_SECONDS
                )
        elif transaction["snapshot_service_running"]:
            raise LifecycleError(
                "maintenance snapshot claims a running service without an owned task"
            )
        runtime.transaction_journal.unlink()
        return {
            "recovered": True,
            "direction": "rollback",
            "operation": transaction["operation"],
            "health": health,
            "data_preserved": True,
        }

    def status(self) -> dict[str, Any]:
        runtime = self.runtime()
        create_profile_directories(runtime)
        with profile_install_lock(runtime):
            return self._status_locked(runtime)

    def _status_locked(self, runtime: ProfileRuntime) -> dict[str, Any]:
        journal = _read_optional_json(runtime.transaction_journal)
        if journal is not None:
            transaction = _validate_transaction(runtime, journal)
            return {
                "installed": runtime.deployment_receipt.is_file(),
                "runtime": runtime.public_identity(),
                "recovery_required": True,
                "transaction_operation": transaction["operation"],
                "transaction_phase": transaction["phase"],
                "error": "run `hermes document-reader recover` before another lifecycle change",
            }
        config = self._load_config(runtime)
        if config is None:
            task_probe = self._tasks().probe_name(runtime.task_name)
            task_exists = _task_name_exists(task_probe)
            listener_open = loopback_port_open(runtime.port)
            incomplete = task_exists or listener_open
            return {
                "installed": False,
                "runtime": runtime.public_identity(),
                "recovery_required": incomplete,
                "task": task_probe,
                "listener_open": listener_open,
                "error": (
                    "service config is missing while a profile task/listener remains; "
                    "restore exact authority before any removal"
                    if incomplete
                    else None
                ),
            }
        token = validate_token_file(runtime.token_file)
        engine_error = None
        try:
            validate_engine_config(runtime)
        except (LifecycleError, ProfileRuntimeError) as exc:
            engine_error = f"{type(exc).__name__}: engine configuration is not ready"
        spec = _task_spec(runtime, config)
        task = self._tasks().inspect(spec)
        installed = runtime.deployment_receipt.is_file()
        health = None
        errors: list[str] = []
        try:
            task_exists = _attest_task_result(task, allow_absent=not installed)
            if not installed and task_exists:
                errors.append("owned task exists without a deployment receipt")
        except LifecycleError as exc:
            errors.append(str(exc))
        if installed:
            try:
                deployment = _validate_deployment(
                    runtime, read_bounded_json(runtime.deployment_receipt)
                )
                desktop = _validate_desktop_receipt(
                    runtime, read_bounded_json(runtime.desktop_receipt)
                )
                if (
                    deployment["release_id"] != config["release_id"]
                    or deployment["service_config_sha256"]
                    != sha256_file(runtime.config_file)
                    or deployment["desktop_sha256"]
                    != desktop["installed_sha256"]
                    or desktop["release_id"] != deployment["release_id"]
                    or runtime.desktop_plugin.is_symlink()
                    or not runtime.desktop_plugin.is_file()
                    or sha256_file(runtime.desktop_plugin)
                    != deployment["desktop_sha256"]
                ):
                    raise LifecycleError(
                        "deployment authority does not match config/Desktop state"
                    )
            except (LifecycleError, ProfileRuntimeError) as exc:
                errors.append(str(exc))
        if loopback_port_open(runtime.port):
            try:
                health = attest_health(config, token)
            except LifecycleError as exc:
                errors.append(str(exc))
        return {
            "installed": installed,
            "runtime": runtime.public_identity(),
            "release": config["release_id"],
            "task": task,
            "health": health,
            "engine_error": engine_error,
            "error": "; ".join(errors) if errors else None,
        }

    def rollback(self, *, start: bool = True) -> dict[str, Any]:
        runtime = self.runtime()
        create_profile_directories(runtime)
        with profile_install_lock(runtime):
            if os.path.lexists(runtime.transaction_journal):
                raise LifecycleError(
                    "an interrupted lifecycle transaction needs `hermes document-reader recover`"
                )
            return self._rollback_locked(runtime, start=start)

    def _rollback_locked(self, runtime: ProfileRuntime, *, start: bool) -> dict[str, Any]:
        receipt_raw = _read_optional_json(runtime.deployment_receipt)
        if receipt_raw is None:
            raise LifecycleError("Document Reader is not installed")
        receipt = _validate_deployment(runtime, receipt_raw)
        previous_config_path = receipt["previous_config"]
        previous_deployment_path = receipt["previous_deployment"]
        if previous_config_path is None or previous_deployment_path is None:
            raise LifecycleError("no previous Document Reader release is available")
        current = self._load_config(runtime)
        if current is None:
            raise LifecycleError("deployment receipt exists but the service config is missing")
        if sha256_file(runtime.config_file) != receipt["service_config_sha256"]:
            raise LifecycleError("service config changed after deployment; refusing rollback")
        if receipt["release_id"] != current["release_id"]:
            raise LifecycleError("deployment receipt release does not match the service config")
        token = validate_token_file(runtime.token_file)
        current_spec = _task_spec(runtime, current)
        tasks = self._tasks()
        task_exists = _attest_task_result(tasks.inspect(current_spec), allow_absent=True)
        service_running = loopback_port_open(runtime.port)
        if service_running:
            attest_health(current, token)
            if not task_exists:
                raise LifecycleError("running service has no owned scheduled task")
        previous_config_file = _under(
            runtime.install_dir, Path(previous_config_path), "previous config"
        )
        previous_deployment_file = _under(
            runtime.install_dir, Path(previous_deployment_path), "previous deployment"
        )
        previous_config = validate_service_config(
            runtime, read_bounded_json(previous_config_file)
        )
        previous_deployment = _validate_deployment(
            runtime, read_bounded_json(previous_deployment_file)
        )
        previous_config_sha = sha256_file(previous_config_file)
        previous_deployment_sha = sha256_file(previous_deployment_file)
        if (
            previous_deployment["service_config_sha256"] != previous_config_sha
            or previous_deployment["release_id"] != previous_config["release_id"]
        ):
            raise LifecycleError("previous deployment does not attest the previous config")
        current_desktop = _validate_desktop_receipt(
            runtime, read_bounded_json(runtime.desktop_receipt)
        )
        if (
            current_desktop["release_id"] != receipt["release_id"]
            or current_desktop["installed_sha256"] != receipt["desktop_sha256"]
        ):
            raise LifecycleError("deployment receipt does not attest current Desktop state")
        target_desktop_sha, target_desktop_receipt_sha = _desktop_rollback_target(
            runtime, require_receipt=True
        )
        if previous_deployment["desktop_sha256"] != target_desktop_sha:
            raise LifecycleError("previous deployment does not attest the rollback desktop plugin")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_dir = _under(
            runtime.install_dir,
            runtime.install_dir / "backups" / stamp,
            "backup directory",
        )
        transaction = _maintenance_transaction(
            runtime,
            operation="rollback",
            backup_dir=backup_dir,
            target_config_sha256=previous_config_sha,
            target_deployment_sha256=previous_deployment_sha,
            target_desktop_plugin_sha256=target_desktop_sha,
            target_desktop_receipt_sha256=target_desktop_receipt_sha,
            task_exists=task_exists,
            service_running=service_running,
        )
        try:
            if service_running:
                _shutdown_attested(current, token)
            transaction = _write_transaction(runtime, transaction, "service_stopped")
            if task_exists:
                tasks.remove(current_spec)
                if _attest_task_result(tasks.inspect(current_spec), allow_absent=True):
                    raise LifecycleError("owned scheduled task remained after removal")
            transaction = _write_transaction(runtime, transaction, "task_removed")
            rollback_desktop_plugin(runtime)
            if (
                _file_hash_or_none(runtime.desktop_plugin, "desktop plugin")
                != target_desktop_sha
                or _file_hash_or_none(runtime.desktop_receipt, "desktop receipt")
                != target_desktop_receipt_sha
            ):
                raise LifecycleError("desktop rollback did not reach the journalled target")
            transaction = _write_transaction(runtime, transaction, "desktop_changed")
            atomic_write_bytes(
                runtime.config_file, previous_config_file.read_bytes(), mode=0o600
            )
            atomic_write_bytes(
                runtime.deployment_receipt,
                previous_deployment_file.read_bytes(),
                mode=0o600,
            )
            if (
                sha256_file(runtime.config_file) != previous_config_sha
                or sha256_file(runtime.deployment_receipt) != previous_deployment_sha
            ):
                raise LifecycleError("rollback authority commit did not match its journal")
            transaction = _write_transaction(runtime, transaction, "authority_changed")
            spec = _task_spec(runtime, previous_config)
            tasks.install(spec)
            _attest_task_result(tasks.inspect(spec), allow_absent=False)
            transaction = _write_transaction(runtime, transaction, "task_deployed")
            health = None
            if start:
                tasks.start(spec)
                health = _wait_for_health(previous_config, token, START_TIMEOUT_SECONDS)
                transaction = _write_transaction(runtime, transaction, "started")
            runtime.transaction_journal.unlink()
            return {"release": previous_deployment["release_id"], "health": health}
        except Exception as original:
            try:
                self._recover_locked(runtime, start=True, prefer_forward=False)
            except Exception as recovery:
                raise LifecycleError(
                    f"rollback failed ({original}); recovery also failed ({recovery}); "
                    "run `hermes document-reader recover`"
                ) from original
            raise LifecycleError(f"rollback failed and was recovered: {original}") from original

    def uninstall(self) -> dict[str, Any]:
        runtime = self.runtime()
        create_profile_directories(runtime)
        with profile_install_lock(runtime):
            if os.path.lexists(runtime.transaction_journal):
                raise LifecycleError(
                    "an interrupted lifecycle transaction needs `hermes document-reader recover`"
                )
            return self._uninstall_locked(runtime)

    def _uninstall_locked(self, runtime: ProfileRuntime) -> dict[str, Any]:
        config = self._load_config(runtime)
        if config is None:
            task_exists = _task_name_exists(
                self._tasks().probe_name(runtime.task_name)
            )
            listener_open = loopback_port_open(runtime.port)
            if task_exists or listener_open:
                raise LifecycleError(
                    "service config is missing while a profile task/listener remains; "
                    "refusing an unauthenticated uninstall"
                )
            if (
                runtime.deployment_receipt.exists()
                or runtime.desktop_receipt.exists()
                or runtime.desktop_plugin.exists()
            ):
                raise LifecycleError(
                    "profile authority is incomplete; refusing an unreceipted uninstall"
                )
            return {"removed": False, "data_preserved": str(runtime.data_root)}
        token = validate_token_file(runtime.token_file)
        spec = _task_spec(runtime, config)
        tasks = self._tasks()
        task_exists = _attest_task_result(tasks.inspect(spec), allow_absent=True)
        service_running = loopback_port_open(runtime.port)
        if service_running:
            attest_health(config, token)
            if not task_exists:
                raise LifecycleError("running service has no owned scheduled task")
        deployment = _read_optional_json(runtime.deployment_receipt)
        if deployment is not None:
            deployment = _validate_deployment(runtime, deployment)
            if (
                deployment["release_id"] != config["release_id"]
                or deployment["service_config_sha256"]
                != sha256_file(runtime.config_file)
            ):
                raise LifecycleError("deployment receipt does not attest the service config")
            desktop_authority = _read_optional_json(runtime.desktop_receipt)
            if desktop_authority is None:
                raise LifecycleError("deployment receipt exists without Desktop authority")
            desktop_authority = _validate_desktop_receipt(
                runtime, desktop_authority
            )
            if (
                desktop_authority["release_id"] != deployment["release_id"]
                or desktop_authority["installed_sha256"]
                != deployment["desktop_sha256"]
                or runtime.desktop_plugin.is_symlink()
                or not runtime.desktop_plugin.is_file()
                or sha256_file(runtime.desktop_plugin)
                != deployment["desktop_sha256"]
            ):
                raise LifecycleError("deployment receipt does not attest Desktop state")
        (
            target_desktop_plugin,
            target_desktop_sha,
            target_desktop_receipt,
            target_desktop_receipt_sha,
        ) = _desktop_uninstall_target(runtime)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_dir = _under(
            runtime.install_dir,
            runtime.install_dir / "backups" / stamp,
            "backup directory",
        )
        config_sha = sha256_file(runtime.config_file)
        transaction = _maintenance_transaction(
            runtime,
            operation="uninstall",
            backup_dir=backup_dir,
            target_config_sha256=config_sha,
            target_deployment_sha256=None,
            target_desktop_plugin_sha256=target_desktop_sha,
            target_desktop_receipt_sha256=target_desktop_receipt_sha,
            task_exists=task_exists,
            service_running=service_running,
        )
        try:
            if service_running:
                _shutdown_attested(config, token)
            transaction = _write_transaction(runtime, transaction, "service_stopped")
            if task_exists:
                tasks.remove(spec)
                if _attest_task_result(tasks.inspect(spec), allow_absent=True):
                    raise LifecycleError("owned scheduled task remained after removal")
            transaction = _write_transaction(runtime, transaction, "task_removed")
            _write_desktop_target(
                runtime, target_desktop_plugin, target_desktop_receipt
            )
            if (
                _file_hash_or_none(runtime.desktop_plugin, "desktop plugin")
                != target_desktop_sha
                or _file_hash_or_none(runtime.desktop_receipt, "desktop receipt")
                != target_desktop_receipt_sha
            ):
                raise LifecycleError("desktop uninstall did not reach the journalled target")
            transaction = _write_transaction(runtime, transaction, "desktop_changed")
            runtime.deployment_receipt.unlink(missing_ok=True)
            if sha256_file(runtime.config_file) != config_sha:
                raise LifecycleError("uninstall changed the retained service config")
            transaction = _write_transaction(runtime, transaction, "authority_changed")
            runtime.transaction_journal.unlink()
            # Config, token, releases, logs, and all documents remain recoverable.
            return {
                "removed": True,
                "data_preserved": str(runtime.data_root),
                "runtime_preserved": str(runtime.plugin_root),
            }
        except Exception as original:
            try:
                self._recover_locked(runtime, start=True, prefer_forward=False)
            except Exception as recovery:
                raise LifecycleError(
                    f"uninstall failed ({original}); recovery also failed ({recovery}); "
                    "run `hermes document-reader recover`"
                ) from original
            raise LifecycleError(f"uninstall failed and was recovered: {original}") from original
