"""CLI surface for the profile-scoped Document Reader lifecycle."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from .engine_config import configure_engine
    from .lifecycle import LifecycleError, LifecycleManager, profile_install_lock
    from .profile_runtime import (
        ProfileRuntimeError,
        create_profile_directories,
        resolve_profile_runtime,
    )
except ImportError:
    from engine_config import configure_engine  # type: ignore
    from lifecycle import LifecycleError, LifecycleManager, profile_install_lock  # type: ignore
    from profile_runtime import (  # type: ignore
        ProfileRuntimeError,
        create_profile_directories,
        resolve_profile_runtime,
    )


MINIMUM_HERMES = (0, 20, 0)


def _require_current_hermes() -> None:
    try:
        from hermes_cli import __version__
    except ImportError as exc:
        raise LifecycleError(
            "Hermes Agent 0.20.0 or newer is required; run this through the "
            "selected profile's `hermes ...` command"
        ) from exc
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(__version__))
    if match is None or tuple(int(part) for part in match.groups()) < MINIMUM_HERMES:
        raise LifecycleError(
            f"Hermes Agent 0.20.0 or newer is required (found {__version__!s})"
        )


def setup_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="document_reader_action", required=True)
    configure = actions.add_parser(
        "configure", help="Write this profile's private OCR engine settings"
    )
    configure.add_argument(
        "--api-base",
        required=True,
        help="OpenAI-compatible base URL; HTTPS unless explicitly overridden",
    )
    configure.add_argument("--model", required=True, help="OCR model identifier")
    configure.add_argument("--max-tokens", type=int, default=8192)
    configure.add_argument("--request-timeout", type=int, default=120)
    configure.add_argument("--transport-retries", type=int, default=2)
    configure.add_argument(
        "--ca-bundle",
        help="Relative path beneath this profile's config directory",
    )
    configure.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Allow plaintext HTTP only for an explicitly trusted endpoint",
    )
    configure.add_argument(
        "--allow-remote-mcp-ocr",
        action="store_true",
        help=(
            "Allow the source-only MCP tool to send selected-profile documents "
            "to this OCR endpoint"
        ),
    )
    install = actions.add_parser(
        "install", help="Install or atomically update this profile's service"
    )
    install.add_argument(
        "--legacy-inbox",
        type=Path,
        help="Copy and verify documents from a legacy inbox; never deletes the source",
    )
    install.add_argument("--no-start", action="store_true", help="Install without starting")
    actions.add_parser("status", help="Verify receipts, task ownership, and service health")
    recover = actions.add_parser("recover", help="Recover an interrupted transaction")
    recover.add_argument("--no-start", action="store_true")
    rollback = actions.add_parser("rollback", help="Restore the previous owned release")
    rollback.add_argument("--no-start", action="store_true")
    actions.add_parser(
        "uninstall",
        help=(
            "Remove the owned task and Desktop deployment while preserving "
            "profile state"
        ),
    )


def _run(args: argparse.Namespace) -> dict[str, Any]:
    _require_current_hermes()
    manager = LifecycleManager(Path(__file__).absolute().parent)
    action = args.document_reader_action
    if action == "configure":
        token = getpass.getpass("Document Reader engine API token: ")
        runtime = resolve_profile_runtime()
        create_profile_directories(runtime)
        with profile_install_lock(runtime):
            config = configure_engine(
                runtime,
                api_base=args.api_base,
                model=args.model,
                token=token,
                max_tokens=args.max_tokens,
                request_timeout=args.request_timeout,
                transport_retries=args.transport_retries,
                ca_bundle=args.ca_bundle,
                allow_insecure_http=args.allow_insecure_http,
                allow_remote_mcp_ocr=args.allow_remote_mcp_ocr,
            )
        return {
            "configured": True,
            "profile": runtime.profile_name,
            "profile_fingerprint": runtime.fingerprint,
            "model": config["model"],
            "restart_required": runtime.config_file.is_file(),
        }
    if action == "install":
        return manager.install(
            start=not args.no_start,
            legacy_inbox=args.legacy_inbox,
        )
    if action == "status":
        return manager.status()
    if action == "recover":
        return manager.recover(start=not args.no_start)
    if action == "rollback":
        return manager.rollback(start=not args.no_start)
    if action == "uninstall":
        return manager.uninstall()
    raise LifecycleError(f"unknown Document Reader action: {action}")


def handle_command(args: argparse.Namespace) -> int:
    try:
        result = _run(args)
    except (LifecycleError, ProfileRuntimeError, OSError, ValueError) as exc:
        print(f"Document Reader: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="document-reader")
    setup_parser(parser)
    return handle_command(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
