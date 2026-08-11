"""Strict profile-owned OCR engine configuration.

Secrets are stored only in ``engine.token`` beside a non-secret exact-schema
``engine.json``. They are never inherited from process/user environment and
never appear in service/task arguments.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

try:
    from .profile_runtime import (
        ProfileRuntime,
        ProfileRuntimeError,
        atomic_write_bytes,
        atomic_write_json,
        _read_regular_bytes,
        read_bounded_json,
        read_private_single_line,
        write_private_single_line,
    )
except ImportError:
    from profile_runtime import (  # type: ignore
        ProfileRuntime,
        ProfileRuntimeError,
        atomic_write_bytes,
        atomic_write_json,
        _read_regular_bytes,
        read_bounded_json,
        read_private_single_line,
        write_private_single_line,
    )


ENGINE_SCHEMA = 1
ENGINE_CONFIG_KEYS = {
    "schema",
    "api_base",
    "model",
    "max_tokens",
    "request_timeout",
    "transport_retries",
    "ca_bundle",
    "allow_insecure_http",
    "allow_remote_mcp_ocr",
}
MAX_ENGINE_CONFIG_BYTES = 32 * 1024
MAX_CA_BUNDLE_BYTES = 4 * 1024 * 1024
ENGINE_TRANSACTION_KEYS = {
    "schema",
    "plugin",
    "profile",
    "profile_fingerprint",
    "phase",
    "previous_config",
    "previous_config_sha256",
    "previous_token",
    "previous_token_sha256",
    "new_config_sha256",
    "new_token_sha256",
    "started_at",
}


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProfileRuntimeError(f"engine {label} must be between {minimum} and {maximum}")
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_hash(path: Path, *, maximum: int) -> str:
    return _sha_bytes(_read_regular_bytes(path, maximum=maximum))


def _transaction_path(runtime: ProfileRuntime) -> Path:
    return runtime.config_dir / "engine-transaction.json"


def _contained_config(runtime: ProfileRuntime, value: Any, label: str) -> Path:
    candidate = Path(str(value)).resolve(strict=False)
    try:
        candidate.relative_to(runtime.config_dir.resolve(strict=False))
    except ValueError as exc:
        raise ProfileRuntimeError(f"engine transaction {label} escapes profile config") from exc
    return candidate


def _validate_transaction(runtime: ProfileRuntime, raw: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(raw)
    if set(value) != ENGINE_TRANSACTION_KEYS:
        raise ProfileRuntimeError("engine transaction schema mismatch")
    expected = {
        "schema": 1,
        "plugin": "document-reader",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ProfileRuntimeError(f"engine transaction {key} identity mismatch")
    if value.get("phase") not in {"prepared", "token_written", "config_written"}:
        raise ProfileRuntimeError("engine transaction phase is invalid")
    for key in (
        "previous_config_sha256",
        "previous_token_sha256",
        "new_config_sha256",
        "new_token_sha256",
    ):
        item = value.get(key)
        if item is not None and not re.fullmatch(r"[0-9a-f]{64}", str(item)):
            raise ProfileRuntimeError(f"engine transaction {key} is invalid")
    for path_key, hash_key in (
        ("previous_config", "previous_config_sha256"),
        ("previous_token", "previous_token_sha256"),
    ):
        path_value = value.get(path_key)
        hash_value = value.get(hash_key)
        if (path_value is None) != (hash_value is None):
            raise ProfileRuntimeError("engine transaction backup identity is incomplete")
        if path_value is not None:
            backup = _contained_config(runtime, path_value, path_key)
            maximum = MAX_ENGINE_CONFIG_BYTES if path_key == "previous_config" else 2050
            if _strict_hash(backup, maximum=maximum) != hash_value:
                raise ProfileRuntimeError(f"engine transaction {path_key} backup is invalid")
    return value


def recover_engine_configuration(runtime: ProfileRuntime) -> dict[str, Any]:
    transaction_path = _transaction_path(runtime)
    if not os.path.lexists(transaction_path):
        return {"recovered": False}
    transaction = _validate_transaction(
        runtime,
        read_bounded_json(transaction_path, maximum=MAX_ENGINE_CONFIG_BYTES),
    )
    current_config_hash = (
        _strict_hash(runtime.engine_config_file, maximum=MAX_ENGINE_CONFIG_BYTES)
        if os.path.lexists(runtime.engine_config_file)
        else None
    )
    current_token_hash = (
        _strict_hash(runtime.engine_token_file, maximum=2050)
        if os.path.lexists(runtime.engine_token_file)
        else None
    )
    if (
        current_config_hash == transaction["new_config_sha256"]
        and current_token_hash == transaction["new_token_sha256"]
    ):
        validate_engine_config(runtime)
        transaction_path.unlink()
        return {"recovered": True, "direction": "forward"}

    allowed_config = {None, transaction["new_config_sha256"], transaction["previous_config_sha256"]}
    allowed_token = {None, transaction["new_token_sha256"], transaction["previous_token_sha256"]}
    if current_config_hash not in allowed_config or current_token_hash not in allowed_token:
        raise ProfileRuntimeError("engine files changed outside the interrupted transaction")
    if transaction["previous_config"] is None:
        runtime.engine_config_file.unlink(missing_ok=True)
        runtime.engine_token_file.unlink(missing_ok=True)
    else:
        config_backup = _contained_config(runtime, transaction["previous_config"], "previous_config")
        token_backup = _contained_config(runtime, transaction["previous_token"], "previous_token")
        atomic_write_bytes(
            runtime.engine_config_file,
            _read_regular_bytes(config_backup, maximum=MAX_ENGINE_CONFIG_BYTES),
            mode=0o600,
        )
        atomic_write_bytes(
            runtime.engine_token_file,
            _read_regular_bytes(token_backup, maximum=2050),
            mode=0o600,
        )
        validate_engine_config(runtime)
    transaction_path.unlink()
    return {"recovered": True, "direction": "rollback"}


def _validate_api_base(value: Any, allow_insecure_http: bool) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or value != value.strip()
        or any(ord(character) < 33 or character.isspace() for character in value)
    ):
        raise ProfileRuntimeError("engine api_base is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ProfileRuntimeError("engine api_base is not a valid URL") from exc
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProfileRuntimeError("engine api_base must be an absolute HTTP(S) URL without credentials/query")
    if parsed.scheme == "http" and allow_insecure_http is not True:
        raise ProfileRuntimeError("plaintext engine api_base requires allow_insecure_http=true")
    if parsed.scheme == "https" and allow_insecure_http is True:
        raise ProfileRuntimeError("allow_insecure_http must be false for an HTTPS engine")
    return value.rstrip("/")


def _validate_ca_bundle(runtime: ProfileRuntime, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ProfileRuntimeError("engine ca_bundle must be a short relative path or null")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProfileRuntimeError("engine ca_bundle must stay beneath the profile config directory")
    candidate = runtime.config_dir / relative
    try:
        candidate.relative_to(runtime.config_dir)
    except ValueError as exc:
        raise ProfileRuntimeError("engine ca_bundle escapes the profile config directory") from exc
    try:
        _read_regular_bytes(candidate, maximum=MAX_CA_BUNDLE_BYTES)
    except ProfileRuntimeError as exc:
        raise ProfileRuntimeError("engine ca_bundle does not name a regular profile config file")
    return relative.as_posix()


def validate_engine_config(
    runtime: ProfileRuntime,
    raw: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    value = dict(raw) if raw is not None else read_bounded_json(
        runtime.engine_config_file, maximum=MAX_ENGINE_CONFIG_BYTES
    )
    if set(value) != ENGINE_CONFIG_KEYS:
        missing = sorted(ENGINE_CONFIG_KEYS - set(value))
        extra = sorted(set(value) - ENGINE_CONFIG_KEYS)
        raise ProfileRuntimeError(
            f"engine config schema mismatch (missing={missing}, extra={extra})"
        )
    if value.get("schema") != ENGINE_SCHEMA:
        raise ProfileRuntimeError("engine config schema is unsupported")
    allow_http = value.get("allow_insecure_http")
    if not isinstance(allow_http, bool):
        raise ProfileRuntimeError("engine allow_insecure_http must be boolean")
    allow_remote_mcp = value.get("allow_remote_mcp_ocr")
    if not isinstance(allow_remote_mcp, bool):
        raise ProfileRuntimeError("engine allow_remote_mcp_ocr must be boolean")
    api_base = _validate_api_base(value.get("api_base"), allow_http)
    model = value.get("model")
    if (
        not isinstance(model, str)
        or model != model.strip()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model)
    ):
        raise ProfileRuntimeError("engine model identifier is invalid")
    normalized = {
        "schema": ENGINE_SCHEMA,
        "api_base": api_base,
        "model": model,
        "max_tokens": _bounded_int(value.get("max_tokens"), "max_tokens", 256, 32768),
        "request_timeout": _bounded_int(value.get("request_timeout"), "request_timeout", 5, 300),
        "transport_retries": _bounded_int(value.get("transport_retries"), "transport_retries", 0, 2),
        "ca_bundle": _validate_ca_bundle(runtime, value.get("ca_bundle")),
        "allow_insecure_http": allow_http,
        "allow_remote_mcp_ocr": allow_remote_mcp,
    }
    if normalized != value:
        raise ProfileRuntimeError("engine config values are not canonical")
    token = read_private_single_line(
        runtime.engine_token_file, minimum=16, maximum=2048
    )
    return normalized, token


def configure_engine(
    runtime: ProfileRuntime,
    *,
    api_base: str,
    model: str,
    token: str,
    max_tokens: int = 8192,
    request_timeout: int = 120,
    transport_retries: int = 2,
    ca_bundle: str | None = None,
    allow_insecure_http: bool = False,
    allow_remote_mcp_ocr: bool = False,
) -> dict[str, Any]:
    recover_engine_configuration(runtime)
    value = {
        "schema": ENGINE_SCHEMA,
        "api_base": api_base.rstrip("/"),
        "model": model,
        "max_tokens": max_tokens,
        "request_timeout": request_timeout,
        "transport_retries": transport_retries,
        "ca_bundle": ca_bundle,
        "allow_insecure_http": allow_insecure_http,
        "allow_remote_mcp_ocr": allow_remote_mcp_ocr,
    }
    # Validate non-secret fields before writing either file.
    _validate_api_base(value["api_base"], allow_insecure_http)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise ProfileRuntimeError("engine model identifier is invalid")
    _bounded_int(max_tokens, "max_tokens", 256, 32768)
    _bounded_int(request_timeout, "request_timeout", 5, 300)
    _bounded_int(transport_retries, "transport_retries", 0, 2)
    if not isinstance(allow_remote_mcp_ocr, bool):
        raise ProfileRuntimeError("engine allow_remote_mcp_ocr must be boolean")
    value["ca_bundle"] = _validate_ca_bundle(runtime, ca_bundle)
    runtime.config_dir.mkdir(parents=True, exist_ok=True)
    token_bytes = (str(token) + "\n").encode("utf-8")
    # Validate the new secret before creating a transaction.
    if (
        not 16 <= len(str(token).encode("utf-8")) <= 2048
        or str(token) != str(token).strip()
        or any(character.isspace() for character in str(token))
    ):
        raise ProfileRuntimeError("engine token must be one bounded value without whitespace")

    config_exists = os.path.lexists(runtime.engine_config_file)
    token_exists = os.path.lexists(runtime.engine_token_file)
    if config_exists != token_exists:
        raise ProfileRuntimeError("engine config/token pair is incomplete")
    previous_config: str | None = None
    previous_token: str | None = None
    previous_config_sha: str | None = None
    previous_token_sha: str | None = None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = runtime.config_dir / "engine-backups" / stamp
    if config_exists:
        validate_engine_config(runtime)
        previous_config_bytes = _read_regular_bytes(
            runtime.engine_config_file, maximum=MAX_ENGINE_CONFIG_BYTES
        )
        previous_token_bytes = _read_regular_bytes(
            runtime.engine_token_file, maximum=2050
        )
        backup_dir.mkdir(parents=True)
        config_backup = backup_dir / "engine.json"
        token_backup = backup_dir / "engine.token"
        atomic_write_bytes(config_backup, previous_config_bytes, mode=0o600)
        atomic_write_bytes(token_backup, previous_token_bytes, mode=0o600)
        previous_config = str(config_backup)
        previous_token = str(token_backup)
        previous_config_sha = _strict_hash(
            config_backup, maximum=MAX_ENGINE_CONFIG_BYTES
        )
        previous_token_sha = _strict_hash(token_backup, maximum=2050)

    transaction = {
        "schema": 1,
        "plugin": "document-reader",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "phase": "prepared",
        "previous_config": previous_config,
        "previous_config_sha256": previous_config_sha,
        "previous_token": previous_token,
        "previous_token_sha256": previous_token_sha,
        "new_config_sha256": _sha_bytes(_json_bytes(value)),
        "new_token_sha256": _sha_bytes(token_bytes),
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    transaction_path = _transaction_path(runtime)
    atomic_write_json(transaction_path, transaction)
    _validate_transaction(runtime, transaction)
    try:
        write_private_single_line(
            runtime.engine_token_file, str(token), minimum=16, maximum=2048
        )
        transaction["phase"] = "token_written"
        atomic_write_json(transaction_path, transaction)
        atomic_write_json(runtime.engine_config_file, value)
        transaction["phase"] = "config_written"
        atomic_write_json(transaction_path, transaction)
        validated, _ = validate_engine_config(runtime)
        if (
            _strict_hash(
                runtime.engine_config_file, maximum=MAX_ENGINE_CONFIG_BYTES
            )
            != transaction["new_config_sha256"]
            or _strict_hash(runtime.engine_token_file, maximum=2050)
            != transaction["new_token_sha256"]
        ):
            raise ProfileRuntimeError("engine configuration hashes did not commit atomically")
        transaction_path.unlink()
        return validated
    except Exception as original:
        try:
            recover_engine_configuration(runtime)
        except Exception as recovery:
            raise ProfileRuntimeError(
                f"engine configuration failed ({original}); recovery also failed ({recovery})"
            ) from original
        raise
