from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "document_reader_archive_smoke", ROOT / "scripts" / "smoke_plugin_archive.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load plugin archive smoke validator")
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SMOKE
SPEC.loader.exec_module(SMOKE)


class PluginArchiveSmokePolicyTest(unittest.TestCase):
    def test_runtime_state_and_secret_paths_are_rejected(self):
        for path in (
            ".test-tmp/private/result.json",
            "auth-token",
            "config/receipt.json",
            "history.json",
            "service.token",
            "uploads/client.pdf",
            "state/service.json",
        ):
            with self.subTest(path=path):
                with self.assertRaises(SMOKE.SmokeError):
                    SMOKE._safe_archive_name(path)

    def test_high_confidence_secrets_and_executable_placeholders_are_detected(self):
        self.assertIsNotNone(
            SMOKE.HIGH_CONFIDENCE_SECRET_RE.search(
                b"-----BEGIN PRIVATE KEY-----\nsynthetic-test-only"
            )
        )
        self.assertIsNotNone(
            SMOKE.EXECUTABLE_PLACEHOLDER_RE.search(b"http://your-ocr-host/v1")
        )

    def test_policy_constant_parsers_accept_crlf(self):
        profile = b'PLUGIN_VERSION = "0.1.0"\r\nSERVICE_API_VERSION = 1\r\n'
        desktop = (
            b"const VERSION = '0.1.0'\r\n"
            b"export default { id: 'document-reader', version: VERSION }\r\n"
        )
        self.assertEqual(
            SMOKE._python_constant(profile, "PLUGIN_VERSION", "profile_runtime.py"),
            "0.1.0",
        )
        self.assertEqual(
            SMOKE._python_constant(
                profile, "SERVICE_API_VERSION", "profile_runtime.py"
            ),
            "1",
        )
        self.assertEqual(SMOKE._desktop_version(desktop), "0.1.0")

    def test_dashboard_references_resolve_relative_to_dashboard_root(self):
        references = SMOKE._referenced_files(
            {"entry": "dist/index.js", "api": "plugin_api.py"},
            base=PurePosixPath("dashboard"),
        )
        self.assertEqual(
            references,
            {"dashboard/dist/index.js", "dashboard/plugin_api.py"},
        )


if __name__ == "__main__":
    unittest.main()
