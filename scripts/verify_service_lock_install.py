"""Verify an isolated Windows service environment against its hashed lock and pip report."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


LOCK_TITLE = "# Hermes Document Reader service dependency lock"
LOCK_SOURCES = (
    "# sources: install/service-requirements.txt + "
    "scripts/lock-inputs/service-bootstrap.txt"
)
LOCK_GENERATOR = (
    "# generator: uv 0.12.3; uv pip compile --generate-hashes --only-binary=:all:"
)
LOCK_INSTALL = (
    "# install: python -m pip install --require-hashes --only-binary=:all: "
    "--requirement <this-file>"
)
LOCK_ENTRY_RE = re.compile(
    r"^([a-z0-9][a-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)\s+"
    r"((?:--hash=sha256:[0-9a-f]{64})(?:\s+--hash=sha256:[0-9a-f]{64})*)$"
)
HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")
SUPPORTED_MINORS = {"3.11": "windows-cpython-311-x86_64", "3.14": "windows-cpython-314-x86_64"}
SUPPORTED_MACHINES = {"amd64", "x86_64"}


class VerificationError(RuntimeError):
    """Raised when an installed service environment does not match its lock."""


@dataclass(frozen=True)
class LockedPackage:
    version: str
    hashes: frozenset[str]


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def parse_lock(path: Path, expected_target: str) -> dict[str, LockedPackage]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise VerificationError(f"cannot read UTF-8 lock {path}: {exc}") from exc
    if not text.endswith("\n"):
        raise VerificationError(f"{path} must end with a newline")
    lines = text.split("\n")
    expected_header = [
        LOCK_TITLE,
        f"# target: {expected_target}",
        LOCK_SOURCES,
        LOCK_GENERATOR,
        LOCK_INSTALL,
    ]
    if lines[:5] != expected_header:
        raise VerificationError(f"{path} has the wrong target or generation contract")

    logical: list[tuple[int, str]] = []
    current = ""
    for number, source_line in enumerate(lines[5:], 6):
        line = source_line.strip()
        if not line or line.startswith("#"):
            if current:
                raise VerificationError(f"{path}:{number} interrupts a continued requirement")
            continue
        continued = line.endswith("\\")
        fragment = line[:-1].rstrip() if continued else line
        current = f"{current} {fragment}".strip()
        if not continued:
            logical.append((number, current))
            current = ""
    if current:
        raise VerificationError(f"{path} ends with an unfinished continuation")

    packages: dict[str, LockedPackage] = {}
    for number, value in logical:
        match = LOCK_ENTRY_RE.fullmatch(value)
        if match is None:
            raise VerificationError(
                f"{path}:{number} must be an exact pin followed only by SHA-256 hashes"
            )
        name = canonical_name(match.group(1))
        if name != match.group(1) or name in packages:
            raise VerificationError(f"{path}:{number} has a duplicate or noncanonical package")
        hashes = [HASH_RE.fullmatch(item) for item in match.group(3).split()]
        if any(item is None for item in hashes):
            raise VerificationError(f"{path}:{number} contains an invalid hash")
        digests = frozenset(item.group(1) for item in hashes if item is not None)
        if len(digests) != len(hashes):
            raise VerificationError(f"{path}:{number} repeats a hash")
        packages[name] = LockedPackage(match.group(2), digests)
    if list(packages) != sorted(packages):
        raise VerificationError(f"{path} package pins are not sorted")
    if not packages:
        raise VerificationError(f"{path} contains no packages")
    return packages


def verify_runtime(expected_minor: str) -> dict[str, str]:
    expected_target = SUPPORTED_MINORS.get(expected_minor)
    if expected_target is None:
        raise VerificationError(f"unsupported Python service-lock lane: {expected_minor}")
    actual_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_minor != expected_minor:
        raise VerificationError(f"expected Python {expected_minor}, running Python {actual_minor}")
    implementation = sys.implementation.name.lower()
    if implementation != "cpython":
        raise VerificationError(f"expected CPython, running {implementation}")
    if sys.platform != "win32" or platform.system() != "Windows":
        raise VerificationError(f"expected Windows, running {sys.platform}/{platform.system()}")
    machine = platform.machine().lower()
    if machine not in SUPPORTED_MACHINES:
        raise VerificationError(f"expected x86_64/AMD64, running {platform.machine()}")
    expected_cache_tag = f"cpython-{expected_minor.replace('.', '')}"
    if sys.implementation.cache_tag != expected_cache_tag:
        raise VerificationError(
            f"expected cache tag {expected_cache_tag}, running {sys.implementation.cache_tag}"
        )
    if sys.prefix == sys.base_prefix:
        raise VerificationError("service lock must be verified inside an isolated virtual environment")
    return {
        "implementation": implementation,
        "python": actual_minor,
        "cache_tag": sys.implementation.cache_tag,
        "platform": sys.platform,
        "machine": machine,
        "target": expected_target,
    }


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be an object")
    return value


def _wheel_tags(url: str, label: str) -> tuple[str, str, str, str]:
    filename = Path(unquote(urlsplit(url).path)).name
    if not filename.lower().endswith(".whl"):
        raise VerificationError(f"{label} did not install from a wheel: {url}")
    components = filename[:-4].rsplit("-", 3)
    if len(components) != 4:
        raise VerificationError(f"{label} has an invalid wheel filename: {filename}")
    python_tag, abi_tag, platform_tag = components[-3:]
    platform_tags = {item.lower() for item in platform_tag.split(".")}
    if "any" not in platform_tags and "win_amd64" not in platform_tags:
        raise VerificationError(f"{label} selected a non-Windows-x64 wheel: {filename}")
    return filename, python_tag, abi_tag, platform_tag


def verify_report(
    report_path: Path,
    lock: dict[str, LockedPackage],
    runtime: dict[str, str],
) -> list[dict[str, str]]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read pip JSON report {report_path}: {exc}") from exc
    report = _require_mapping(report, "pip report")
    if str(report.get("version")) != "1":
        raise VerificationError(f"unsupported pip report version: {report.get('version')!r}")

    environment = _require_mapping(report.get("environment"), "pip report environment")
    expected_environment = {
        "implementation_name": "cpython",
        "python_version": runtime["python"],
        "sys_platform": "win32",
        "platform_system": "Windows",
    }
    for key, expected in expected_environment.items():
        if str(environment.get(key)) != expected:
            raise VerificationError(
                f"pip report environment {key}={environment.get(key)!r}, expected {expected!r}"
            )
    report_machine = str(environment.get("platform_machine", "")).lower()
    if report_machine not in SUPPORTED_MACHINES:
        raise VerificationError(f"pip report environment has unsupported machine {report_machine!r}")

    installs = report.get("install")
    if not isinstance(installs, list):
        raise VerificationError("pip report install must be an array")
    selected: dict[str, dict[str, str]] = {}
    for index, value in enumerate(installs):
        item = _require_mapping(value, f"pip report install[{index}]")
        metadata = _require_mapping(item.get("metadata"), f"pip report install[{index}].metadata")
        name = canonical_name(str(metadata.get("name", "")))
        version = str(metadata.get("version", ""))
        if not name or name in selected:
            raise VerificationError(f"pip report has a missing or duplicate package at install[{index}]")
        expected = lock.get(name)
        if expected is None or expected.version != version:
            raise VerificationError(f"pip report selected unpinned {name}=={version}")
        download = _require_mapping(
            item.get("download_info"), f"pip report install[{index}].download_info"
        )
        url = str(download.get("url", ""))
        filename, python_tag, abi_tag, platform_tag = _wheel_tags(url, name)
        archive = _require_mapping(
            download.get("archive_info"),
            f"pip report install[{index}].download_info.archive_info",
        )
        hashes = _require_mapping(archive.get("hashes"), f"pip report hash set for {name}")
        sha256 = str(hashes.get("sha256", "")).lower()
        if sha256 not in expected.hashes:
            raise VerificationError(f"pip report selected an unhashed artifact for {name}=={version}")
        selected[name] = {
            "name": name,
            "version": version,
            "sha256": sha256,
            "wheel": filename,
            "python_tag": python_tag,
            "abi_tag": abi_tag,
            "platform_tag": platform_tag,
        }
    if set(selected) != set(lock):
        missing = sorted(set(lock) - set(selected))
        extra = sorted(set(selected) - set(lock))
        raise VerificationError(f"pip report differs from lock; missing={missing}; extra={extra}")
    return [selected[name] for name in sorted(selected)]


def verify_inventory(lock: dict[str, LockedPackage]) -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            raise VerificationError("installed distribution is missing Name metadata")
        name = canonical_name(raw_name)
        if name in installed:
            raise VerificationError(f"installed environment repeats distribution {name}")
        installed[name] = distribution.version
    expected = {name: package.version for name, package in lock.items()}
    if installed != expected:
        missing = sorted(set(expected) - set(installed))
        extra = sorted(set(installed) - set(expected))
        drift = sorted(
            f"{name}: installed={installed[name]} locked={expected[name]}"
            for name in set(installed) & set(expected)
            if installed[name] != expected[name]
        )
        raise VerificationError(
            f"installed inventory differs from lock; missing={missing}; extra={extra}; drift={drift}"
        )
    return installed


def verify_install(lock_path: Path, report_path: Path, expected_minor: str) -> str:
    runtime = verify_runtime(expected_minor)
    lock = parse_lock(lock_path, runtime["target"])
    selected = verify_report(report_path, lock, runtime)
    installed = verify_inventory(lock)
    contract = {
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "runtime": runtime,
        "selected_wheels": selected,
        "installed": installed,
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--python-minor", required=True, choices=sorted(SUPPORTED_MINORS))
    args = parser.parse_args()
    try:
        digest = verify_install(args.lock.resolve(), args.report.resolve(), args.python_minor)
    except VerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Verified exact Windows service inventory and wheel selection: sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
