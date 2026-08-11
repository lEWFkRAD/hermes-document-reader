"""Call-time Hermes profile binding for the Document Reader plugin.

Nothing in this module caches ``HERMES_HOME`` or the active profile.  Hermes
0.20 can route requests to several profile backends in one Desktop process, so
every command/API request must resolve its context when it runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


PLUGIN_ID = "document-reader"
PLUGIN_VERSION = "0.1.0"
SERVICE_API_VERSION = 1
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
TASK_PREFIX = "Hermes_DocumentReader_"
PORT_BASE = 28_000
PORT_SPAN = 16_000
TOKEN_BYTES = 48
MAX_CONFIG_BYTES = 64 * 1024
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


class ProfileRuntimeError(RuntimeError):
    """A profile path, identity, or runtime file is unsafe or inconsistent."""


def _canonical(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProfileRuntimeError(f"cannot resolve path {path!s}: {exc}") from exc


def _normalized_path(path: Path) -> str:
    value = str(_canonical(path))
    return os.path.normcase(value) if os.name == "nt" else value


def _fingerprint(home: Path) -> str:
    return hashlib.sha256(_normalized_path(home).encode("utf-8")).hexdigest()


def _call_time_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        raw = os.environ.get("HERMES_HOME", "").strip()
        if raw:
            return Path(raw)
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "").strip()
            return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"
        return Path.home() / ".hermes"
    return Path(get_hermes_home())


def _call_time_profile_name(home: Path) -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
    except ImportError:
        if home.parent.name == "profiles" and PROFILE_RE.fullmatch(home.name):
            return home.name
        return "default"
    return str(get_active_profile_name()).strip() or "default"


def _normalize_profile_name(value: str) -> str:
    try:
        from hermes_cli.profiles import normalize_profile_name, validate_profile_name
    except ImportError:
        normalized = str(value).strip().lower()
        if not PROFILE_RE.fullmatch(normalized):
            raise ProfileRuntimeError(f"invalid Hermes profile name: {value!r}")
        return normalized
    try:
        normalized = normalize_profile_name(str(value))
        validate_profile_name(normalized)
    except (TypeError, ValueError) as exc:
        raise ProfileRuntimeError(f"invalid Hermes profile name: {value!r}") from exc
    return normalized


def _default_profile_root(home: Path) -> Path:
    """Resolve the profile family root without trusting a caller label."""

    if home.parent.name == "profiles" and PROFILE_RE.fullmatch(home.name):
        return _canonical(home.parent.parent)
    try:
        from hermes_constants import get_default_hermes_root
    except ImportError:
        return _profile_root(home)
    try:
        return _canonical(Path(get_default_hermes_root()))
    except (OSError, TypeError, ValueError, ProfileRuntimeError):
        return _profile_root(home)


def _infer_profile_name(home: Path, root: Path) -> str:
    if home == root:
        return "default"
    profiles = _canonical(root / "profiles")
    try:
        relative = home.relative_to(profiles)
    except ValueError:
        return "custom"
    if len(relative.parts) == 1 and PROFILE_RE.fullmatch(relative.parts[0]):
        return relative.parts[0]
    raise ProfileRuntimeError("selected HERMES_HOME is nested inside profiles but is not a profile root")


def _profile_root(home: Path) -> Path:
    if home.parent.name == "profiles" and PROFILE_RE.fullmatch(home.name):
        return home.parent.parent
    return home


def _contained(base: Path, child: Path, label: str) -> Path:
    base = _canonical(base)
    child = _canonical(child)
    try:
        child.relative_to(base)
    except ValueError as exc:
        raise ProfileRuntimeError(f"{label} escapes the selected Hermes profile: {child}") from exc
    return child


@dataclass(frozen=True)
class ProfileRuntime:
    home: Path
    profile_name: str
    fingerprint: str
    owner_id: str
    profile_root: Path
    plugin_root: Path
    data_root: Path
    inbox: Path
    processed: Path
    jobs: Path
    state: Path
    logs: Path
    config_dir: Path
    config_file: Path
    token_file: Path
    engine_config_file: Path
    engine_token_file: Path
    runtime_dir: Path
    releases_dir: Path
    owner_file: Path
    lock_file: Path
    install_dir: Path
    deployment_receipt: Path
    lifecycle_lock: Path
    transaction_journal: Path
    desktop_dir: Path
    desktop_plugin: Path
    desktop_receipt: Path
    task_name: str
    port: int

    def public_identity(self) -> dict[str, Any]:
        return {
            "plugin": PLUGIN_ID,
            "version": PLUGIN_VERSION,
            "api_version": SERVICE_API_VERSION,
            "profile": self.profile_name,
            "profile_fingerprint": self.fingerprint,
            "owner_id": self.owner_id,
            "port": self.port,
        }


def _strict_recorded_port(
    candidate: Path,
    expected_home: Path,
    expected_profile: str,
) -> tuple[int, str]:
    data = read_bounded_json(candidate)
    if set(data) != SERVICE_CONFIG_KEYS:
        raise ProfileRuntimeError(f"service config schema mismatch: {candidate}")
    expected_home = _canonical(expected_home)
    expected_fingerprint = _fingerprint(expected_home)
    expected_owner = hashlib.sha256(
        f"{PLUGIN_ID}\0{expected_fingerprint}".encode("utf-8")
    ).hexdigest()
    expected_root = expected_home / PLUGIN_ID
    expected = {
        "schema": 1,
        "plugin": PLUGIN_ID,
        "api_version": SERVICE_API_VERSION,
        "profile": expected_profile,
        "profile_fingerprint": expected_fingerprint,
        "owner_id": expected_owner,
        "hermes_home": str(expected_home),
        "plugin_root": str(expected_root),
        "data_root": str(expected_root / "data"),
        "inbox": str(expected_root / "data" / "inbox"),
        "processed": str(expected_root / "data" / "processed"),
        "jobs": str(expected_root / "data" / "jobs"),
        "state": str(expected_root / "data" / "state"),
        "logs": str(expected_root / "data" / "logs"),
        "token_file": str(expected_root / "config" / "service.token"),
        "bind": "127.0.0.1",
        "task_name": f"{TASK_PREFIX}{expected_fingerprint[:12]}",
    }
    for key, value in expected.items():
        if data.get(key) != value:
            raise ProfileRuntimeError(f"service config {key} identity mismatch: {candidate}")
    port = data.get("port")
    owner = data.get("owner_id")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ProfileRuntimeError(f"service config port is invalid: {candidate}")
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{64}", owner):
        raise ProfileRuntimeError(f"service config owner is invalid: {candidate}")
    if not isinstance(data.get("version"), str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", data["version"]
    ):
        raise ProfileRuntimeError(f"service config version is invalid: {candidate}")
    if not re.fullmatch(r"[0-9a-f]{32}", str(data.get("instance_id", ""))):
        raise ProfileRuntimeError(f"service config instance is invalid: {candidate}")
    release_id = str(data.get("release_id", ""))
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,96}", release_id):
        raise ProfileRuntimeError(f"service config release is invalid: {candidate}")
    releases = expected_root / "runtime" / "releases"
    release_root = _contained(releases, Path(str(data["release_root"])), "release root")
    if release_root.name != release_id:
        raise ProfileRuntimeError(f"service config release path is inconsistent: {candidate}")
    entry = _contained(release_root, Path(str(data["service_entry"])), "service entry")
    python = _contained(release_root, Path(str(data["runtime_python"])), "runtime python")
    if not entry.is_file() or not python.is_file():
        raise ProfileRuntimeError(f"service config release files are missing: {candidate}")
    return port, owner


def _configured_ports(root: Path, root_profile_name: str) -> dict[int, str]:
    candidates = [
        (root / PLUGIN_ID / "config" / "service.json", root, root_profile_name)
    ]
    profiles = root / "profiles"
    if os.path.lexists(profiles):
        if _is_link_or_reparse(profiles) or not profiles.is_dir():
            raise ProfileRuntimeError("Hermes profiles root must be a regular directory")
        for path in sorted(profiles.iterdir()):
            if not PROFILE_RE.fullmatch(path.name):
                continue
            if _is_link_or_reparse(path) or not path.is_dir():
                raise ProfileRuntimeError(
                    f"Hermes profile root must not be a link/reparse point: {path}"
                )
            candidates.append(
                (path / PLUGIN_ID / "config" / "service.json", path, path.name)
            )
    used: dict[int, str] = {}
    for candidate, expected_home, expected_profile in candidates:
        if not os.path.lexists(candidate):
            continue
        port, owner = _strict_recorded_port(candidate, expected_home, expected_profile)
        if port in used and used[port] != owner:
            raise ProfileRuntimeError(f"two profile configs claim Document Reader port {port}")
        used[port] = owner
    return used


def deterministic_profile_port(
    home: Path,
    profile_name: str,
    fingerprint: str,
    owner_id: str,
) -> int:
    """Pick a stable profile port and avoid every recorded sibling profile.

    Once written to ``service.json`` the configured port wins.  The deterministic
    probe is only for first install; a foreign live listener is never skipped or
    killed later -- lifecycle preflight refuses it.
    """

    existing = home / PLUGIN_ID / "config" / "service.json"
    if os.path.lexists(existing):
        port, recorded_owner = _strict_recorded_port(existing, home, profile_name)
        if recorded_owner != owner_id:
            raise ProfileRuntimeError("selected profile config has a foreign owner")
        return port

    root = _profile_root(home)
    root_profile_name = profile_name if root == home else "default"
    used = _configured_ports(root, root_profile_name)
    offset = int(fingerprint[:8], 16) % PORT_SPAN
    for step in range(PORT_SPAN):
        port = PORT_BASE + ((offset + step) % PORT_SPAN)
        if port not in used or used[port] == owner_id:
            return port
    raise ProfileRuntimeError("no unclaimed deterministic Document Reader port remains")


def resolve_profile_runtime(
    *,
    home: str | Path | None = None,
    profile_name: str | None = None,
) -> ProfileRuntime:
    raw_home = Path(home) if home is not None else _call_time_home()
    if home is not None and not raw_home.is_absolute():
        raise ProfileRuntimeError("an explicit HERMES_HOME must be absolute")
    selected_home = _canonical(raw_home)
    profile_root = _default_profile_root(selected_home)
    inferred_name = _infer_profile_name(selected_home, profile_root)
    call_time_home = _canonical(_call_time_home())
    if selected_home == call_time_home:
        active_name = _normalize_profile_name(_call_time_profile_name(selected_home))
        if active_name != inferred_name:
            raise ProfileRuntimeError(
                "Hermes active profile identity disagrees with the selected HERMES_HOME"
            )
    selected_name = _normalize_profile_name(
        profile_name if profile_name is not None else inferred_name
    )
    if selected_name != inferred_name:
        raise ProfileRuntimeError(
            f"profile {selected_name!r} does not own selected HERMES_HOME {selected_home}"
        )

    fingerprint = _fingerprint(selected_home)
    owner_id = hashlib.sha256(
        f"{PLUGIN_ID}\0{fingerprint}".encode("utf-8")
    ).hexdigest()
    plugin_root = _contained(selected_home, selected_home / PLUGIN_ID, "Document Reader plugin root")
    data_root = _contained(plugin_root, plugin_root / "data", "Document Reader data root")
    config_dir = plugin_root / "config"
    runtime_dir = plugin_root / "runtime"
    install_dir = plugin_root / "install"
    desktop_dir = _contained(
        selected_home,
        selected_home / "desktop-plugins" / PLUGIN_ID,
        "desktop plugin directory",
    )
    port = deterministic_profile_port(selected_home, selected_name, fingerprint, owner_id)
    return ProfileRuntime(
        home=selected_home,
        profile_name=selected_name,
        fingerprint=fingerprint,
        owner_id=owner_id,
        profile_root=profile_root,
        plugin_root=plugin_root,
        data_root=data_root,
        inbox=data_root / "inbox",
        processed=data_root / "processed",
        jobs=data_root / "jobs",
        state=data_root / "state",
        logs=data_root / "logs",
        config_dir=config_dir,
        config_file=config_dir / "service.json",
        token_file=config_dir / "service.token",
        engine_config_file=config_dir / "engine.json",
        engine_token_file=config_dir / "engine.token",
        runtime_dir=runtime_dir,
        releases_dir=runtime_dir / "releases",
        owner_file=runtime_dir / "owner.json",
        lock_file=runtime_dir / "service.lock",
        install_dir=install_dir,
        deployment_receipt=install_dir / "deployment.json",
        lifecycle_lock=install_dir / "lifecycle.lock",
        transaction_journal=install_dir / "transaction.json",
        desktop_dir=desktop_dir,
        desktop_plugin=desktop_dir / "plugin.js",
        desktop_receipt=desktop_dir / ".document-reader-owner.json",
        task_name=f"{TASK_PREFIX}{fingerprint[:12]}",
        port=port,
    )


def create_profile_directories(runtime: ProfileRuntime) -> None:
    for path in (
        runtime.data_root,
        runtime.inbox,
        runtime.processed,
        runtime.jobs,
        runtime.state,
        runtime.logs,
        runtime.config_dir,
        runtime.runtime_dir,
        runtime.releases_dir,
        runtime.install_dir,
        runtime.desktop_dir,
    ):
        _contained(runtime.home, path, "profile directory").mkdir(parents=True, exist_ok=True)


def _unlink_exact_regular(path: Path, expected: os.stat_result, label: str) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise ProfileRuntimeError(f"cannot inspect {label}: {exc}") from exc
    if (
        _is_link_or_reparse(path)
        or not stat.S_ISREG(current.st_mode)
        or not os.path.samestat(expected, current)
    ):
        raise ProfileRuntimeError(f"{label} identity changed before cleanup")
    try:
        path.unlink()
    except OSError as exc:
        raise ProfileRuntimeError(f"cannot remove {label}: {exc}") from exc


def _preserve_private_windows_destination(
    path: Path, expected: os.stat_result
) -> tuple[Path, os.stat_result]:
    _reject_reparse_chain(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ProfileRuntimeError(f"cannot inspect existing private file: {exc}") from exc
    if (
        _is_link_or_reparse(path)
        or not stat.S_ISREG(before.st_mode)
        or not os.path.samestat(expected, before)
    ):
        raise ProfileRuntimeError("existing private file identity changed before backup")
    _validate_windows_secret_acl(path)
    try:
        after_acl = path.lstat()
    except OSError as exc:
        raise ProfileRuntimeError(
            f"cannot re-attest existing private file: {exc}"
        ) from exc
    _reject_reparse_chain(path)
    if _is_link_or_reparse(path) or not os.path.samestat(before, after_acl):
        raise ProfileRuntimeError("existing private file changed during ACL validation")

    backup = path.with_name(f".{path.name}.private-backup")
    try:
        os.link(path, backup, follow_symlinks=False)
    except FileExistsError as exc:
        raise ProfileRuntimeError(
            "an unresolved private-file backup blocks this write"
        ) from exc
    except OSError as exc:
        raise ProfileRuntimeError(
            f"cannot preserve existing private file: {exc}"
        ) from exc

    try:
        backup_info = backup.lstat()
        current = path.lstat()
        if (
            _is_link_or_reparse(backup)
            or _is_link_or_reparse(path)
            or not stat.S_ISREG(backup_info.st_mode)
            or not os.path.samestat(before, backup_info)
            or not os.path.samestat(before, current)
        ):
            raise ProfileRuntimeError(
                "existing private file changed while its backup was created"
            )
        return backup, backup_info
    except Exception:
        try:
            backup_info = backup.lstat()
            if (
                not _is_link_or_reparse(backup)
                and os.path.samestat(before, backup_info)
            ):
                backup.unlink(missing_ok=True)
        except (OSError, ProfileRuntimeError):
            pass
        raise


def _recover_private_windows_destination(
    path: Path,
    backup: Path,
    previous: os.stat_result,
) -> None:
    _reject_reparse_chain(backup)
    try:
        backup_info = backup.lstat()
    except OSError as exc:
        raise ProfileRuntimeError(f"cannot inspect private-file backup: {exc}") from exc
    if (
        _is_link_or_reparse(backup)
        or not stat.S_ISREG(backup_info.st_mode)
        or not os.path.samestat(previous, backup_info)
    ):
        raise ProfileRuntimeError("private-file backup identity changed before recovery")
    _validate_windows_secret_acl(backup)
    final_backup_info = backup.lstat()
    if _is_link_or_reparse(backup) or not os.path.samestat(
        backup_info, final_backup_info
    ):
        raise ProfileRuntimeError("private-file backup changed during ACL validation")

    if os.path.lexists(path):
        current = path.lstat()
        if (
            _is_link_or_reparse(path)
            or not stat.S_ISREG(current.st_mode)
            or not os.path.samestat(previous, current)
        ):
            raise ProfileRuntimeError(
                "refusing to overwrite a foreign path during private-file recovery"
            )
        _validate_windows_secret_acl(path)
    else:
        try:
            os.link(backup, path, follow_symlinks=False)
        except OSError as exc:
            raise ProfileRuntimeError(
                f"cannot restore previous private file: {exc}"
            ) from exc
        restored = path.lstat()
        if (
            _is_link_or_reparse(path)
            or not stat.S_ISREG(restored.st_mode)
            or not os.path.samestat(previous, restored)
        ):
            raise ProfileRuntimeError(
                "restored private file does not match its preserved identity"
            )
        _validate_windows_secret_acl(path)
        final_restored = path.lstat()
        if _is_link_or_reparse(path) or not os.path.samestat(
            restored, final_restored
        ):
            raise ProfileRuntimeError(
                "restored private file changed during ACL validation"
            )

    _unlink_exact_regular(backup, previous, "private-file backup")


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_chain(path.parent)
    private_windows_file = os.name == "nt" and mode == 0o600
    if private_windows_file and os.path.lexists(
        path.with_name(f".{path.name}.private-backup")
    ):
        raise ProfileRuntimeError(
            "an unresolved private-file backup blocks this write"
        )
    initial_info = None
    if os.path.lexists(path):
        initial_info = path.lstat()
        if _is_link_or_reparse(path):
            raise ProfileRuntimeError(f"refusing to replace link/reparse-point file: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    temporary_info = None
    published_info = None
    previous_backup = None
    previous_info = None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
            if private_windows_file:
                _harden_windows_secret_acl(temporary_path)
                temporary_info = temporary_path.lstat()
                if _is_link_or_reparse(temporary_path) or not stat.S_ISREG(
                    temporary_info.st_mode
                ):
                    raise ProfileRuntimeError(
                        "private temporary file changed while its ACL was hardened"
                    )
        if private_windows_file:
            if initial_info is None:
                if os.path.lexists(path):
                    raise ProfileRuntimeError(
                        "private destination appeared while its write was prepared"
                    )
            else:
                previous_backup, previous_info = (
                    _preserve_private_windows_destination(path, initial_info)
                )
        try:
            os.replace(temporary_path, path)
            if private_windows_file:
                candidate_info = path.lstat()
                if (
                    temporary_info is None
                    or _is_link_or_reparse(path)
                    or not os.path.samestat(temporary_info, candidate_info)
                ):
                    raise ProfileRuntimeError(
                        "private file identity changed while it was published"
                    )
                published_info = candidate_info

                # A rename normally preserves the source security descriptor,
                # but that is not a portable filesystem guarantee.  Hosted
                # Windows runners can reapply the destination directory's
                # inherited DACL while publishing a temporary file.  Harden
                # and attest the public path itself before returning it.
                _harden_windows_secret_acl(path)
                final_info = path.lstat()
                if _is_link_or_reparse(path) or not os.path.samestat(
                    published_info, final_info
                ):
                    raise ProfileRuntimeError(
                        "private file identity changed while its published ACL was hardened"
                    )

                if previous_backup is not None and previous_info is not None:
                    _unlink_exact_regular(
                        previous_backup, previous_info, "private-file backup"
                    )
                    previous_backup = None
        except Exception as original:
            if private_windows_file:
                try:
                    if published_info is not None:
                        _unlink_exact_regular(
                            path, published_info, "failed private publication"
                        )
                    if previous_backup is not None and previous_info is not None:
                        _recover_private_windows_destination(
                            path, previous_backup, previous_info
                        )
                        previous_backup = None
                except Exception as recovery_error:
                    raise ProfileRuntimeError(
                        "private write failed and its previous file could not be restored"
                    ) from recovery_error
            raise original
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    payload = json.dumps(dict(value), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload, mode=mode)


def _current_windows_sid() -> str:
    if os.name != "nt":
        raise ProfileRuntimeError("Windows SID requested on a non-Windows host")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    sid = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"S-1(?:-[0-9]+)+", sid):
        raise ProfileRuntimeError("could not resolve the current Windows user SID")
    return sid


def _harden_windows_secret_acl(path: Path) -> None:
    if os.name != "nt":
        return
    user_sid = _current_windows_sid()
    script = (
        "& { param([string]$LiteralPath,[string]$UserSid) "
        "$ErrorActionPreference='Stop'; "
        "$a=Get-Acl -LiteralPath $LiteralPath; "
        "$a.SetAccessRuleProtection($true,$false); "
        "foreach($rule in @($a.Access)){[void]$a.RemoveAccessRuleSpecific($rule)}; "
        "$allow=[Security.AccessControl.AccessControlType]::Allow; "
        "$user=New-Object Security.Principal.SecurityIdentifier($UserSid); "
        "$system=New-Object Security.Principal.SecurityIdentifier('S-1-5-18'); "
        "$userRights=[Security.AccessControl.FileSystemRights]::Read -bor "
        "[Security.AccessControl.FileSystemRights]::Write; "
        "$userRule=New-Object Security.AccessControl.FileSystemAccessRule"
        "($user,$userRights,$allow); "
        "$systemRule=New-Object Security.AccessControl.FileSystemAccessRule"
        "($system,[Security.AccessControl.FileSystemRights]::FullControl,$allow); "
        "$a.AddAccessRule($userRule); $a.AddAccessRule($systemRule); "
        "Set-Acl -LiteralPath $LiteralPath -AclObject $a }"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(path),
            user_sid,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise ProfileRuntimeError("could not restrict the Windows ACL on the service token")
    _validate_windows_secret_acl(path)


def _validate_windows_secret_acl(path: Path) -> None:
    if os.name != "nt":
        return
    user_sid = _current_windows_sid()
    script = (
        "& { param([string]$LiteralPath) "
        "$a=Get-Acl -LiteralPath $LiteralPath; "
        "$r=@($a.Access | ForEach-Object { "
        "$s=$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value; "
        "[pscustomobject]@{sid=$s;type=[string]$_.AccessControlType;inherited=$_.IsInherited} }); "
        "[pscustomobject]@{protected=$a.AreAccessRulesProtected;rules=$r} | "
        "ConvertTo-Json -Compress -Depth 4 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise ProfileRuntimeError("could not inspect the Windows ACL on the service token")
    try:
        value = json.loads(result.stdout)
        rules = value["rules"]
        if isinstance(rules, dict):
            rules = [rules]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ProfileRuntimeError("Windows returned an invalid token ACL") from exc
    if value.get("protected") is not True or not isinstance(rules, list):
        raise ProfileRuntimeError("service token ACL inheritance is not disabled")
    allowed = {user_sid, "S-1-5-18"}
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ProfileRuntimeError("service token ACL rule is invalid")
        sid = str(rule.get("sid", ""))
        if rule.get("type") != "Allow" or rule.get("inherited") is not False or sid not in allowed:
            raise ProfileRuntimeError("service token ACL contains a foreign or inherited principal")
        seen.add(sid)
    if seen != allowed:
        raise ProfileRuntimeError("service token ACL is missing the current user or SYSTEM")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProfileRuntimeError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _reject_reparse_chain(path: Path) -> None:
    """Reject symlink/junction traversal in every existing literal component."""

    current = Path(path).expanduser()
    if not current.is_absolute():
        raise ProfileRuntimeError("private/config path must be absolute")
    while True:
        if os.path.lexists(current) and _is_link_or_reparse(current):
            raise ProfileRuntimeError(
                f"refusing link/reparse-point path component: {current}"
            )
        parent = current.parent
        if parent == current:
            break
        current = parent


def _read_regular_bytes(
    path: Path,
    *,
    maximum: int,
    validate_private_acl: bool = False,
) -> bytes:
    _reject_reparse_chain(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ProfileRuntimeError(f"cannot inspect {path}: {exc}") from exc
    if _is_link_or_reparse(path):
        raise ProfileRuntimeError(f"refusing link/reparse-point file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProfileRuntimeError(f"cannot open {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as exc:
            raise ProfileRuntimeError(f"cannot re-attest {path}: {exc}") from exc
        _reject_reparse_chain(path)
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not os.path.samestat(before, info)
            or not os.path.samestat(after, info)
            or not stat.S_ISREG(info.st_mode)
            or (reparse and attributes & reparse)
        ):
            raise ProfileRuntimeError(f"regular non-reparse file required: {path}")
        if info.st_size <= 0 or info.st_size > maximum:
            raise ProfileRuntimeError(f"invalid file size for {path}: {info.st_size}")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) <= 0 or len(payload) > maximum:
            raise ProfileRuntimeError(f"file grew beyond its limit while reading: {path}")
        if validate_private_acl:
            _validate_windows_secret_acl(path)
        try:
            final_path_info = path.lstat()
        except OSError as exc:
            raise ProfileRuntimeError(f"cannot finally attest {path}: {exc}") from exc
        _reject_reparse_chain(path)
        final_handle_info = os.fstat(descriptor)
        if (
            not os.path.samestat(info, final_handle_info)
            or not os.path.samestat(final_path_info, final_handle_info)
        ):
            raise ProfileRuntimeError(f"file identity changed during validation: {path}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        confirm = b""
        while len(confirm) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(confirm)))
            if not chunk:
                break
            confirm += chunk
        if confirm != payload:
            raise ProfileRuntimeError(f"file content changed during validation: {path}")
        return payload
    finally:
        os.close(descriptor)


def read_bounded_json(path: Path, *, maximum: int = MAX_CONFIG_BYTES) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular_bytes(path, maximum=maximum).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileRuntimeError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileRuntimeError(f"JSON object required in {path}")
    return value


def ensure_profile_token(runtime: ProfileRuntime) -> str:
    if os.path.lexists(runtime.token_file):
        return validate_token_file(runtime.token_file)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    runtime.token_file.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_chain(runtime.token_file.parent)
    try:
        descriptor = os.open(
            runtime.token_file,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return ensure_profile_token(runtime)
    created_info = None
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            handle.write((token + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(runtime.token_file, 0o600)
            created_info = os.fstat(handle.fileno())
            _harden_windows_secret_acl(runtime.token_file)
            after_harden = runtime.token_file.lstat()
            if (
                _is_link_or_reparse(runtime.token_file)
                or not os.path.samestat(created_info, after_harden)
            ):
                raise ProfileRuntimeError(
                    "service token path changed while its ACL was being hardened"
                )
            persisted = validate_token_file(runtime.token_file)
            if persisted != token:
                raise ProfileRuntimeError(
                    "service token changed before creation could be attested"
                )
            final_path_info = runtime.token_file.lstat()
            if (
                _is_link_or_reparse(runtime.token_file)
                or not os.path.samestat(created_info, final_path_info)
            ):
                raise ProfileRuntimeError(
                    "service token identity changed after ACL validation"
                )
            os.lseek(handle.fileno(), 0, os.SEEK_SET)
            if os.read(handle.fileno(), 130) != (token + "\n").encode("utf-8"):
                raise ProfileRuntimeError(
                    "service token content changed after ACL validation"
                )
    except Exception:
        try:
            current = runtime.token_file.lstat()
            if (
                created_info is not None
                and not _is_link_or_reparse(runtime.token_file)
                and os.path.samestat(created_info, current)
            ):
                runtime.token_file.unlink(missing_ok=True)
        except (OSError, ProfileRuntimeError):
            pass
        raise
    return persisted


def validate_token_file(path: Path) -> str:
    value = read_private_single_line(path, minimum=43, maximum=128)
    if not TOKEN_RE.fullmatch(value):
        raise ProfileRuntimeError("service token must be one bounded base64url value")
    return value


def read_private_single_line(
    path: Path,
    *,
    minimum: int,
    maximum: int,
) -> str:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ProfileRuntimeError("private file path must be absolute")
    try:
        _reject_reparse_chain(path)
        info = path.lstat()
        if os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ProfileRuntimeError("private file must not be accessible by group/other")
        raw = _read_regular_bytes(
            path,
            maximum=maximum + 2,
            validate_private_acl=True,
        ).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProfileRuntimeError(f"private file is not strict UTF-8: {exc}") from exc
    value = raw[:-1] if raw.endswith("\n") else raw
    if (
        not minimum <= len(value.encode("utf-8")) <= maximum
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ProfileRuntimeError("private file must contain one bounded value without whitespace")
    return value


def write_private_single_line(
    path: Path,
    value: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    encoded = str(value).encode("utf-8")
    if (
        not minimum <= len(encoded) <= maximum
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ProfileRuntimeError("private value must be bounded and contain no whitespace")
    atomic_write_bytes(path, encoded + b"\n", mode=0o600)
    read_private_single_line(path, minimum=minimum, maximum=maximum)


def loopback_port_open(port: int, *, timeout: float = 0.25) -> bool:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                if sock.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False
