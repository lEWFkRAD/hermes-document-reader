import importlib.util
import hashlib
import os
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("anydoc") is None:
    anydoc_stub = types.ModuleType("anydoc")

    class ConvertError(Exception):
        pass

    anydoc_stub.ConvertError = ConvertError
    anydoc_stub.format_from_path = lambda _path: None
    anydoc_stub.format_from_bytes = lambda _data: None
    anydoc_stub.to_markdown_bytes = lambda _data, *, format: ""
    sys.modules["anydoc"] = anydoc_stub
SPEC = importlib.util.spec_from_file_location("document_reader_mcp", ROOT / "mcp" / "anydoc-mcp.py")
mcp_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mcp_module
SPEC.loader.exec_module(mcp_module)


class FakeImage:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class McpInputBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve() / "home"
        self.data = self.home / "document-reader" / "data"
        self.inbox = self.data / "inbox"
        self.processed = self.data / "processed"
        self.config = self.home / "document-reader" / "config"
        for path in (self.inbox, self.processed, self.config):
            path.mkdir(parents=True, exist_ok=True)
        self.runtime = types.SimpleNamespace(
            home=self.home,
            fingerprint="a" * 64,
            data_root=self.data,
            inbox=self.inbox,
            processed=self.processed,
            engine_config_file=self.config / "engine.json",
            engine_token_file=self.config / "engine.token",
        )
        self.document = self.inbox / "scan.pdf"
        self.document.write_bytes(b"%PDF-test")
        self.runtime_patch = mock.patch.object(
            mcp_module, "_selected_runtime", return_value=self.runtime
        )
        self.runtime_patch.start()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HERMES_DOCUMENT_READER_ALLOWED_ROOTS": str(self.inbox),
                "HERMES_DOCUMENT_READER_MAX_INPUT_BYTES": "1024",
                "HERMES_DOCUMENT_READER_MAX_OUTPUT_CHARS": "2000",
                "HERMES_DOCUMENT_READER_MAX_PAGES": "10",
                "HERMES_DOCUMENT_READER_MAX_REMOTE_ATTEMPTS": "10",
                "HERMES_DOCUMENT_READER_OCR_CONCURRENCY": "2",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.runtime_patch.stop()
        self.temp.cleanup()

    def engine(self, *, consent=True, api_base="https://ocr.invalid/v1"):
        return mcp_module.grm_ocr.EngineConfig(
            api_base=api_base,
            api_key="profile-secret-value",
            model="grm-test",
            max_tokens=1024,
            request_timeout=30,
            transport_retries=0,
            ca_bundle=None,
            allow_insecure_http=False,
            allow_remote_mcp_ocr=consent,
        )

    def test_approved_regular_file_is_identity_pinned_and_bounded(self):
        approved = mcp_module.resolve_input(str(self.document))
        self.assertEqual(approved.path, self.document)
        self.assertEqual(mcp_module._read_input(approved), b"%PDF-test")

    def test_relative_outside_and_cross_profile_roots_are_rejected(self):
        with self.assertRaises(PermissionError):
            mcp_module.resolve_input("scan.pdf")
        outside = self.home / "outside.pdf"
        outside.write_bytes(b"x")
        with self.assertRaises(PermissionError):
            mcp_module.resolve_input(str(outside))
        with mock.patch.dict(
            os.environ,
            {"HERMES_DOCUMENT_READER_ALLOWED_ROOTS": str(self.data / "jobs")},
            clear=False,
        ):
            (self.data / "jobs").mkdir()
            with self.assertRaises(PermissionError):
                mcp_module.allowed_roots(self.runtime)

    def test_configured_roots_must_be_absolute_existing_and_can_only_narrow(self):
        nested = self.inbox / "customer-a"
        nested.mkdir()
        with mock.patch.dict(
            os.environ, {"HERMES_DOCUMENT_READER_ALLOWED_ROOTS": str(nested)}, clear=False
        ):
            self.assertEqual(mcp_module.allowed_roots(self.runtime), (nested,))
        with mock.patch.dict(
            os.environ, {"HERMES_DOCUMENT_READER_ALLOWED_ROOTS": "relative-root"}, clear=False
        ):
            with self.assertRaises(PermissionError):
                mcp_module.allowed_roots(self.runtime)
        missing = self.inbox / "missing"
        with mock.patch.dict(
            os.environ, {"HERMES_DOCUMENT_READER_ALLOWED_ROOTS": str(missing)}, clear=False
        ):
            with self.assertRaises(PermissionError):
                mcp_module.allowed_roots(self.runtime)

    def test_root_reparse_chain_is_rejected_before_use(self):
        original = mcp_module.grm_ocr._reject_reparse_chain

        def reject(path, label):
            if label == "document root":
                raise ValueError("simulated junction")
            return original(path, label)

        with mock.patch.object(mcp_module.grm_ocr, "_reject_reparse_chain", side_effect=reject):
            with self.assertRaises(ValueError):
                mcp_module.allowed_roots(self.runtime)

    def test_replacing_approved_file_before_open_fails_closed(self):
        approved = mcp_module.resolve_input(str(self.document))
        original_open = mcp_module.grm_ocr._open_no_follow

        def replace_then_open(path):
            path.unlink()
            path.write_bytes(b"%PDF-other")
            return original_open(path)

        with mock.patch.object(
            mcp_module.grm_ocr, "_open_no_follow", side_effect=replace_then_open
        ):
            with self.assertRaises(PermissionError):
                mcp_module._read_input(approved)

    def test_parent_reparse_swap_after_open_is_rechecked(self):
        approved = mcp_module.resolve_input(str(self.document))
        original = mcp_module.grm_ocr._reject_reparse_chain
        document_checks = 0

        def reject_after_open(path, label):
            nonlocal document_checks
            if label == "document path":
                document_checks += 1
                if document_checks == 2:
                    raise ValueError("simulated parent junction swap")
            return original(path, label)

        with mock.patch.object(
            mcp_module.grm_ocr, "_reject_reparse_chain", side_effect=reject_after_open
        ):
            with self.assertRaises(ValueError):
                mcp_module._read_input(approved)

    def test_selected_profile_change_invalidates_an_approved_handle(self):
        approved = mcp_module.resolve_input(str(self.document))
        other = types.SimpleNamespace(**vars(self.runtime))
        other.home = self.home.parent / "other"
        other.fingerprint = "b" * 64
        with mock.patch.object(mcp_module, "_selected_runtime", return_value=other):
            with self.assertRaises(PermissionError):
                mcp_module._read_input(approved)

    def test_file_growth_after_approval_is_rejected(self):
        approved = mcp_module.resolve_input(str(self.document))
        self.document.write_bytes(b"x" * 1025)
        with self.assertRaises(PermissionError):
            mcp_module._read_input(approved)

    def test_remote_ocr_uses_profile_config_and_consent_not_global_env(self):
        image = self.inbox / "scan.png"
        image.write_bytes(b"image")
        config = self.engine(consent=False)
        with mock.patch.dict(
            os.environ,
            {
                "GRM_OCR_API_BASE": "https://poison.invalid/v1",
                "GRM_OCR_API_KEY": "global-secret",
                "HERMES_DOCUMENT_READER_ALLOW_REMOTE_OCR": "true",
            },
            clear=False,
        ), mock.patch.object(
            mcp_module, "_load_engine_config", return_value=config
        ) as load_config, mock.patch.object(mcp_module, "_plan_pages") as plan, mock.patch.object(
            mcp_module.grm_ocr, "client"
        ) as client:
            with self.assertRaisesRegex(PermissionError, "not enabled"):
                mcp_module.convert_with_ocr(str(image))
            load_config.assert_called_once_with(self.runtime)
            plan.assert_not_called()
            client.assert_not_called()

    def test_engine_endpoint_or_consent_change_discards_ocr_result(self):
        image = self.inbox / "scan.png"
        image.write_bytes(b"image")
        first = self.engine(consent=True)
        second = self.engine(consent=True, api_base="https://changed.invalid/v1")
        plan = mcp_module.PagePlan("image", ((1, 1, 1.0),), 3)
        with mock.patch.object(
            mcp_module, "_load_engine_config", side_effect=(first, second)
        ), mock.patch.object(mcp_module, "_plan_pages", return_value=plan), mock.patch.object(
            mcp_module, "_ocr_pages", return_value="private OCR"
        ), mock.patch.object(mcp_module.grm_ocr, "configure"):
            with self.assertRaises(PermissionError):
                mcp_module.convert_with_ocr(str(image))

    def test_replaced_private_snapshot_is_never_sent_to_remote_ocr(self):
        image = self.inbox / "scan.png"
        image.write_bytes(b"approved image bytes")
        config = self.engine(consent=True)
        plan = mcp_module.PagePlan("image", ((1, 1, 1.0),), 3)

        def replace_snapshot(snapshot, _identity):
            snapshot.unlink()
            snapshot.write_bytes(b"outside replacement bytes")
            return plan

        with mock.patch.object(
            mcp_module, "_load_engine_config", return_value=config
        ), mock.patch.object(
            mcp_module, "_plan_pages", side_effect=replace_snapshot
        ), mock.patch.object(
            mcp_module.grm_ocr, "configure"
        ), mock.patch.object(
            mcp_module.grm_ocr, "ocr_page_raw"
        ) as remote:
            with self.assertRaises(PermissionError):
                mcp_module.convert_with_ocr(str(image))
        remote.assert_not_called()

    def test_pdf_geometry_is_rejected_before_any_render(self):
        class Page:
            render = mock.Mock()

            def get_width(self):
                return 1

            def get_height(self):
                return 100000

            def close(self):
                pass

        page = Page()

        class Document:
            def __len__(self):
                return 1

            def __getitem__(self, _index):
                return page

            def close(self):
                pass

        pdfium = types.SimpleNamespace(PdfDocument=lambda _path: Document())
        with mock.patch.dict(sys.modules, {"pypdfium2": pdfium}):
            with self.assertRaises(ValueError):
                mcp_module._plan_pages(self.document)
        page.render.assert_not_called()

    def test_remote_fanout_is_bounded_and_closes_every_page(self):
        images = [FakeImage() for _ in range(6)]
        plan = mcp_module.PagePlan("image", tuple((1, 1, 1.0) for _ in images), 18)
        approved = mcp_module.resolve_input(str(self.document))
        attempts = []

        def raw(_image, *, on_delta, max_retries):
            attempts.append(max_retries + 1)
            on_delta("page")
            return "page"

        with mock.patch.object(mcp_module, "_iter_pages", return_value=iter(images)), mock.patch.object(
            mcp_module, "_approved_runtime", return_value=self.runtime
        ), mock.patch.object(
            mcp_module, "_assert_engine_current"
        ), mock.patch.object(mcp_module.grm_ocr, "ocr_page_raw", side_effect=raw), mock.patch.object(
            mcp_module.grm_ocr, "raw_to_markdown", side_effect=lambda value: value
        ):
            result = mcp_module._ocr_pages(self.document, plan, approved, self.engine())
        self.assertEqual(result, "\n\n".join("page" for _ in images))
        self.assertEqual(sum(attempts), 6)
        self.assertTrue(all(image.closed for image in images))

    def test_aggregate_output_limit_cancels_and_closes_pending_pages(self):
        images = [FakeImage() for _ in range(4)]
        plan = mcp_module.PagePlan("image", tuple((1, 1, 1.0) for _ in images), 12)
        approved = mcp_module.resolve_input(str(self.document))

        def pages():
            yielded = 0
            try:
                for yielded, image in enumerate(images, start=1):
                    yield image
            finally:
                for image in images[yielded:]:
                    image.close()

        with mock.patch.dict(
            os.environ, {"HERMES_DOCUMENT_READER_MAX_OUTPUT_CHARS": "1000"}, clear=False
        ), mock.patch.object(mcp_module, "_iter_pages", return_value=pages()), mock.patch.object(
            mcp_module, "_approved_runtime", return_value=self.runtime
        ), mock.patch.object(
            mcp_module, "_assert_engine_current"
        ), mock.patch.object(
            mcp_module.grm_ocr, "ocr_page_raw", return_value="x" * 600
        ), mock.patch.object(
            mcp_module.grm_ocr, "raw_to_markdown", side_effect=lambda value: value
        ):
            with self.assertRaises(ValueError):
                mcp_module._ocr_pages(self.document, plan, approved, self.engine())
        self.assertTrue(all(image.closed for image in images))

    def test_cancellation_resistant_remote_work_hits_a_process_fatal_boundary(self):
        image = FakeImage()
        plan = mcp_module.PagePlan("image", ((1, 1, 1.0),), 3)
        approved = mcp_module.resolve_input(str(self.document))
        release = threading.Event()

        def resistant(_image, *, on_delta, max_retries):
            release.wait(30)
            return "late"

        def release_instead_of_exiting():
            release.set()

        with mock.patch.object(
            mcp_module, "_iter_pages", return_value=iter((image,))
        ), mock.patch.object(
            mcp_module, "_approved_runtime", return_value=self.runtime
        ), mock.patch.object(
            mcp_module, "_assert_engine_current"
        ), mock.patch.object(
            mcp_module.grm_ocr, "ocr_page_raw", side_effect=resistant
        ), mock.patch.object(
            mcp_module, "_fatal_unquiesced_remote", side_effect=release_instead_of_exiting
        ) as fatal:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                mcp_module._ocr_pages(
                    self.document,
                    plan,
                    approved,
                    self.engine(),
                    request_timeout=1,
                )
        self.assertLess(time.monotonic() - started, 5)
        fatal.assert_called_once_with()
        self.assertTrue(image.closed)

    def test_local_conversion_runs_in_a_bounded_secret_scrubbed_worker(self):
        launched = {}

        class Process:
            _handle = 1

            def wait(self, timeout):
                launched["timeout"] = timeout
                return 0

            def poll(self):
                return 0

            def kill(self):
                raise AssertionError("successful worker must not be killed")

        def popen(arguments, **kwargs):
            launched["arguments"] = arguments
            launched["environment"] = kwargs["env"]
            Path(arguments[6]).write_text("bounded markdown", encoding="utf-8")
            return Process()

        with mock.patch.dict(
            os.environ,
            {
                "PYTHONPATH": str(self.home / "hostile-imports"),
                "PYTHONHOME": str(self.home / "hostile-python"),
                "PYTHONUSERBASE": str(self.home / "hostile-userbase"),
            },
            clear=False,
        ), mock.patch.object(
            mcp_module.subprocess, "Popen", side_effect=popen
        ), mock.patch.object(
            mcp_module, "_assign_windows_worker_job", return_value=None
        ), mock.patch.object(mcp_module.anydoc, "to_markdown_bytes") as in_process:
            result = mcp_module._to_markdown(b"plain text", ".txt")
        self.assertEqual(result, "bounded markdown")
        self.assertEqual(launched["timeout"], 60)
        self.assertNotIn("GRM_OCR_API_KEY", launched["environment"])
        self.assertNotIn("PYTHONPATH", launched["environment"])
        self.assertNotIn("PYTHONHOME", launched["environment"])
        self.assertNotIn("PYTHONUSERBASE", launched["environment"])
        self.assertEqual(launched["arguments"][1], "-I")
        self.assertEqual(int(launched["arguments"][9]), len(b"plain text"))
        self.assertEqual(
            launched["arguments"][10], hashlib.sha256(b"plain text").hexdigest()
        )
        in_process.assert_not_called()

    def test_local_worker_rejects_a_replaced_snapshot_before_conversion(self):
        source = self.inbox / "worker.txt"
        output = self.inbox / "worker-output.md"
        expected = b"approved"
        source.write_bytes(b"replaced")
        completed = mcp_module.subprocess.run(
            [
                sys.executable,
                "-I",
                str(ROOT / "mcp" / "anydoc-mcp.py"),
                "--local-worker",
                str(source),
                ".txt",
                str(output),
                "2000",
                str(256 * 1024 * 1024),
                str(len(expected)),
                hashlib.sha256(expected).hexdigest(),
            ],
            cwd=str(self.inbox),
            env=mcp_module._worker_environment(),
            stdin=mcp_module.subprocess.DEVNULL,
            stdout=mcp_module.subprocess.DEVNULL,
            stderr=mcp_module.subprocess.DEVNULL,
            close_fds=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 68)
        self.assertFalse(output.exists())

    def test_actual_worker_ignores_a_hostile_pythonpath(self):
        hostile = self.home / "hostile-imports"
        hostile.mkdir()
        marker = hostile / "imported.txt"
        (hostile / "anydoc.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
            "raise RuntimeError('hostile anydoc imported')\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"PYTHONPATH": str(hostile)}, clear=False
        ):
            self.assertEqual(
                mcp_module._to_markdown(b"isolated worker", ".txt"),
                "isolated worker",
            )
        self.assertFalse(marker.exists())

    def test_local_conversion_timeout_terminates_the_worker(self):
        class Process:
            _handle = 1

            def __init__(self):
                self.killed = False
                self.waits = 0

            def wait(self, timeout):
                self.waits += 1
                if self.waits == 1:
                    raise mcp_module.subprocess.TimeoutExpired("worker", timeout)
                return -9

            def poll(self):
                return -9 if self.killed else None

            def kill(self):
                self.killed = True

        process = Process()
        with mock.patch.object(mcp_module.subprocess, "Popen", return_value=process), mock.patch.object(
            mcp_module, "_assign_windows_worker_job", return_value=None
        ):
            with self.assertRaises(ValueError):
                mcp_module._to_markdown(b"plain text", ".txt")
        self.assertTrue(process.killed)

    def test_scanned_pdf_worker_signal_falls_through_to_profile_ocr(self):
        config = self.engine(consent=True)
        plan = mcp_module.PagePlan("pdf", ((1, 1, 1.0),), 3)
        with mock.patch.object(
            mcp_module, "_to_markdown", side_effect=mcp_module._NeedsOcr("OCR needed")
        ), mock.patch.object(
            mcp_module, "_load_engine_config", return_value=config
        ), mock.patch.object(
            mcp_module, "_plan_pages", return_value=plan
        ) as page_plan, mock.patch.object(
            mcp_module, "_ocr_pages", return_value="remote text"
        ) as remote, mock.patch.object(mcp_module.grm_ocr, "configure"):
            result = mcp_module.convert_with_ocr(str(self.document))
        self.assertEqual(result, "remote text")
        page_plan.assert_called_once()
        remote.assert_called_once()

    def test_archive_expansion_is_rejected_before_local_worker_start(self):
        member = types.SimpleNamespace(
            filename="word/document.xml",
            external_attr=0,
            flag_bits=0,
            file_size=129 * 1024 * 1024,
            compress_size=1024,
        )
        archive = mock.MagicMock()
        archive.__enter__.return_value.infolist.return_value = [member]
        with mock.patch.object(mcp_module.zipfile, "ZipFile", return_value=archive), mock.patch.object(
            mcp_module.subprocess, "Popen"
        ) as popen:
            with self.assertRaises(ValueError):
                mcp_module._to_markdown(b"zip", ".docx")
            popen.assert_not_called()

    def test_archive_special_files_and_cross_platform_unsafe_names_are_rejected(self):
        cases = (
            ("../outside", 0),
            ("safe/link", stat.S_IFLNK),
            ("safe/socket", stat.S_IFSOCK),
            ("C:drive-relative", 0),
            ("safe/file:stream", 0),
            ("safe/COM1.txt", 0),
            ("safe/trailing. ", 0),
        )
        for name, mode in cases:
            member = types.SimpleNamespace(
                filename=name,
                external_attr=mode << 16,
                flag_bits=0,
                file_size=1,
                compress_size=1,
            )
            archive = mock.MagicMock()
            archive.__enter__.return_value.infolist.return_value = [member]
            with mock.patch.object(mcp_module.zipfile, "ZipFile", return_value=archive):
                with self.subTest(name=name, mode=mode), self.assertRaises(ValueError):
                    mcp_module._preflight_local_container(b"zip", ".docx")

    def test_public_errors_redact_paths_urls_and_tokens(self):
        hostile = (
            f"failed {self.document} https://private.example/v1 "
            "profile-secret-value"
        )
        with mock.patch.object(mcp_module, "resolve_input", side_effect=RuntimeError(hostile)):
            with self.assertRaises(RuntimeError) as captured:
                mcp_module.convert_document(str(self.document))
        rendered = str(captured.exception)
        self.assertNotIn(str(self.home), rendered)
        self.assertNotIn("private.example", rendered)
        self.assertNotIn("profile-secret-value", rendered)


if __name__ == "__main__":
    unittest.main()
