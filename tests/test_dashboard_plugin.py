from __future__ import annotations

import json
import os
import asyncio
import io
from pathlib import Path

import pytest
from fastapi import HTTPException

from dashboard import plugin_api as dashboard


class DashboardTasks:
    def __init__(self, *, exists=True, matches=True):
        self.exists = exists
        self.matches = matches

    def inspect(self, spec):
        return {"exists": self.exists, "action_matches": self.matches}


def _runtime_attestation(lifecycle_module):
    value = {
        "contract": {
            "implementation": "cpython",
            "python_version": "3.11.15",
            "cache_tag": "cpython-311",
            "platform": "win32",
            "machine": "x86_64",
            "pointer_bits": 64,
        },
        "lock_file": "install/locks/windows-cpython-311-x86_64.txt",
        "lock_sha256": "c" * 64,
        "pip_version": "26.2.1",
        "dependency_set_sha256": "b" * 64,
        "artifact_set_sha256": "d" * 64,
        "installed_content_sha256": "e" * 64,
    }
    value["identity_sha256"] = lifecycle_module.sha256_json(value)
    return value


def _authority(monkeypatch, tmp_path: Path):
    runtime_module = dashboard._profile_runtime
    lifecycle_module = dashboard._lifecycle
    monkeypatch.setattr(runtime_module, "_harden_windows_secret_acl", lambda path: None)
    monkeypatch.setattr(runtime_module, "_validate_windows_secret_acl", lambda path: None)
    monkeypatch.setattr(runtime_module, "_default_profile_root", lambda home: home)
    home = (tmp_path / "hermes").resolve()
    runtime = runtime_module.resolve_profile_runtime(home=home, profile_name="default")
    runtime_module.create_profile_directories(runtime)
    release_root = runtime.releases_dir / "0.1.0-dashboard"
    entry = release_root / "install" / "profile_service.py"
    python = release_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    entry.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    entry.write_text("# service\n", encoding="utf-8")
    python.write_bytes(b"python")
    runtime_module.atomic_write_json(
        release_root / "release.json",
        {
            "schema": 1,
            "plugin": "document-reader",
            "version": "0.1.0",
            "release_id": "0.1.0-dashboard",
            "source_hash": "a" * 64,
            "source_files": {
                relative: "c" * 64
                for relative in lifecycle_module.RELEASE_SOURCE_FILES
            },
            "runtime_attestation": _runtime_attestation(lifecycle_module),
            "provisioned": True,
        },
    )
    desktop_data = b"desktop"
    release = lifecycle_module.Release(
        "0.1.0-dashboard",
        "a" * 64,
        release_root,
        entry,
        python,
        desktop_data,
        lifecycle_module.sha256_bytes(desktop_data),
        _runtime_attestation(lifecycle_module),
    )
    config = lifecycle_module.build_service_config(runtime, release)
    runtime_module.atomic_write_json(runtime.config_file, config)
    runtime_module.write_private_single_line(
        runtime.token_file, "T" * 64, minimum=43, maximum=128
    )
    runtime_module.atomic_write_bytes(runtime.desktop_plugin, b"desktop", mode=0o644)
    desktop_sha = lifecycle_module.sha256_file(runtime.desktop_plugin)
    desktop = {
        "schema": 1,
        "plugin": "document-reader",
        "version": "0.1.0",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "release_id": release.release_id,
        "installed_sha256": desktop_sha,
        "source_sha256": desktop_sha,
        "installed_at": "2026-08-10T00:00:00Z",
        "previous_plugin": None,
        "previous_receipt": None,
    }
    runtime_module.atomic_write_json(runtime.desktop_receipt, desktop)
    deployment = {
        "schema": 1,
        "plugin": "document-reader",
        "version": "0.1.0",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "release_id": release.release_id,
        "source_hash": "a" * 64,
        "service_config_sha256": lifecycle_module.sha256_file(runtime.config_file),
        "desktop_sha256": desktop_sha,
        "task_name": runtime.task_name,
        "port": runtime.port,
        "installed_at": "2026-08-10T00:00:00Z",
        "previous_deployment": None,
        "previous_config": None,
    }
    runtime_module.atomic_write_json(runtime.deployment_receipt, deployment)
    tasks = DashboardTasks()
    monkeypatch.setattr(
        lifecycle_module.LifecycleManager, "_tasks", lambda self: tasks
    )
    return runtime, config, deployment, tasks


def test_dashboard_requires_exact_deployment_desktop_and_task_authority(monkeypatch, tmp_path):
    runtime, config, deployment, tasks = _authority(monkeypatch, tmp_path)
    selected, selected_config, token = dashboard._context(runtime)
    assert selected == runtime
    assert selected_config == config
    assert token == "T" * 64

    runtime.deployment_receipt.unlink()
    with pytest.raises(HTTPException) as missing:
        dashboard._context(runtime)
    assert missing.value.status_code == 503
    dashboard._profile_runtime.atomic_write_json(runtime.deployment_receipt, deployment)

    dashboard._profile_runtime.atomic_write_bytes(
        runtime.desktop_plugin, b"tampered", mode=0o644
    )
    with pytest.raises(HTTPException) as stale:
        dashboard._context(runtime)
    assert stale.value.status_code == 503


def test_dashboard_attests_actual_task_before_reading_token(monkeypatch, tmp_path):
    runtime, _, _, tasks = _authority(monkeypatch, tmp_path)
    tasks.exists = False
    called = False

    def token_read(path):
        nonlocal called
        called = True
        return "T" * 64

    monkeypatch.setattr(dashboard._profile_runtime, "validate_token_file", token_read)
    with pytest.raises(HTTPException) as error:
        dashboard._context(runtime)
    assert error.value.status_code == 503
    assert called is False


def test_dashboard_refuses_lifecycle_journal_and_profile_authority_race(monkeypatch, tmp_path):
    runtime, config, _, _ = _authority(monkeypatch, tmp_path)
    dashboard._profile_runtime.atomic_write_json(runtime.transaction_journal, {"phase": "prepared"})
    with pytest.raises(HTTPException) as transaction:
        dashboard._context(runtime)
    assert transaction.value.status_code == 503
    runtime.transaction_journal.unlink()

    token = "T" * 64
    changed = dict(config)
    changed["instance_id"] = "f" * 32
    monkeypatch.setattr(
        dashboard, "_context", lambda selected=None: (runtime, changed, token)
    )
    with pytest.raises(HTTPException) as race:
        with dashboard._profile_gate(runtime, config, token):
            pass
    assert race.value.status_code == 503


@pytest.mark.parametrize("endpoint", ["state", "upload", "asset"])
def test_dashboard_withholds_state_upload_and_asset_on_call_time_profile_swap(
    monkeypatch, tmp_path, endpoint
):
    runtime, config, _, _ = _authority(monkeypatch, tmp_path)
    other = dashboard._profile_runtime.resolve_profile_runtime(
        home=(tmp_path / "other-hermes").resolve(), profile_name="default"
    )
    token = "T" * 64
    monkeypatch.setattr(
        dashboard, "_attested_context", lambda: (runtime, config, token)
    )
    monkeypatch.setattr(
        dashboard, "_context", lambda selected=None: (runtime, config, token)
    )
    selected = iter((runtime, other))
    monkeypatch.setattr(
        dashboard._profile_runtime,
        "resolve_profile_runtime",
        lambda: next(selected),
    )
    monkeypatch.setattr(
        dashboard._lifecycle, "attest_health", lambda selected_config, selected_token: {}
    )
    if endpoint == "state":
        payload = json.dumps(
            {"profile": runtime.profile_name, "version": config["version"], "queue": []}
        ).encode()
        monkeypatch.setattr(
            dashboard,
            "_request",
            lambda *args, **kwargs: (200, {"content-type": "application/json"}, payload),
        )
        call = dashboard.state()
    elif endpoint == "asset":
        monkeypatch.setattr(
            dashboard,
            "_request",
            lambda *args, **kwargs: (200, {"content-type": "image/jpeg"}, b"jpeg"),
        )
        call = dashboard.asset("20260810-120000-deadbeef", "page_1.jpg")
    else:
        monkeypatch.setattr(
            dashboard,
            "_stream_upload",
            lambda *args, **kwargs: (
                200,
                {"content-type": "application/json"},
                b'{"ok":true,"name":"scan.pdf"}',
            ),
        )

        class Upload:
            filename = "scan.pdf"
            file = io.BytesIO(b"pdf")

            async def close(self):
                return None

        call = dashboard.upload(runtime.profile_name, runtime.fingerprint, Upload())
    with pytest.raises(HTTPException) as swapped:
        asyncio.run(call)
    assert swapped.value.status_code == 503


def test_dashboard_upload_refuses_stale_batch_profile_assertion(monkeypatch, tmp_path):
    runtime, config, _, _ = _authority(monkeypatch, tmp_path)
    token = "T" * 64
    monkeypatch.setattr(
        dashboard, "_attested_context", lambda: (runtime, config, token)
    )
    forwarded = False

    def forward(*args, **kwargs):
        nonlocal forwarded
        forwarded = True
        raise AssertionError("stale profile upload must not be forwarded")

    monkeypatch.setattr(dashboard, "_stream_upload", forward)

    class Upload:
        filename = "scan.pdf"
        file = io.BytesIO(b"pdf")

        async def close(self):
            return None

    with pytest.raises(HTTPException) as stale:
        asyncio.run(dashboard.upload(runtime.profile_name, "f" * 64, Upload()))
    assert stale.value.status_code == 409
    assert forwarded is False


def test_dashboard_state_and_html_strip_paths_secrets_urls_and_active_content():
    sentinels = {
        "home": r"C:\\Users\\secret\\.hermes",
        "token": "TOP-SECRET-TOKEN",
        "url": "https://upstream.example/private",
    }
    state = {
        "queue": [{"name": "scan.pdf", "size": 123, **sentinels}],
        "job": {
            "id": "20260810-120000-deadbeef",
            "name": "scan.pdf",
            "state": "working",
            "paths": sentinels,
            "error": sentinels["url"],
            "partial": "safe text",
            "pages": [{"n": 1, "state": "error", "error": sentinels["home"]}],
            "regions": [],
        },
        "history": [
            {
                "id": "20260810-120000-deadbeef",
                "name": "scan.pdf",
                "links": {
                    "md": "http://127.0.0.1:9999/jobs/20260810-120000-deadbeef/result.md?token=secret"
                },
                "paths": sentinels,
            }
        ],
        "engine": sentinels,
    }
    sanitized = dashboard.sanitize_state(None, state)
    serialized = json.dumps(sanitized)
    for sentinel in sentinels.values():
        assert sentinel not in serialized
    assert "127.0.0.1" not in serialized
    assert sanitized["history"][0]["files"] == {"md": "result.md"}

    cleaned = dashboard.sanitize_html(
        '<script>alert(1)</script><p onclick="steal()">safe</p>'
        '<img src="https://upstream.example/private"><a href="file:///secret">x</a>'
    )
    assert cleaned == "<p>safe</p>x"
    assert "script" not in cleaned and "onclick" not in cleaned
    assert "https://" not in cleaned and "file:" not in cleaned
