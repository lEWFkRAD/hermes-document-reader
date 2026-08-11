#!/usr/bin/env python3
"""Exercise the real release provision, reuse, and tamper gates on Windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _expect_lifecycle_error(action, label: str, lifecycle) -> str:
    try:
        action()
    except lifecycle.LifecycleError as exc:
        return str(exc)
    raise AssertionError(f"{label} was accepted")


def _assert_environment(release, lifecycle) -> None:
    observed = lifecycle._installed_environment_attestation(release.python)
    for key in ("pip_version", "dependency_set_sha256", "installed_content_sha256"):
        if observed[key] != release.runtime_attestation[key]:
            raise AssertionError(f"restored runtime changed {key}")


def _installed_tree(root: Path) -> dict[str, str]:
    runtime_root = root / ".venv"
    result: dict[str, str] = {}
    for candidate in sorted(runtime_root.rglob("*")):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        relative = candidate.relative_to(runtime_root).as_posix()
        if candidate.name == "RECORD" and candidate.parent.name.endswith(".dist-info"):
            continue
        result[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return result


def _installed_tree_drift(first_root: Path, second_root: Path) -> dict[str, object]:
    first = _installed_tree(first_root)
    second = _installed_tree(second_root)
    paths = sorted(set(first) | set(second))
    changed = [path for path in paths if first.get(path) != second.get(path)]
    first_site = first_root / ".venv" / "Lib" / "site-packages"
    second_site = second_root / ".venv" / "Lib" / "site-packages"
    return {
        "first_file_count": len(first),
        "second_file_count": len(second),
        "changed_file_count": len(changed),
        "changed_files": changed[:50],
        "first_site_directory_order": os.listdir(first_site)[:100],
        "second_site_directory_order": os.listdir(second_site)[:100],
    }


def run(source_root: Path, temporary_parent: Path) -> dict[str, object]:
    if os.name != "nt" or sys.implementation.name != "cpython":
        raise RuntimeError("the lifecycle provisioning gate requires Windows CPython")
    if sys.version_info[:2] not in {(3, 11), (3, 14)}:
        raise RuntimeError("the lifecycle provisioning gate requires CPython 3.11 or 3.14")

    source_root = source_root.resolve(strict=True)
    temporary_parent = temporary_parent.resolve(strict=True)
    if not temporary_parent.is_dir() or temporary_parent.is_symlink():
        raise RuntimeError("temporary parent must be a regular directory")
    sys.path.insert(0, str(source_root))
    import lifecycle  # pylint: disable=import-outside-toplevel
    import profile_runtime  # pylint: disable=import-outside-toplevel

    work = Path(tempfile.mkdtemp(prefix=f"drg{sys.version_info.minor}-", dir=temporary_parent))
    try:
        if work.parent != temporary_parent or not work.name.startswith(
            f"drg{sys.version_info.minor}-"
        ):
            raise RuntimeError("temporary gate root escaped its parent")
        home = work / "h"
        profile_runtime._default_profile_root = lambda selected: selected
        runtime = profile_runtime.resolve_profile_runtime(home=home)
        profile_runtime.create_profile_directories(runtime)

        expected_source_hash = lifecycle.source_hash(source_root)
        first = lifecycle.stage_release(runtime, source_root, provision=True)
        after_first_source_hash = lifecycle.source_hash(source_root)
        if (
            first.source_hash != expected_source_hash
            or after_first_source_hash != expected_source_hash
        ):
            raise AssertionError("release source changed during the first clean provision")
        second = lifecycle.stage_release(runtime, source_root, provision=True)
        after_second_source_hash = lifecycle.source_hash(source_root)
        if (
            second.source_hash != expected_source_hash
            or after_second_source_hash != expected_source_hash
        ):
            raise AssertionError("release source changed during the second clean provision")
        if (
            first.release_id != second.release_id
            or first.root != second.root
            or first.source_hash != second.source_hash
            or first.runtime_attestation != second.runtime_attestation
        ):
            raise AssertionError(
                "two clean provisions did not converge on one release identity: "
                + json.dumps(
                    {
                        "first_release_id": first.release_id,
                        "second_release_id": second.release_id,
                        "first_root": str(first.root),
                        "second_root": str(second.root),
                        "first_source_hash": first.source_hash,
                        "second_source_hash": second.source_hash,
                        "first_attestation": first.runtime_attestation,
                        "second_attestation": second.runtime_attestation,
                        "installed_tree_drift": _installed_tree_drift(
                            first.root, second.root
                        ),
                    },
                    sort_keys=True,
                )
            )
        _assert_environment(second, lifecycle)

        site_packages = second.root / ".venv" / "Lib" / "site-packages"
        hook_marker = work / "hostile-hook-ran.txt"
        pth = site_packages / "hostile-document-reader.pth"
        sitecustomize = site_packages / "sitecustomize.py"
        if pth.exists() or sitecustomize.exists():
            raise AssertionError("hostile-hook test names collide with installed files")
        hook_program = (
            "from pathlib import Path\n"
            f"Path({str(hook_marker)!r}).write_text('executed', encoding='utf-8')\n"
        )
        pth.write_text(
            f"import pathlib; pathlib.Path({str(hook_marker)!r}).write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )
        sitecustomize.write_text(hook_program, encoding="utf-8")
        try:
            launched = subprocess.run(
                [
                    str(second.python),
                    "-B",
                    "-I",
                    "-S",
                    "-u",
                    str(second.entry),
                    "--config",
                    str(work / "missing-service.json"),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=lifecycle._isolated_subprocess_env(),
            )
            if launched.returncode == 0:
                raise AssertionError("service unexpectedly accepted a missing config")
            if hook_marker.exists():
                raise AssertionError("isolated service launch executed a hostile Python hook")
            hostile_error = _expect_lifecycle_error(
                lambda: lifecycle._installed_environment_attestation(second.python),
                "untracked hostile hooks",
                lifecycle,
            )
        finally:
            pth.unlink(missing_ok=True)
            sitecustomize.unlink(missing_ok=True)
            hook_marker.unlink(missing_ok=True)
        _assert_environment(second, lifecycle)

        bytecode = next(iter(sorted(site_packages.rglob("*.pyc"))), None)
        if bytecode is None:
            raise AssertionError("provisioned runtime contains no attested bytecode")
        original_bytecode = bytecode.read_bytes()
        try:
            bytecode.write_bytes(original_bytecode + b"document-reader-pyc-tamper")
            bytecode_error = _expect_lifecycle_error(
                lambda: lifecycle.stage_release(runtime, source_root, provision=True),
                "modified installed bytecode",
                lifecycle,
            )
        finally:
            bytecode.write_bytes(original_bytecode)
        _assert_environment(second, lifecycle)

        record_file = site_packages / "bs4" / "__init__.py"
        original_record_file = record_file.read_bytes()
        try:
            record_file.write_bytes(original_record_file + b"\n# document-reader-record-tamper\n")
            record_error = _expect_lifecycle_error(
                lambda: lifecycle.stage_release(runtime, source_root, provision=True),
                "modified RECORD-hashed package file",
                lifecycle,
            )
        finally:
            record_file.write_bytes(original_record_file)
        _assert_environment(second, lifecycle)

        return {
            "python": sys.version.split()[0],
            "release_id": second.release_id,
            "runtime_identity": second.runtime_attestation["identity_sha256"],
            "hostile_hook_rejection": hostile_error,
            "bytecode_rejection": bytecode_error,
            "record_rejection": record_error,
        }
    finally:
        resolved = work.resolve(strict=False)
        if resolved.parent != temporary_parent or not resolved.name.startswith(
            f"drg{sys.version_info.minor}-"
        ):
            raise RuntimeError("refusing to clean an unexpected lifecycle gate root")
        shutil.rmtree(resolved, ignore_errors=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--temporary-parent", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.source_root, args.temporary_parent), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
