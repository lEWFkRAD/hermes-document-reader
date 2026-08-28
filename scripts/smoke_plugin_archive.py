#!/usr/bin/env python3
"""Validate an extracted, no-.git Document Reader plugin release archive."""

from __future__ import annotations

import argparse
import compileall
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
LOCK_CONTRACTS = {
    "install/locks/windows-cpython-311-x86_64.txt": "windows-cpython-311-x86_64",
    "install/locks/windows-cpython-314-x86_64.txt": "windows-cpython-314-x86_64",
}
EXACT_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;@\\]+)$")
LOCK_ENTRY_RE = re.compile(
    r"^([a-z0-9][a-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)\s+"
    r"((?:--hash=sha256:[0-9a-f]{64})(?:\s+--hash=sha256:[0-9a-f]{64})*)$"
)
LOCK_HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")
REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\."
    r"(?:html|js|json|md|ps1|py|txt|yaml|yml))"
)
FORBIDDEN_PARTS = {
    ".git",
    ".pytest_cache",
    ".test-tmp",
    ".venv",
    "__pycache__",
    "backups",
    "data",
    "docs",
    "history",
    "inbox",
    "jobs",
    "logs",
    "needs-review",
    "node_modules",
    "on-hold",
    "onhold",
    "processed",
    "quarantine",
    "receipts",
    "retry",
    "runtime",
    "state",
    "uploads",
    "venv",
}
FORBIDDEN_SUFFIXES = {
    ".env",
    ".jks",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".pid",
    ".pyc",
    ".pyo",
    ".sock",
    ".token",
}
HIGH_CONFIDENCE_SECRET_RE = re.compile(
    rb"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----|"
    rb"\bgh[opsu]_[A-Za-z0-9]{30,}\b|"
    rb"\bgithub_pat_[A-Za-z0-9_]{50,}\b|"
    rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b|"
    rb"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"
)
EXECUTABLE_PLACEHOLDER_RE = re.compile(
    rb"<hermes-home>|"
    rb"http://your-ocr-host|"
    rb"http://your-vllm-host|"
    rb"C:/Users/youruser",
    re.IGNORECASE,
)
FORBIDDEN_TOKEN_NAME_RE = re.compile(
    r"^\.?(?:(?:api|auth|access|refresh)[-_.]?tokens?|tokens?)(?:\.[^/]*)?$",
    re.IGNORECASE,
)


def _python_constant(raw: bytes, name: str, source: str) -> str:
    pattern = re.compile(
        rb"^" + re.escape(name.encode("ascii"))
        + rb"\s*=\s*(?:[\"']([^\"']+)[\"']|([0-9]+))\s*(?:#.*)?\r?$",
        re.MULTILINE,
    )
    match = pattern.search(raw)
    if match is None:
        raise SmokeError(f"{source} does not declare literal {name}")
    return (match.group(1) or match.group(2)).decode("ascii")


def _desktop_version(raw: bytes) -> str:
    constants = {
        match.group(1): match.group(2)
        for match in re.finditer(
            rb"^(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*=\s*[\"']([^\"']+)[\"'];?\s*\r?$",
            raw,
            re.MULTILINE,
        )
    }
    literal = re.search(rb"\bversion\s*:\s*[\"']([^\"']+)[\"']", raw)
    if literal:
        return literal.group(1).decode("ascii")
    reference = re.search(rb"\bversion\s*:\s*([A-Z][A-Z0-9_]*)\b", raw)
    if reference:
        return constants.get(reference.group(1), b"").decode("ascii")
    return ""


class SmokeError(RuntimeError):
    """The plugin archive violates its install or release contract."""


def _allowlist(repository_root: Path) -> list[str]:
    source = repository_root / "scripts" / "plugin-release-files.json"
    try:
        parsed = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError(f"cannot read plugin release allowlist: {exc}") from exc
    files = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, list) or not files:
        raise SmokeError("plugin release allowlist must contain a non-empty files array")
    normalized = [str(item).replace("\\", "/") for item in files]
    if normalized != sorted(normalized) or len(normalized) != len(set(normalized)):
        raise SmokeError("plugin release allowlist must be sorted and duplicate-free")
    return normalized


def _safe_archive_name(name: str) -> str:
    normalized = name.replace("\\", "/").removeprefix("./")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise SmokeError(f"unsafe ZIP member path: {name}")
    lowered = {part.casefold() for part in path.parts}
    if lowered & FORBIDDEN_PARTS:
        raise SmokeError(f"forbidden runtime/state path in plugin archive: {normalized}")
    if path.parts[0].casefold() == "dist":
        raise SmokeError(f"forbidden root build-output path in plugin archive: {normalized}")
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        raise SmokeError(f"forbidden generated or secret file in plugin archive: {normalized}")
    basename = path.name.casefold()
    if (
        basename == "history.json"
        or basename == "service.token"
        or "ownership" in basename
        or "receipt" in basename
        or basename == ".env"
        or basename.startswith(".env.")
        or FORBIDDEN_TOKEN_NAME_RE.fullmatch(basename)
    ):
        raise SmokeError(f"forbidden runtime receipt or credential in plugin archive: {normalized}")
    return normalized


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _referenced_files(value: Any, *, base: PurePosixPath) -> set[str]:
    references: set[str] = set()
    for string in _iter_strings(value):
        for match in REFERENCE_RE.finditer(string.replace("\\", "/")):
            candidate = PurePosixPath(match.group(1))
            resolved = base / candidate
            references.add(str(resolved))
    return references


def _parse_yaml(raw: bytes, name: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SmokeError(f"{name} is not valid UTF-8 YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SmokeError(f"{name} must contain a YAML mapping")
    return parsed


def _parse_json(raw: bytes, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"{name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SmokeError(f"{name} must contain a JSON object")
    return parsed


def _canonical_requirement_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _parse_exact_requirements(raw: bytes, name: str) -> dict[str, str]:
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise SmokeError(f"{name} is not valid UTF-8") from exc
    pins: dict[str, str] = {}
    for number, source_line in enumerate(text.split("\n"), 1):
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        match = EXACT_PIN_RE.fullmatch(line)
        if match is None:
            raise SmokeError(f"{name}:{number} is not one unconditional exact name==version pin")
        package = _canonical_requirement_name(match.group(1))
        if package in pins:
            raise SmokeError(f"{name} repeats {package}")
        pins[package] = match.group(2)
    if not pins:
        raise SmokeError(f"{name} has no exact pins")
    return pins


def _validate_service_lock(
    raw: bytes, name: str, target: str, direct_requirements: dict[str, str]
) -> None:
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise SmokeError(f"{name} is not valid UTF-8") from exc
    if not text.endswith("\n"):
        raise SmokeError(f"{name} must end with a newline")
    lines = text.split("\n")
    expected_header = [
        "# Hermes Document Reader service dependency lock",
        f"# target: {target}",
        "# sources: install/service-requirements.txt + scripts/lock-inputs/service-bootstrap.txt",
        "# generator: uv 0.12.3; uv pip compile --generate-hashes --only-binary=:all:",
        "# install: python -m pip install --require-hashes --only-binary=:all: --requirement <this-file>",
    ]
    if lines[:5] != expected_header:
        raise SmokeError(f"{name} has the wrong target or generation contract")

    logical: list[tuple[int, str]] = []
    current = ""
    for number, source_line in enumerate(lines[5:], 6):
        line = source_line.strip()
        if not line or line.startswith("#"):
            if current:
                raise SmokeError(f"{name}:{number} interrupts a continued requirement")
            continue
        continued = line.endswith("\\")
        fragment = line[:-1].rstrip() if continued else line
        current = f"{current} {fragment}".strip()
        if not continued:
            logical.append((number, current))
            current = ""
    if current:
        raise SmokeError(f"{name} ends with an unfinished continuation")

    packages: dict[str, str] = {}
    for number, value in logical:
        match = LOCK_ENTRY_RE.fullmatch(value)
        if match is None:
            raise SmokeError(
                f"{name}:{number} must be an exact pin followed only by SHA-256 hashes"
            )
        package = _canonical_requirement_name(match.group(1))
        if package != match.group(1) or package in packages:
            raise SmokeError(f"{name}:{number} has a duplicate or noncanonical package")
        hashes = [LOCK_HASH_RE.fullmatch(item) for item in match.group(3).split()]
        if any(match is None for match in hashes) or len(hashes) != len(
            {match.group(1) for match in hashes if match is not None}
        ):
            raise SmokeError(f"{name}:{number} has invalid or repeated hashes")
        packages[package] = match.group(2)
    if list(packages) != sorted(packages):
        raise SmokeError(f"{name} package pins are not sorted")
    if len(packages) <= len(direct_requirements):
        raise SmokeError(f"{name} is not a transitive dependency lock")
    for package, version in direct_requirements.items():
        if packages.get(package) != version:
            raise SmokeError(f"{name} does not satisfy {package}=={version}")
    if packages.get("pip") != "26.2.1":
        raise SmokeError(f"{name} does not hash pip==26.2.1")
    if packages.get("setuptools") != "84.0.0":
        raise SmokeError(f"{name} does not hash setuptools==84.0.0")


def _import_smoke(root: Path) -> None:
    code = r"""
import importlib
import importlib.util
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location(
    "hermes_document_reader_release",
    root / "__init__.py",
    submodule_search_locations=[str(root)],
)
if spec is None or spec.loader is None:
    raise RuntimeError("could not construct plugin import spec")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
for child in ("cli", "engine_config", "lifecycle", "profile_runtime"):
    importlib.import_module(f"{spec.name}.{child}")

for index, relative in enumerate((
    "dashboard/plugin_api.py",
    "install/profile_service.py",
    "service/ocr_service.py",
)):
    target = root / relative
    if not target.exists():
        raise FileNotFoundError(relative)
    module_name = f"document_reader_smoke_{index}"
    child_spec = importlib.util.spec_from_file_location(module_name, target)
    if child_spec is None or child_spec.loader is None:
        raise RuntimeError(f"could not construct import spec for {relative}")
    child_module = importlib.util.module_from_spec(child_spec)
    sys.modules[module_name] = child_module
    child_spec.loader.exec_module(child_module)

print(f"imported {spec.name} and runtime entry points")
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["HERMES_HOME"] = str(root / ".smoke-hermes-home")
    result = subprocess.run(
        [sys.executable, "-c", code, str(root)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SmokeError(f"plugin entry-point import failed: {detail}")


def smoke_archive(archive: Path, version: str, repository_root: Path) -> None:
    if not SEMVER_RE.fullmatch(version):
        raise SmokeError(f"invalid requested version: {version}")
    expected = _allowlist(repository_root)
    with zipfile.ZipFile(archive) as bundle:
        members = [member for member in bundle.infolist() if not member.is_dir()]
        names = [_safe_archive_name(member.filename) for member in members]
        if names != sorted(names):
            raise SmokeError("plugin ZIP entries must be sorted")
        if names != expected:
            missing = sorted(set(expected) - set(names))
            extra = sorted(set(names) - set(expected))
            raise SmokeError(f"plugin ZIP differs from allowlist; missing={missing}; extra={extra}")
        for member, normalized in zip(members, names, strict=True):
            unix_mode = member.external_attr >> 16
            if unix_mode and (unix_mode & 0o170000) == 0o120000:
                raise SmokeError(f"symbolic links are forbidden in plugin archive: {normalized}")
            payload = bundle.read(member)
            if HIGH_CONFIDENCE_SECRET_RE.search(payload):
                raise SmokeError(f"high-confidence secret material found in {normalized}")
            if Path(normalized).suffix.casefold() in {".html", ".js", ".mjs", ".ps1", ".py"}:
                if EXECUTABLE_PLACEHOLDER_RE.search(payload.replace(b"\\", b"/")):
                    raise SmokeError(
                        f"executable deployment placeholder found in {normalized}"
                    )

        manifest = _parse_yaml(bundle.read("plugin.yaml"), "plugin.yaml")
        if manifest.get("name") != "document-reader":
            raise SmokeError("plugin.yaml has the wrong plugin name")
        if str(manifest.get("version", "")) != version:
            raise SmokeError("plugin.yaml version does not match the archive version")

        if _desktop_version(
            bundle.read("desktop-plugin/document-reader/plugin.js")
        ) != version:
            raise SmokeError("desktop plugin version does not match the archive version")

        profile_runtime = bundle.read("profile_runtime.py")
        service_runtime = bundle.read("service/ocr_service.py")
        if _python_constant(profile_runtime, "PLUGIN_VERSION", "profile_runtime.py") != version:
            raise SmokeError("profile_runtime.py PLUGIN_VERSION does not match the archive version")
        if _python_constant(service_runtime, "VERSION", "service/ocr_service.py") != version:
            raise SmokeError("service/ocr_service.py VERSION does not match the archive version")
        if _python_constant(
            profile_runtime, "SERVICE_API_VERSION", "profile_runtime.py"
        ) != _python_constant(service_runtime, "API_VERSION", "service/ocr_service.py"):
            raise SmokeError("profile and service API versions do not match")

        direct_requirements = _parse_exact_requirements(
            bundle.read("install/service-requirements.txt"),
            "install/service-requirements.txt",
        )
        for package, bootstrap_version in {
            "pip": "26.2.1",
            "setuptools": "84.0.0",
        }.items():
            if package in direct_requirements:
                raise SmokeError(f"service lock input repeats {package}")
            direct_requirements[package] = bootstrap_version
        for lock_name, target in LOCK_CONTRACTS.items():
            _validate_service_lock(
                bundle.read(lock_name), lock_name, target, direct_requirements
            )

        references = _referenced_files(manifest, base=PurePosixPath("."))
        if "dashboard/manifest.json" in names:
            dashboard = _parse_json(
                bundle.read("dashboard/manifest.json"), "dashboard/manifest.json"
            )
            if dashboard.get("name") != "document-reader":
                raise SmokeError("dashboard/manifest.json has the wrong plugin name")
            if str(dashboard.get("version", "")) != version:
                raise SmokeError(
                    "dashboard/manifest.json version does not match the archive version"
                )
            if dashboard.get("entry") != "dist/index.js":
                raise SmokeError(
                    "dashboard/manifest.json must load dist/index.js"
                )
            if dashboard.get("api") != "plugin_api.py":
                raise SmokeError(
                    "dashboard/manifest.json must load plugin_api.py"
                )
            references |= _referenced_files(
                dashboard, base=PurePosixPath("dashboard")
            )
        missing_references = sorted(reference for reference in references if reference not in names)
        if missing_references:
            raise SmokeError(
                f"manifest references missing plugin files: {missing_references}"
            )

        with tempfile.TemporaryDirectory(prefix="document-reader-plugin-") as temporary:
            root = Path(temporary)
            bundle.extractall(root)
            if (root / ".git").exists():
                raise SmokeError("plugin archive unexpectedly contains Git metadata")
            if not compileall.compile_dir(root, quiet=1, force=True):
                raise SmokeError("Python compilation failed in extracted plugin archive")
            _import_smoke(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        smoke_archive(args.archive.resolve(), args.version, args.repository_root.resolve())
    except (OSError, SmokeError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Verified installable Document Reader plugin archive {args.archive.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
