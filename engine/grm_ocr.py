# -*- coding: utf-8 -*-
"""Shared OCR client for an OpenAI-compatible multimodal endpoint.

The configured model is called with thinking disabled so its response budget
is reserved for recognized document content. Chandra supplies the prompts and
parsers; this module owns the bounded transport, retry, normalization, and
streaming behavior used by the profile service and source-only tools.
"""

import base64
import dataclasses
import hmac
import io
import json
import os
import re
import ssl
import stat
import threading
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup
from bs4 import Comment
import httpx
from openai import OpenAI

from chandra.model.util import detect_repeat_token, scale_to_fit
from chandra.output import parse_html, parse_markdown
from chandra.prompts import PROMPT_MAPPING

def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclasses.dataclass(frozen=True)
class EngineConfig:
    api_base: str
    api_key: str = dataclasses.field(repr=False)
    model: str
    max_tokens: int
    request_timeout: int
    transport_retries: int
    ca_bundle: str | None
    allow_insecure_http: bool = False
    allow_remote_mcp_ocr: bool = False
    ca_bundle_pem: str | None = dataclasses.field(default=None, repr=False)


def validate_config(config: EngineConfig) -> EngineConfig:
    """Normalize and revalidate an immutable OCR transport configuration."""
    api_base = str(config.api_base).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(api_base)
    if (
        not 1 <= len(api_base) <= 2048
        or any(ord(ch) < 33 or ch.isspace() for ch in api_base)
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("OCR API base must be an absolute HTTP(S) URL without userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("OCR API base must not contain a query or fragment")
    if parsed.scheme == "http" and config.allow_insecure_http is not True:
        raise ValueError("OCR API base uses plaintext HTTP without explicit profile opt-in")
    if parsed.scheme == "https" and config.allow_insecure_http is True:
        raise ValueError("OCR plaintext opt-in must be false for an HTTPS API base")

    model = str(config.model).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise ValueError("OCR model identifier is invalid")
    api_key = str(config.api_key)
    if not api_key or len(api_key) > 2048 or any(ch in api_key for ch in "\r\n"):
        raise ValueError("OCR API key must be a bounded single-line value")

    def bounded(value, label, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}")
        return value

    if type(config.allow_remote_mcp_ocr) is not bool:
        raise ValueError("OCR MCP consent must be a boolean")
    ca_bundle = config.ca_bundle
    ca_bundle_pem = config.ca_bundle_pem
    if ca_bundle is not None:
        ca_path = Path(ca_bundle).expanduser()
        if ca_bundle_pem is None:
            ca_path = ca_path.resolve()
            if not ca_path.is_file() or ca_path.is_symlink():
                raise ValueError("OCR CA bundle must be a regular non-link file")
        elif not ca_path.is_absolute():
            raise ValueError("OCR CA bundle path must be absolute")
        ca_bundle = str(ca_path)
    elif ca_bundle_pem is not None:
        raise ValueError("OCR CA bundle data requires a configured bundle")
    if ca_bundle_pem is not None:
        if not isinstance(ca_bundle_pem, str) or not 1 <= len(ca_bundle_pem.encode("utf-8")) <= 4 * 1024 * 1024:
            raise ValueError("OCR CA bundle data is invalid")
        try:
            ssl.create_default_context(cadata=ca_bundle_pem)
        except (OSError, ssl.SSLError) as exc:
            raise ValueError("OCR CA bundle data is invalid") from exc

    return EngineConfig(
        api_base=api_base,
        api_key=api_key,
        model=model,
        max_tokens=bounded(config.max_tokens, "OCR max tokens", 256, 32768),
        request_timeout=bounded(config.request_timeout, "OCR timeout", 5, 300),
        transport_retries=bounded(config.transport_retries, "OCR retries", 0, 2),
        ca_bundle=ca_bundle,
        allow_insecure_http=config.allow_insecure_http is True,
        allow_remote_mcp_ocr=config.allow_remote_mcp_ocr,
        ca_bundle_pem=ca_bundle_pem,
    )


def load_config() -> EngineConfig:
    api_base = os.environ.get("GRM_OCR_API_BASE", "").strip().rstrip("/")
    if not api_base:
        raise ValueError("GRM_OCR_API_BASE must be configured before OCR can run")
    parsed = urllib.parse.urlsplit(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("GRM_OCR_API_BASE must be an absolute HTTP(S) URL without userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("GRM_OCR_API_BASE must not contain a query or fragment")
    if parsed.scheme == "http" and not _enabled("GRM_OCR_ALLOW_INSECURE_HTTP"):
        raise ValueError(
            "GRM_OCR_API_BASE uses plaintext HTTP; set "
            "GRM_OCR_ALLOW_INSECURE_HTTP=1 only for an explicitly trusted network"
        )

    model = os.environ.get("GRM_OCR_MODEL", "").strip()
    if not model:
        raise ValueError("GRM_OCR_MODEL must be configured before OCR can run")

    ca_bundle = os.environ.get("GRM_OCR_CA_BUNDLE", "").strip() or None
    if ca_bundle is not None:
        ca_path = Path(ca_bundle).expanduser().resolve()
        if not ca_path.is_file():
            raise ValueError(f"GRM_OCR_CA_BUNDLE does not exist: {ca_path}")
        ca_bundle = str(ca_path)

    api_key = os.environ.get("GRM_OCR_API_KEY", "local")
    if not api_key or len(api_key) > 2048 or any(ch in api_key for ch in "\r\n"):
        raise ValueError("GRM_OCR_API_KEY must be a bounded single-line value")

    return validate_config(EngineConfig(
        api_base=api_base,
        api_key=api_key,
        model=model,
        max_tokens=_bounded_int("GRM_OCR_MAX_TOKENS", 8192, 256, 32768),
        request_timeout=_bounded_int("GRM_OCR_TIMEOUT", 120, 5, 300),
        transport_retries=_bounded_int("GRM_OCR_TRANSPORT_RETRIES", 0, 0, 2),
        ca_bundle=ca_bundle,
        allow_insecure_http=_enabled("GRM_OCR_ALLOW_INSECURE_HTTP"),
        allow_remote_mcp_ocr=False,
    ))


_PROFILE_CONFIG_KEYS = {
    "schema", "api_base", "model", "max_tokens", "request_timeout",
    "transport_retries", "ca_bundle", "allow_insecure_http", "allow_remote_mcp_ocr",
}


def _is_link_or_reparse_info(info) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        reparse and getattr(info, "st_file_attributes", 0) & reparse
    )


def _reject_reparse_chain(path: Path, label: str) -> None:
    """Reject every existing literal link/junction component without resolving it."""

    current = Path(os.path.abspath(os.fspath(path)))
    if not current.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    while True:
        if os.path.lexists(current):
            try:
                info = current.lstat()
            except OSError as exc:
                raise ValueError(f"{label} is inaccessible") from exc
            if _is_link_or_reparse_info(info):
                raise ValueError(f"{label} path contains a link or reparse point")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _open_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt":
        return os.open(path, flags)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(
        str(path),
        0x80000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), str(path))
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except Exception:
        kernel.CloseHandle(handle)
        raise


def _stat_signature(info) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
    )


def _same_path_handle_identity(path_info, handle_info) -> bool:
    """Compare pathname and open-handle stats without NTFS ctime drift."""

    path_signature = _stat_signature(path_info)
    handle_signature = _stat_signature(handle_info)
    if os.name == "nt":
        path_signature = path_signature[:-1]
        handle_signature = handle_signature[:-1]
    return path_signature == handle_signature


def read_regular_bytes(
    path: Path,
    *,
    maximum: int,
    label: str,
    require_private_mode: bool = False,
    validate_windows_acl: bool = False,
) -> bytes:
    """Boundedly read one literal regular file through an identity-pinned handle."""

    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    _reject_reparse_chain(path.parent, label)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is missing or inaccessible") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or _is_link_or_reparse_info(before)
        or before.st_size <= 0
        or before.st_size > maximum
    ):
        raise ValueError(f"{label} must be a bounded regular non-link file")
    if require_private_mode and os.name != "nt" and before.st_mode & 0o077:
        raise PermissionError(f"{label} must not be accessible by group or other users")

    try:
        descriptor = _open_no_follow(path)
    except OSError as exc:
        raise ValueError(f"{label} is missing or inaccessible") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not _same_path_handle_identity(before, opened)
            or not stat.S_ISREG(opened.st_mode)
            or _is_link_or_reparse_info(opened)
            or opened.st_size <= 0
            or opened.st_size > maximum
        ):
            raise ValueError(f"{label} changed identity while opening")
        if validate_windows_acl:
            _validate_windows_secret_acl(descriptor)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            after_path = path.lstat()
        except OSError as exc:
            raise ValueError(f"{label} changed identity while reading") from exc
        _reject_reparse_chain(path.parent, label)
        if (
            not raw
            or len(raw) > maximum
            or len(raw) != opened.st_size
            or _stat_signature(opened) != _stat_signature(after)
            or _is_link_or_reparse_info(after_path)
            or not os.path.samestat(after, after_path)
        ):
            raise ValueError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def _validate_windows_secret_acl(descriptor: int) -> None:
    """Require an inherited-free DACL containing only this user and SYSTEM."""
    if os.name != "nt":
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", ctypes.c_ushort),
        ]

    class ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", ACE_HEADER),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    token_handle = wintypes.HANDLE()
    security_descriptor = ctypes.c_void_p()
    owner_sid = ctypes.c_void_p()
    dacl = ctypes.c_void_p()

    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetSecurityInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int,
    ]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    advapi.CreateWellKnownSid.argtypes = [
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.CreateWellKnownSid.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    try:
        process = kernel.GetCurrentProcess()
        if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token_handle)):
            raise PermissionError("could not inspect the private token ACL")
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token_handle, 1, None, 0, ctypes.byref(needed))
        if needed.value <= 0:
            raise PermissionError("could not inspect the private token ACL")
        token_buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(
            token_handle, 1, token_buffer, needed, ctypes.byref(needed)
        ):
            raise PermissionError("could not inspect the private token ACL")
        current_sid = ctypes.cast(
            token_buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)
        ).contents.Sid

        system_size = wintypes.DWORD(68)
        system_buffer = ctypes.create_string_buffer(system_size.value)
        if not advapi.CreateWellKnownSid(
            22, None, system_buffer, ctypes.byref(system_size)
        ):
            raise PermissionError("could not inspect the private token ACL")
        system_sid = ctypes.cast(system_buffer, ctypes.c_void_p)

        result = advapi.GetSecurityInfo(
            wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)),
            1,
            0x00000004,
            ctypes.byref(owner_sid),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if result != 0 or not security_descriptor.value or not dacl.value:
            raise PermissionError("private token ACL is unavailable or unsafe")

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(
            security_descriptor, ctypes.byref(control), ctypes.byref(revision)
        ) or not control.value & 0x1000:
            raise PermissionError("private token ACL inheritance must be disabled")

        size_info = ACL_SIZE_INFORMATION()
        if not advapi.GetAclInformation(
            dacl, ctypes.byref(size_info), ctypes.sizeof(size_info), 2
        ) or size_info.AceCount > 16:
            raise PermissionError("private token ACL is invalid")
        seen_current = False
        seen_system = False
        same_identity = bool(advapi.EqualSid(current_sid, system_sid))
        for index in range(size_info.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise PermissionError("private token ACL is invalid")
            header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
            if (
                header.AceType != 0
                or header.AceFlags & 0x10
                or header.AceSize < ACCESS_ALLOWED_ACE.SidStart.offset + ctypes.sizeof(wintypes.DWORD)
            ):
                raise PermissionError("private token ACL contains an unsafe rule")
            sid = ctypes.c_void_p(ace.value + ACCESS_ALLOWED_ACE.SidStart.offset)
            if advapi.EqualSid(sid, current_sid):
                seen_current = True
                if same_identity:
                    seen_system = True
            elif advapi.EqualSid(sid, system_sid):
                seen_system = True
            else:
                raise PermissionError("private token ACL contains a foreign principal")
        if not seen_current or not seen_system:
            raise PermissionError("private token ACL must grant only this user and SYSTEM")
    finally:
        if security_descriptor.value:
            kernel.LocalFree(security_descriptor)
        if token_handle.value:
            kernel.CloseHandle(token_handle)


def read_private_value(path: Path, minimum: int, maximum: int) -> str:
    raw = read_regular_bytes(
        Path(path),
        maximum=maximum + 1,
        label="private token",
        require_private_mode=True,
        validate_windows_acl=True,
    )
    try:
        rendered = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("private token must be strict UTF-8") from exc
    value = rendered[:-1] if rendered.endswith("\n") else rendered
    if (
        not minimum <= len(value.encode("utf-8")) <= maximum
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ValueError("private token must be one exact bounded line without whitespace")
    return value


def load_profile_config(config_path: Path, token_path: Path) -> EngineConfig:
    """Load the selected profile's exact engine config without consulting env."""
    config_path = Path(config_path)
    token_path = Path(token_path)
    if (
        not config_path.is_absolute()
        or not token_path.is_absolute()
        or config_path.name != "engine.json"
        or token_path.name != "engine.token"
        or config_path.parent != token_path.parent
        or config_path.parent.name != "config"
    ):
        raise ValueError("engine configuration must use the selected profile config paths")
    transaction_path = config_path.parent / "engine-transaction.json"
    if os.path.lexists(transaction_path):
        raise ValueError("engine configuration transaction is incomplete")
    try:
        config_raw = read_regular_bytes(
            config_path,
            maximum=16 * 1024,
            label="engine config",
            require_private_mode=True,
        )
        payload = json.loads(config_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("engine config must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _PROFILE_CONFIG_KEYS:
        raise ValueError("engine config has missing or unexpected keys")
    if type(payload["schema"]) is not int or payload["schema"] != 1:
        raise ValueError("engine config schema must be 1")
    if type(payload["allow_insecure_http"]) is not bool:
        raise ValueError("engine allow_insecure_http must be a boolean")
    if type(payload["allow_remote_mcp_ocr"]) is not bool:
        raise ValueError("engine allow_remote_mcp_ocr must be a boolean")
    for key in ("api_base", "model"):
        if not isinstance(payload[key], str):
            raise ValueError(f"engine {key} must be a string")
    for key in ("max_tokens", "request_timeout", "transport_retries"):
        if type(payload[key]) is not int:
            raise ValueError(f"engine {key} must be an integer")

    ca_bundle = payload["ca_bundle"]
    ca_bundle_pem = None
    ca_bundle_raw = None
    if ca_bundle is not None:
        if not isinstance(ca_bundle, str) or not ca_bundle.strip():
            raise ValueError("engine ca_bundle must be null or a relative file name")
        relative_ca = Path(ca_bundle)
        if relative_ca.is_absolute() or ".." in relative_ca.parts:
            raise ValueError("engine ca_bundle must stay within the profile config directory")
        ca_path = Path(os.path.abspath(config_path.parent / relative_ca))
        if not ca_path.is_relative_to(config_path.parent):
            raise ValueError("engine ca_bundle escapes the profile config directory")
        try:
            ca_bundle_raw = read_regular_bytes(
                ca_path,
                maximum=4 * 1024 * 1024,
                label="engine CA bundle",
                require_private_mode=True,
            )
            ca_bundle_pem = ca_bundle_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("engine CA bundle must be valid UTF-8 PEM") from exc
        ca_bundle = str(ca_path)

    api_key = read_private_value(token_path, minimum=16, maximum=2048)
    confirm_config = read_regular_bytes(
        config_path,
        maximum=16 * 1024,
        label="engine config",
        require_private_mode=True,
    )
    confirm_token = read_private_value(token_path, minimum=16, maximum=2048)
    if ca_bundle_raw is not None:
        confirm_ca = read_regular_bytes(
            Path(ca_bundle),
            maximum=4 * 1024 * 1024,
            label="engine CA bundle",
            require_private_mode=True,
        )
    else:
        confirm_ca = None
    if (
        os.path.lexists(transaction_path)
        or not hmac.compare_digest(config_raw, confirm_config)
        or not hmac.compare_digest(api_key, confirm_token)
        or (
            ca_bundle_raw is not None
            and (confirm_ca is None or not hmac.compare_digest(ca_bundle_raw, confirm_ca))
        )
    ):
        raise ValueError("engine configuration changed while loading")
    return validate_config(EngineConfig(
        api_base=payload["api_base"],
        api_key=api_key,
        model=payload["model"],
        max_tokens=payload["max_tokens"],
        request_timeout=payload["request_timeout"],
        transport_retries=payload["transport_retries"],
        ca_bundle=ca_bundle,
        allow_insecure_http=payload["allow_insecure_http"],
        allow_remote_mcp_ocr=payload["allow_remote_mcp_ocr"],
        ca_bundle_pem=ca_bundle_pem,
    ))


_client = None
_client_config = None
_configured_config = None
_client_lock = threading.Lock()


def configure(config: EngineConfig | None) -> None:
    """Pin one selected profile config for this service process."""
    global _client, _client_config, _configured_config
    normalized = validate_config(config) if config is not None else None
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None
        _client_config = None
        _configured_config = normalized


def current_config() -> EngineConfig:
    configured = _configured_config
    if configured is not None:
        return validate_config(configured)
    return load_config()


def _transport_verify(config: EngineConfig):
    if config.ca_bundle_pem is not None:
        return ssl.create_default_context(cadata=config.ca_bundle_pem)
    return config.ca_bundle or True


def client() -> OpenAI:
    """Return a client whose environment-backed configuration is revalidated."""
    global _client, _client_config
    config = current_config()
    with _client_lock:
        if _client is None or config != _client_config:
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
            verify = _transport_verify(config)
            transport = httpx.Client(
                verify=verify,
                timeout=httpx.Timeout(
                    config.request_timeout,
                    connect=min(10, config.request_timeout),
                    pool=min(10, config.request_timeout),
                ),
            )
            _client = OpenAI(
                api_key=config.api_key,
                base_url=config.api_base,
                timeout=config.request_timeout,
                max_retries=config.transport_retries,
                http_client=transport,
            )
            _client_config = config
        return _client


def probe(timeout: int = 8) -> None:
    """Perform a bounded, authenticated, TLS-verified model endpoint check."""
    config = current_config()
    timeout = max(1, min(int(timeout), 15))
    headers = {"Authorization": f"Bearer {config.api_key}"}
    verify = _transport_verify(config)
    with httpx.Client(
        verify=verify,
        timeout=httpx.Timeout(timeout, connect=min(5, timeout)),
        follow_redirects=False,
    ) as transport:
        response = transport.get(f"{config.api_base}/models", headers=headers)
        response.raise_for_status()


def _stream_once(b64: str, on_delta, temperature: float, top_p: float):
    """One streamed completion. Returns (raw, aborted_for_repeats)."""
    config = current_config()
    stream = client().chat.completions.create(
        model=config.model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": PROMPT_MAPPING["ocr_layout"]},
                ],
            }
        ],
        max_tokens=config.max_tokens,
        temperature=temperature,
        top_p=top_p,
        stream=True,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    parts = []
    size = 0
    next_check = 1500
    try:
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if not delta:
                continue
            parts.append(delta)
            size += len(delta)
            if on_delta is not None:
                on_delta("".join(parts))
            if size >= next_check:
                next_check = size + 1500
                if detect_repeat_token("".join(parts)):
                    return "".join(parts), True
        return "".join(parts), False
    finally:
        try:
            stream.close()
        except Exception:
            pass


_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")

_SAFE_HTML_TAGS = {
    "p", "div", "span", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "s", "small", "blockquote", "pre", "code",
    "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "caption", "sup", "sub",
}
_DROP_WITH_CONTENT = {
    "script", "style", "iframe", "object", "embed", "link", "meta", "base",
    "form", "input", "button", "select", "option", "textarea", "svg", "math",
    "audio", "video", "source", "track", "canvas", "template",
}
_SAFE_COMMON_ATTRS = {"class", "data-bbox", "data-label"}
_SAFE_TABLE_ATTRS = {"colspan", "rowspan", "scope"}
_SAFE_CLASS = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


def sanitize_ocr_html(html: str) -> str:
    """Strictly allowlist inert OCR layout markup for browser rendering.

    OCR output is attacker-influenced model output. URL-bearing, executable,
    form, SVG/MathML, embedded-media, style, id, and event attributes are never
    retained. Unknown layout tags are unwrapped so their readable text remains.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for tag in list(soup.find_all(True)):
        if not tag.name:
            continue
        name = tag.name.lower()
        if name in _DROP_WITH_CONTENT:
            tag.decompose()
            continue
        if name not in _SAFE_HTML_TAGS:
            tag.unwrap()
            continue

        allowed = set(_SAFE_COMMON_ATTRS)
        if name in {"th", "td"}:
            allowed.update(_SAFE_TABLE_ATTRS)
        clean_attrs = {}
        for raw_key, raw_value in list(tag.attrs.items()):
            key = raw_key.lower()
            if key not in allowed:
                continue
            if key == "class":
                values = raw_value if isinstance(raw_value, list) else str(raw_value).split()
                classes = [value for value in values if _SAFE_CLASS.fullmatch(str(value))][:8]
                if classes:
                    clean_attrs[key] = classes
            elif key == "data-bbox":
                try:
                    values = [float(value) for value in str(raw_value).split()]
                except (TypeError, ValueError):
                    continue
                if len(values) == 4 and all(0 <= value <= 1000 for value in values):
                    clean_attrs[key] = " ".join(f"{value:g}" for value in values)
            elif key == "data-label":
                label = re.sub(r"[^A-Za-z0-9 _.-]", "", str(raw_value))[:48].strip()
                if label:
                    clean_attrs[key] = label
            elif key in {"colspan", "rowspan"}:
                try:
                    span = int(raw_value)
                except (TypeError, ValueError):
                    continue
                if 1 <= span <= 100:
                    clean_attrs[key] = str(span)
            elif key == "scope" and str(raw_value).lower() in {"row", "col", "rowgroup", "colgroup"}:
                clean_attrs[key] = str(raw_value).lower()
        tag.attrs = clean_attrs
    return str(soup)


def _mostly_images(raw: str) -> bool:
    """True when the page parsed to little besides image placeholders.

    GRM occasionally classifies a form-dense page (e.g. four W-2 copies) as
    Figure regions and reads nothing — a nondeterministic miss worth a retry.
    Pages that legitimately are pure images just burn the retries and keep
    the last answer.
    """
    try:
        md = raw_to_markdown(raw)
    except Exception:
        return False
    text = _IMG_MD.sub("", md).strip()
    return len(md) > 0 and len(text) < 120


def ocr_page_raw(image, on_delta=None, max_retries: int = 3) -> str:
    """OCR one PIL page image. Returns chandra-format raw output.

    on_delta(text_so_far) is called after each streamed chunk when given (on a
    retry it restarts from empty). Degenerate repetition (e.g. multi-copy 1099
    pages sending the model into a loop) is detected mid-stream, the request is
    aborted, and generation retries with bumped temperature — same policy as
    chandra's generate_vllm.
    """
    img = scale_to_fit(image)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    temperature, top_p = 0.0, 0.1
    raw = ""
    for attempt in range(max_retries + 1):
        raw, aborted = _stream_once(b64, on_delta, temperature, top_p)
        bad = aborted or detect_repeat_token(raw) or (
            len(raw) > 50 and detect_repeat_token(raw, cut_from_end=50)
        ) or _mostly_images(raw)
        if not bad or attempt == max_retries:
            break
        temperature = min(0.2 * (attempt + 1), 0.8)
        top_p = 0.95
    return raw


def normalize_raw(raw: str) -> str:
    """GRM wraps its answer in a ```html fence and an <html><body> shell;
    chandra's parsers expect bare top-level divs. Unwrap both."""
    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    if "<body" in text:
        soup = BeautifulSoup(text, "html.parser")
        body = soup.find("body")
        if body is not None:
            text = "".join(str(c) for c in body.children)
    return text


def raw_to_markdown(raw: str) -> str:
    return parse_markdown(normalize_raw(raw))


def raw_to_html(raw: str) -> str:
    return parse_html(normalize_raw(raw))


def ocr_page_markdown(image) -> str:
    return raw_to_markdown(ocr_page_raw(image))
