"""Minimal release entrypoint for the profile-owned OCR service.

The service itself owns strict config parsing, authentication, owner locking,
and HTTP handling.  This shim keeps Task Scheduler's action stable and refuses
legacy argument forms that could put tokens or profile paths in process logs.
"""

from __future__ import annotations

import runpy
import os
import stat
import sys
from pathlib import Path


def _owned_site_packages(release_root: Path) -> Path:
    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.dont_write_bytecode
    ):
        raise SystemExit(
            "Document Reader service must start with Python -B -I -S"
        )
    expected_python = release_root / ".venv" / "Scripts" / "python.exe"
    try:
        if Path(sys.executable).resolve(strict=True) != expected_python.resolve(strict=True):
            raise SystemExit("Document Reader service interpreter is not release-owned")
    except OSError as exc:
        raise SystemExit("Document Reader release interpreter is unavailable") from exc
    site_packages = release_root / ".venv" / "Lib" / "site-packages"
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for candidate in (
        release_root,
        release_root / ".venv",
        release_root / ".venv" / "Lib",
        site_packages,
    ):
        try:
            info = os.lstat(candidate)
        except OSError as exc:
            raise SystemExit("Document Reader release runtime is incomplete") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)
        ):
            raise SystemExit("Document Reader release runtime contains a link/reparse point")
    return site_packages


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "--config":
        raise SystemExit("usage: profile_service.py --config <absolute-service.json>")
    config = Path(sys.argv[2])
    if not config.is_absolute():
        raise SystemExit("service config path must be absolute")
    release_root = Path(__file__).resolve().parents[1]
    site_packages = _owned_site_packages(release_root)
    sys.path.append(str(site_packages))
    service = release_root / "service" / "ocr_service.py"
    if not service.is_file():
        raise SystemExit("Document Reader release is incomplete: service runtime missing")
    sys.argv[0] = str(service)
    runpy.run_path(str(service), run_name="__main__")


if __name__ == "__main__":
    main()
