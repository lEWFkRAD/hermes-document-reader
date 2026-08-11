from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import venv
from contextlib import contextmanager
from pathlib import Path

import pytest

import engine_config
import lifecycle
import profile_runtime
import cli


@pytest.fixture(autouse=True)
def no_native_acl(monkeypatch):
    # Native ACL behavior is covered by the Windows runtime security suite. The
    # architecture tests run inside sandboxes that intentionally deny WRITE_DAC.
    monkeypatch.setattr(profile_runtime, "_harden_windows_secret_acl", lambda path: None)
    monkeypatch.setattr(profile_runtime, "_validate_windows_secret_acl", lambda path: None)
    monkeypatch.setattr(
        profile_runtime,
        "_default_profile_root",
        lambda home: home.parent.parent if home.parent.name == "profiles" else home,
    )


def _runtime(home: Path, name: str = "default") -> profile_runtime.ProfileRuntime:
    runtime = profile_runtime.resolve_profile_runtime(home=home, profile_name=name)
    profile_runtime.create_profile_directories(runtime)
    return runtime


def _release_source(root: Path) -> dict[str, bytes]:
    captured: dict[str, bytes] = {}
    for index, relative in enumerate(lifecycle.RELEASE_SOURCE_FILES):
        data = f"original-{index}:{relative}".encode("utf-8")
        path = root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        captured[relative] = data
    return captured


def _runtime_attestation(*, artifact: str = "d" * 64) -> dict:
    contract = {
        "implementation": "cpython",
        "python_version": "3.11.15",
        "cache_tag": "cpython-311",
        "platform": "win32",
        "machine": "x86_64",
        "pointer_bits": 64,
    }
    value = {
        "contract": contract,
        "lock_file": "install/locks/windows-cpython-311-x86_64.txt",
        "lock_sha256": "c" * 64,
        "pip_version": "26.1.2",
        "dependency_set_sha256": "b" * 64,
        "artifact_set_sha256": artifact,
        "installed_content_sha256": "e" * 64,
    }
    value["identity_sha256"] = lifecycle.sha256_json(value)
    return value


def _stage_attestation(contract, lock_file, lock_sha, *, artifact="d" * 64):
    value = {
        "contract": dict(contract),
        "lock_file": lock_file,
        "lock_sha256": lock_sha,
        "pip_version": "26.1.2",
        "dependency_set_sha256": "b" * 64,
        "artifact_set_sha256": artifact,
        "installed_content_sha256": "e" * 64,
    }
    value["identity_sha256"] = lifecycle.sha256_json(value)
    return value


def _mock_environment(monkeypatch, contract):
    monkeypatch.setattr(lifecycle, "_interpreter_contract", lambda python: dict(contract))
    monkeypatch.setattr(
        lifecycle,
        "_installed_environment_attestation",
        lambda python: {
            "pip_version": "26.1.2",
            "dependency_set_sha256": "b" * 64,
            "installed_content_sha256": "e" * 64,
        },
    )


def _release(runtime, release_id: str) -> lifecycle.Release:
    root = runtime.releases_dir / release_id
    entry = root / "install" / "profile_service.py"
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    entry.parent.mkdir(parents=True, exist_ok=True)
    python.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("# service\n", encoding="utf-8")
    python.write_bytes(b"python")
    lifecycle.atomic_write_json(
        root / "release.json",
        {
            "schema": 1,
            "plugin": "document-reader",
            "version": "0.1.0",
            "release_id": release_id,
            "source_hash": "a" * 64,
            "source_files": {
                relative: "c" * 64 for relative in lifecycle.RELEASE_SOURCE_FILES
            },
            "runtime_attestation": _runtime_attestation(),
            "provisioned": True,
        },
    )
    desktop_data = f"desktop:{release_id}".encode("utf-8")
    return lifecycle.Release(
        release_id,
        "a" * 64,
        root,
        entry,
        python,
        desktop_data,
        lifecycle.sha256_bytes(desktop_data),
        _runtime_attestation(),
    )


def test_stage_release_uses_one_immutable_service_and_desktop_snapshot(
    monkeypatch, tmp_path
):
    runtime = _runtime(tmp_path / "hermes")
    source_root = tmp_path / "source"
    original = _release_source(source_root)
    contract = lifecycle._current_runtime_contract()
    _mock_environment(monkeypatch, contract)

    def provision(temporary, expected_contract, lock_file, lock_sha):
        # Simulate a checkout/plugin update during the long dependency install.
        for relative in lifecycle.RELEASE_SOURCE_FILES:
            (source_root / Path(relative)).write_bytes(b"concurrent-update:" + relative.encode())
        python = lifecycle._release_python(temporary)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"runtime")
        return _stage_attestation(expected_contract, lock_file, lock_sha)

    monkeypatch.setattr(lifecycle, "_provision_release", provision)
    release = lifecycle.stage_release(runtime, source_root, provision=True)

    for relative in lifecycle.RELEASE_FILES:
        assert (release.root / Path(relative)).read_bytes() == original[relative]
    assert release.desktop_data == original[lifecycle.DESKTOP_RELEASE_FILE]
    assert release.desktop_sha256 == lifecycle.sha256_bytes(release.desktop_data)
    assert release.release_id.endswith(
        f"-{release.runtime_attestation['identity_sha256'][:12]}"
    )

    backup = runtime.install_dir / "desktop-snapshot-test"
    backup.mkdir(parents=True, exist_ok=True)
    lifecycle.deploy_desktop_plugin(
        runtime,
        release.desktop_data,
        release.desktop_sha256,
        release.release_id,
        backup,
    )
    assert runtime.desktop_plugin.read_bytes() == original[lifecycle.DESKTOP_RELEASE_FILE]


def test_release_snapshot_rejects_ancestor_link(tmp_path):
    real_source = tmp_path / "real-source"
    _release_source(real_source)
    linked_source = tmp_path / "linked-source"
    try:
        linked_source.symlink_to(real_source, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links are unavailable: {exc}")
    with pytest.raises(lifecycle.LifecycleError, match="link/reparse"):
        lifecycle.capture_release_source(linked_source)


def test_release_snapshot_rejects_directory_swap(monkeypatch, tmp_path):
    real_source = tmp_path / "real-source"
    _release_source(real_source)
    original_attest = lifecycle._attest_release_source_directories
    calls = 0

    def swap_after_attestation(identities):
        nonlocal calls
        original_attest(identities)
        calls += 1
        if calls == 3:
            service = real_source / "service"
            moved = real_source / "service-before-swap"
            service.rename(moved)
            service.mkdir()
            for name in ("ocr_service.py", "firm.html"):
                (service / name).write_bytes((moved / name).read_bytes())

    monkeypatch.setattr(
        lifecycle, "_attest_release_source_directories", swap_after_attestation
    )
    with pytest.raises(lifecycle.LifecycleError, match="source directory changed"):
        lifecycle.capture_release_source(real_source)


def test_resolved_dependency_set_is_part_of_release_identity(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    source_root = tmp_path / "source"
    _release_source(source_root)
    artifact_hashes = iter(("1" * 64, "2" * 64))
    active = {"attestation": None}
    contract = lifecycle._current_runtime_contract()
    _mock_environment(monkeypatch, contract)

    def provision(temporary, expected_contract, lock_file, lock_sha):
        python = lifecycle._release_python(temporary)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"runtime")
        active["attestation"] = _stage_attestation(
            expected_contract,
            lock_file,
            lock_sha,
            artifact=next(artifact_hashes),
        )
        return active["attestation"]

    monkeypatch.setattr(lifecycle, "_provision_release", provision)
    first = lifecycle.stage_release(runtime, source_root, provision=True)
    second = lifecycle.stage_release(runtime, source_root, provision=True)
    assert first.source_hash == second.source_hash
    assert first.release_id != second.release_id
    assert first.root != second.root


def test_interpreter_cache_tag_is_part_of_release_identity(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    source_root = tmp_path / "source"
    _release_source(source_root)
    contracts = iter(
        (
            {
                "implementation": "cpython",
                "python_version": "3.11.15",
                "cache_tag": "cpython-311",
                "platform": "win32",
                "machine": "x86_64",
                "pointer_bits": 64,
            },
            {
                "implementation": "cpython",
                "python_version": "3.14.0",
                "cache_tag": "cpython-314",
                "platform": "win32",
                "machine": "x86_64",
                "pointer_bits": 64,
            },
        )
    )
    active = {"contract": None}
    monkeypatch.setattr(lifecycle, "_current_runtime_contract", lambda: next(contracts))
    monkeypatch.setattr(
        lifecycle, "_interpreter_contract", lambda python: dict(active["contract"])
    )
    monkeypatch.setattr(
        lifecycle,
        "_installed_environment_attestation",
        lambda python: {
            "pip_version": "26.1.2",
            "dependency_set_sha256": "b" * 64,
            "installed_content_sha256": "e" * 64,
        },
    )

    def provision(temporary, expected_contract, lock_file, lock_sha):
        active["contract"] = dict(expected_contract)
        python = lifecycle._release_python(temporary)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"runtime")
        return _stage_attestation(expected_contract, lock_file, lock_sha)

    monkeypatch.setattr(lifecycle, "_provision_release", provision)
    first = lifecycle.stage_release(runtime, source_root, provision=True)
    second = lifecycle.stage_release(runtime, source_root, provision=True)
    assert first.runtime_attestation["dependency_set_sha256"] == second.runtime_attestation[
        "dependency_set_sha256"
    ]
    assert first.runtime_attestation["contract"]["cache_tag"] != second.runtime_attestation[
        "contract"
    ]["cache_tag"]
    assert first.release_id != second.release_id
    assert first.root != second.root


def test_runtime_contract_uses_build_metadata_under_sanitized_environment(monkeypatch):
    monkeypatch.delenv("PROCESSOR_ARCHITECTURE", raising=False)
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    environment = lifecycle._isolated_subprocess_env()
    assert "PROCESSOR_ARCHITECTURE" not in environment
    assert "PROCESSOR_ARCHITEW6432" not in environment
    current = lifecycle._current_runtime_contract()
    inspected = lifecycle._interpreter_contract(Path(os.sys.executable))
    assert current["machine"] == "x86_64"
    assert inspected == current


def test_existing_release_refuses_installed_content_drift(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    source_root = tmp_path / "source"
    _release_source(source_root)
    contract = lifecycle._current_runtime_contract()
    environment_hashes = iter(("e" * 64, "f" * 64))
    monkeypatch.setattr(lifecycle, "_interpreter_contract", lambda python: dict(contract))
    monkeypatch.setattr(
        lifecycle,
        "_installed_environment_attestation",
        lambda python: {
            "pip_version": "26.1.2",
            "dependency_set_sha256": "b" * 64,
            "installed_content_sha256": next(environment_hashes),
        },
    )

    def provision(temporary, expected_contract, lock_file, lock_sha):
        python = lifecycle._release_python(temporary)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"runtime")
        return _stage_attestation(expected_contract, lock_file, lock_sha)

    monkeypatch.setattr(lifecycle, "_provision_release", provision)
    lifecycle.stage_release(runtime, source_root, provision=True)
    with pytest.raises(lifecycle.LifecycleError, match="environment is inconsistent"):
        lifecycle.stage_release(runtime, source_root, provision=True)


def test_identical_clean_provisions_reuse_exact_release_identity(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    source_root = tmp_path / "source"
    _release_source(source_root)
    contract = lifecycle._current_runtime_contract()
    _mock_environment(monkeypatch, contract)

    def provision(temporary, expected_contract, lock_file, lock_sha):
        python = lifecycle._release_python(temporary)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"runtime")
        return _stage_attestation(expected_contract, lock_file, lock_sha)

    monkeypatch.setattr(lifecycle, "_provision_release", provision)
    first = lifecycle.stage_release(runtime, source_root, provision=True)
    second = lifecycle.stage_release(runtime, source_root, provision=True)
    assert second.release_id == first.release_id
    assert second.root == first.root
    assert second.runtime_attestation == first.runtime_attestation


def test_python_subprocess_isolation_ignores_hostile_pythonpath(monkeypatch, tmp_path):
    runtime_root = tmp_path / "private-runtime"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(runtime_root)
    python = runtime_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site_packages = runtime_root / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{os.sys.version_info.major}.{os.sys.version_info.minor}/site-packages"
    )
    fake = tmp_path / "hostile"
    fake.mkdir()
    (fake / "document_reader_isolation_probe.py").write_text(
        "raise RuntimeError('hostile module imported')\n", encoding="utf-8"
    )
    (site_packages / "document_reader_isolation_probe.py").write_text(
        "VALUE = 'owned-runtime'\n", encoding="utf-8"
    )
    (site_packages / "hostile-hook.pth").write_text(
        str(fake)
        + "\nimport os; os.environ['DOCUMENT_READER_HOSTILE_PTH'] = 'executed'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(fake))
    monkeypatch.setenv("PYTHONHOME", str(fake))
    monkeypatch.setenv("PYTHONUSERBASE", str(fake))
    environment = lifecycle._isolated_subprocess_env()
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "PYTHONUSERBASE" not in environment
    command = lifecycle._private_runtime_command(
        python,
        code=(
            "import os, document_reader_isolation_probe as probe;"
            "print(probe.VALUE);"
            "print(os.environ.get('DOCUMENT_READER_HOSTILE_PTH', 'not-executed'))"
        ),
    )
    assert command[1:5] == ["-B", "-I", "-S", "-c"]
    assert command[6] == str(runtime_root)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["owned-runtime", "not-executed"]


@pytest.mark.parametrize("version", ("3.11.9", "3.14.0"))
def test_private_runtime_command_rejects_setup_python_base_layout(version):
    hosted_python = Path(
        f"C:/hostedtoolcache/windows/Python/{version}/x64/python.exe"
    )
    with pytest.raises(
        lifecycle.LifecycleError,
        match="owned virtual-environment layout",
    ):
        lifecycle._private_runtime_command(hosted_python, code="pass")


def test_shipped_runtime_locks_have_exact_identical_seeded_inventory():
    inventories = []
    artifact_hashes = []
    hashes = []
    for relative in lifecycle.LOCK_FILES:
        inventory, allowed, digest = lifecycle._lock_inventory(
            Path(__file__).resolve().parents[1] / relative
        )
        inventories.append(inventory)
        artifact_hashes.append(allowed)
        hashes.append(digest)
    assert inventories[0] == inventories[1]
    assert hashes[0] == hashes[1]
    assert set(artifact_hashes[0]) == set(inventories[0])
    assert set(artifact_hashes[1]) == set(inventories[1])
    assert all(artifact_hashes[0].values())
    assert all(artifact_hashes[1].values())
    assert inventories[0]["pip"] == "26.1.2"
    assert inventories[0]["setuptools"] == "83.0.0"


def test_pip_report_artifact_hash_must_be_selected_by_lock(tmp_path):
    digest = "1" * 64
    report = tmp_path / "report.json"
    lifecycle.atomic_write_json(
        report,
        {
            "install": [
                {
                    "metadata": {"name": "demo_pkg", "version": "1.0"},
                    "download_info": {
                        "url": "https://packages.example/demo_pkg-1.0-py3-none-any.whl",
                        "archive_info": {"hashes": {"sha256": digest}},
                    },
                }
            ]
        },
    )
    expected = {"demo-pkg": "1.0"}
    assert lifecycle._artifact_set_hash(
        report, expected, {"demo-pkg": frozenset({digest})}
    )
    with pytest.raises(lifecycle.LifecycleError, match="not selected by the lock"):
        lifecycle._artifact_set_hash(
            report, expected, {"demo-pkg": frozenset({"2" * 64})}
        )


def test_private_venv_bootstraps_bundled_pip_only_into_owned_prefix(tmp_path):
    import ensurepip

    runtime_root = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(runtime_root)
    python = runtime_root / "Scripts" / "python.exe"
    wheel = (
        Path(ensurepip.__file__).parent
        / "_bundled"
        / f"pip-{ensurepip.version()}-py3-none-any.whl"
    )
    base_pip = Path(os.sys.base_prefix) / "Lib" / "site-packages" / "pip" / "__init__.py"
    base_before = base_pip.read_bytes() if base_pip.exists() else None
    command = lifecycle._bootstrap_pip_command(
        python,
        (
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--force-reinstall",
            str(wheel),
        ),
    )
    assert command[1:5] == ["-B", "-I", "-S", "-c"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr
    assert (runtime_root / "Lib" / "site-packages" / "pip" / "__init__.py").is_file()
    assert (base_pip.read_bytes() if base_pip.exists() else None) == base_before


def test_record_attestation_allows_only_generated_unhashed_files(tmp_path):
    prefix = tmp_path / "synthetic-venv"
    site = prefix / "Lib" / "site-packages"
    dist = site / "demo-1.0.dist-info"
    scripts = prefix / "Scripts"
    cache = site / "__pycache__"
    dist.mkdir(parents=True)
    scripts.mkdir(parents=True)
    cache.mkdir()
    module = site / "demo.py"
    metadata = dist / "METADATA"
    pyc = cache / "demo.cpython-311.pyc"
    entrypoint = scripts / "demo.exe"
    synthetic_python = scripts / "python.exe"
    module.write_bytes(b"VALUE = 1\n")
    metadata.write_bytes(b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n\n")
    pyc.write_bytes(b"generated-bytecode")
    entrypoint.write_bytes(b"generated-launcher")
    synthetic_python.write_bytes(b"MZ" + b"\0" * (64 * 1024 - 2))

    def hashed(relative, path):
        encoded = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=")
        return f"{relative},sha256={encoded.decode('ascii')},{path.stat().st_size}"

    record = dist / "RECORD"
    entrypoint_record = hashed("../../Scripts/demo.exe", entrypoint)
    record.write_text(
        "\n".join(
            (
                hashed("demo.py", module),
                hashed("demo-1.0.dist-info/METADATA", metadata),
                "__pycache__/demo.cpython-311.pyc,,",
                entrypoint_record,
                "demo-1.0.dist-info/RECORD,,",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    entrypoint.unlink()
    prelude = (
        "import base64,hashlib,importlib.metadata,json,os,pathlib,re,stat,sys;"
        "_distributions=importlib.metadata.distributions;"
        f"importlib.metadata.distributions=lambda *a,**k:_distributions(path=[{str(site)!r}]);"
        f"sys.executable={str(synthetic_python)!r}\n"
    )
    command = [
        os.sys.executable,
        "-B",
        "-I",
        "-S",
        "-c",
        prelude + lifecycle.INSTALLED_ENVIRONMENT_ATTESTATION_SCRIPT,
    ]
    accepted = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert accepted.returncode == 0, accepted.stderr
    original_attestation = json.loads(accepted.stdout)
    alternate_hash = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode("ascii")
    alternate_entrypoint_record = (
        f"../../Scripts/demo.exe,sha256={alternate_hash},108999"
    )
    record.write_text(
        record.read_text(encoding="utf-8").replace(
            entrypoint_record, alternate_entrypoint_record
        ),
        encoding="utf-8",
    )
    alternate_record = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert alternate_record.returncode == 0, alternate_record.stderr
    assert json.loads(alternate_record.stdout) == original_attestation
    record.write_text(
        record.read_text(encoding="utf-8").replace(
            alternate_entrypoint_record, entrypoint_record
        ),
        encoding="utf-8",
    )
    pyc.write_bytes(b"changed-bytecode")
    changed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert changed.returncode == 0, changed.stderr
    assert (
        json.loads(changed.stdout)["installed_content_sha256"]
        != original_attestation["installed_content_sha256"]
    )
    rogue = site / "sitecustomize.py"
    rogue.write_text("raise RuntimeError('untracked')\n", encoding="utf-8")
    untracked = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert untracked.returncode != 0
    assert "untracked non-bytecode file" in untracked.stderr
    rogue.unlink()

    entrypoint.write_bytes(b"generated-launcher")
    present_entrypoint = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert present_entrypoint.returncode != 0
    assert "entrypoint absence is invalid" in present_entrypoint.stderr
    entrypoint.unlink()

    unexpected_script = scripts / "rogue.exe"
    unexpected_script.write_bytes(b"rogue")
    unexpected = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert unexpected.returncode != 0
    assert "unexpected entry" in unexpected.stderr
    unexpected_script.unlink()

    record.write_text(
        record.read_text(encoding="utf-8").replace(hashed("demo.py", module), "demo.py,,"),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert rejected.returncode != 0
    assert "lacks a RECORD hash" in rejected.stderr


def test_installed_content_attestation_is_independent_of_walk_order(tmp_path):
    prefix = tmp_path / "synthetic-venv"
    site = prefix / "Lib" / "site-packages"
    scripts = prefix / "Scripts"
    dist = site / "demo-1.0.dist-info"
    scripts.mkdir(parents=True)
    dist.mkdir(parents=True)
    python = scripts / "python.exe"
    python.write_bytes(b"MZ" + b"\0" * (64 * 1024 - 2))
    module = site / "demo.py"
    metadata = dist / "METADATA"
    module.write_bytes(b"VALUE = 1\n")
    metadata.write_bytes(b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n\n")
    for package in ("zeta", "alpha"):
        cache = site / package / "__pycache__"
        cache.mkdir(parents=True)
        (cache / f"{package}.cpython.pyc").write_bytes(
            f"bytecode:{package}".encode("ascii")
        )

    def record_line(relative, path):
        encoded = base64.urlsafe_b64encode(
            hashlib.sha256(path.read_bytes()).digest()
        ).rstrip(b"=")
        return f"{relative},sha256={encoded.decode('ascii')},{path.stat().st_size}"

    record = dist / "RECORD"
    record.write_text(
        "\n".join(
            (
                record_line("demo.py", module),
                record_line("demo-1.0.dist-info/METADATA", metadata),
                "demo-1.0.dist-info/RECORD,,",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    normal_prelude = f"import sys;sys.executable={str(python)!r}\n"
    reversed_prelude = (
        "import os,sys\n"
        f"sys.executable={str(python)!r}\n"
        "_original_walk=os.walk\n"
        "def _reversed_walk(*args,**kwargs):\n"
        "    for directory,names,files in _original_walk(*args,**kwargs):\n"
        "        names[:]=reversed(names)\n"
        "        files[:]=reversed(files)\n"
        "        yield directory,names,files\n"
        "os.walk=_reversed_walk\n"
    )

    def attest(prelude):
        result = subprocess.run(
            [
                os.sys.executable,
                "-B",
                "-I",
                "-S",
                "-c",
                prelude + lifecycle.INSTALLED_ENVIRONMENT_ATTESTATION_SCRIPT,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=lifecycle._isolated_subprocess_env(),
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["installed_content_sha256"]

    assert attest(normal_prelude) == attest(reversed_prelude)


def test_distribution_entrypoint_removal_rejects_record_mismatch(tmp_path):
    runtime_root = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(runtime_root)
    scripts = runtime_root / "Scripts"
    site = runtime_root / "Lib" / "site-packages"
    dist = site / "demo-1.0.dist-info"
    dist.mkdir()
    metadata = dist / "METADATA"
    metadata.write_bytes(b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n\n")
    entrypoint = scripts / "demo.exe"
    entrypoint.write_bytes(b"tampered-launcher")
    expected = base64.urlsafe_b64encode(hashlib.sha256(b"expected-launcher").digest()).rstrip(
        b"="
    )
    record = dist / "RECORD"
    record.write_text(
        "\n".join(
            (
                f"../../Scripts/demo.exe,sha256={expected.decode('ascii')},{entrypoint.stat().st_size}",
                "demo-1.0.dist-info/RECORD,,",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    python = scripts / "python.exe"
    with pytest.raises(lifecycle.LifecycleError, match="differs from RECORD"):
        lifecycle._remove_distribution_entrypoints(python)
    assert entrypoint.read_bytes() == b"tampered-launcher"


def test_scheduled_service_uses_isolated_interpreter_mode(tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    config = lifecycle.build_service_config(runtime, _release(runtime, "0.1.0-isolated"))
    spec = lifecycle._task_spec(runtime, config)
    assert spec.arguments.startswith('-B -I -S -u "')
    task_script = (
        Path(__file__).resolve().parents[1] / "install" / "windows-task.ps1"
    ).read_text(encoding="utf-8")
    assert "$expectedArguments = '-B -I -S -u \"'" in task_script


def test_private_service_shim_does_not_execute_pth_or_sitecustomize(tmp_path):
    release = tmp_path / "release"
    runtime_root = release / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(runtime_root)
    install_dir = release / "install"
    service_dir = release / "service"
    install_dir.mkdir()
    service_dir.mkdir()
    source_root = Path(__file__).resolve().parents[1]
    shim = install_dir / "profile_service.py"
    shim.write_bytes((source_root / "install" / "profile_service.py").read_bytes())
    marker = tmp_path / "hostile-hook-ran.txt"
    completed = tmp_path / "service-ran.txt"
    config = tmp_path / "service.json"
    config.write_text("{}\n", encoding="utf-8")
    site_packages = runtime_root / "Lib" / "site-packages"
    (site_packages / "hostile.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('pth')\n",
        encoding="utf-8",
    )
    (site_packages / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('sitecustomize')\n",
        encoding="utf-8",
    )
    (service_dir / "ocr_service.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(completed)!r}).write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )
    python = runtime_root / "Scripts" / "python.exe"
    result = subprocess.run(
        [
            str(python),
            "-B",
            "-I",
            "-S",
            "-u",
            str(shim),
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=lifecycle._isolated_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr
    assert completed.read_text(encoding="utf-8") == "ok"
    assert not marker.exists()
    with pytest.raises(lifecycle.LifecycleError, match="attestation failed"):
        lifecycle._installed_environment_attestation(python)
    assert not marker.exists()


def test_checked_hash_bytecode_is_stable_across_clean_recompile(tmp_path):
    runtime_root = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(runtime_root)
    site_packages = runtime_root / "Lib" / "site-packages"
    (site_packages / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    python = runtime_root / "Scripts" / "python.exe"
    lifecycle._prepare_deterministic_bytecode(python, runtime_root)
    compiled = next(site_packages.rglob("demo.*.pyc"))
    first = compiled.read_bytes()
    lifecycle._prepare_deterministic_bytecode(python, runtime_root)
    second = next(site_packages.rglob("demo.*.pyc")).read_bytes()
    assert second == first


def test_stage_release_recovers_only_exact_receipted_orphan(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    source_root = tmp_path / "source"
    _release_source(source_root)
    snapshot = lifecycle.capture_release_source(source_root)
    stage, marker_path = lifecycle._stage_paths(runtime, snapshot)
    assert stage.parent == runtime.plugin_root
    assert marker_path.parent == runtime.plugin_root
    assert len(stage.name) == len(".s-") + 12
    runtime.releases_dir.mkdir(parents=True, exist_ok=True)
    lifecycle.atomic_write_json(
        marker_path, lifecycle._stage_marker(runtime, snapshot, stage)
    )
    stage.mkdir()
    (stage / "orphaned.partial").write_bytes(b"partial")
    contract = lifecycle._current_runtime_contract()
    _mock_environment(monkeypatch, contract)

    def provision(temporary, expected_contract, lock_file, lock_sha):
        assert not (temporary / "orphaned.partial").exists()
        python = lifecycle._release_python(temporary)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"runtime")
        return _stage_attestation(expected_contract, lock_file, lock_sha)

    monkeypatch.setattr(lifecycle, "_provision_release", provision)
    release = lifecycle.stage_release(runtime, source_root, provision=True)
    assert release.root.is_dir()
    assert not os.path.lexists(stage)
    assert not os.path.lexists(marker_path)

    # A similarly named directory without an exact owner receipt is never deleted.
    foreign = runtime.plugin_root / f"{stage.name}-foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("foreign", encoding="utf-8")
    exact_stage, exact_marker = lifecycle._stage_paths(runtime, snapshot)
    exact_stage.mkdir()
    exact_sentinel = exact_stage / "keep.txt"
    exact_sentinel.write_bytes(b"unreceipted-exact-stage")
    with pytest.raises(lifecycle.LifecycleError, match="unreceipted"):
        lifecycle.stage_release(runtime, source_root, provision=True)
    assert exact_stage.is_dir()
    assert exact_sentinel.read_bytes() == b"unreceipted-exact-stage"
    assert (foreign / "keep.txt").read_text(encoding="utf-8") == "foreign"
    assert not exact_marker.exists()


def _desktop_receipt(runtime, release_id: str, content: bytes, *, previous_plugin=None, previous_receipt=None):
    lifecycle.atomic_write_bytes(runtime.desktop_plugin, content, mode=0o644)
    digest = lifecycle.sha256_file(runtime.desktop_plugin)
    receipt = {
        "schema": 1,
        "plugin": "document-reader",
        "version": "0.1.0",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "release_id": release_id,
        "installed_sha256": digest,
        "source_sha256": digest,
        "installed_at": "2026-08-10T00:00:00Z",
        "previous_plugin": previous_plugin,
        "previous_receipt": previous_receipt,
    }
    lifecycle.atomic_write_json(runtime.desktop_receipt, receipt)
    return receipt


def _deployment(runtime, config, desktop_sha, *, previous_config=None, previous_deployment=None):
    return {
        "schema": 1,
        "plugin": "document-reader",
        "version": "0.1.0",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "release_id": config["release_id"],
        "source_hash": "a" * 64,
        "service_config_sha256": lifecycle.sha256_file(runtime.config_file),
        "desktop_sha256": desktop_sha,
        "task_name": runtime.task_name,
        "port": runtime.port,
        "installed_at": "2026-08-10T00:00:00Z",
        "previous_deployment": previous_deployment,
        "previous_config": previous_config,
    }


class FakeTasks:
    def __init__(self, current=None):
        self.current = current
        self.fail_start_once = False
        self.fail_remove_once = False
        self.fail_install_once = False
        self.start_calls = 0

    def inspect(self, spec):
        return {
            "exists": self.current is not None,
            "action_matches": self.current == spec if self.current is not None else False,
        }

    def probe_name(self, task_name):
        return {"exists": self.current is not None, "state": "Ready" if self.current else "Absent"}

    def install(self, spec):
        if self.fail_install_once:
            self.fail_install_once = False
            raise lifecycle.LifecycleError("injected install failure")
        self.current = spec

    def start(self, spec):
        assert self.current == spec
        self.start_calls += 1
        if self.fail_start_once:
            self.fail_start_once = False
            raise lifecycle.LifecycleError("injected start failure")

    def remove(self, spec):
        if self.fail_remove_once:
            self.fail_remove_once = False
            raise lifecycle.LifecycleError("injected remove failure")
        if self.current != spec:
            raise lifecycle.LifecycleError("foreign task")
        self.current = None


def _installed_pair(tmp_path: Path):
    runtime = _runtime(tmp_path / "hermes")
    profile_runtime.write_private_single_line(
        runtime.token_file, "A" * 64, minimum=43, maximum=128
    )
    previous_config = lifecycle.build_service_config(runtime, _release(runtime, "0.1.0-old"))
    lifecycle.atomic_write_json(runtime.config_file, previous_config)
    old_desktop = _desktop_receipt(runtime, "0.1.0-old", b"old desktop")
    previous_deployment = _deployment(
        runtime, previous_config, old_desktop["installed_sha256"]
    )

    backup = runtime.install_dir / "backups" / "prior"
    backup.mkdir(parents=True)
    previous_config_path = backup / "service.json"
    previous_deployment_path = backup / "deployment.json"
    previous_plugin_path = backup / "desktop-plugin.js"
    previous_receipt_path = backup / "desktop-receipt.json"
    lifecycle.atomic_write_json(previous_config_path, previous_config)
    previous_deployment["service_config_sha256"] = lifecycle.sha256_file(previous_config_path)
    lifecycle.atomic_write_json(previous_deployment_path, previous_deployment)
    lifecycle.atomic_write_bytes(previous_plugin_path, b"old desktop", mode=0o644)
    lifecycle.atomic_write_json(previous_receipt_path, old_desktop)

    current_config = lifecycle.build_service_config(runtime, _release(runtime, "0.1.0-new"))
    lifecycle.atomic_write_json(runtime.config_file, current_config)
    current_desktop = _desktop_receipt(
        runtime,
        "0.1.0-new",
        b"new desktop",
        previous_plugin=str(previous_plugin_path),
        previous_receipt=str(previous_receipt_path),
    )
    current_deployment = _deployment(
        runtime,
        current_config,
        current_desktop["installed_sha256"],
        previous_config=str(previous_config_path),
        previous_deployment=str(previous_deployment_path),
    )
    lifecycle.atomic_write_json(runtime.deployment_receipt, current_deployment)
    return runtime, current_config, current_deployment, previous_config, previous_deployment


def _write_interrupted_install(
    runtime,
    *,
    label: str,
    previous_task_exists: bool,
    previous_service_running: bool,
):
    backup = runtime.install_dir / "backups" / label
    previous_config_path = lifecycle._backup_file(
        runtime.config_file, backup, "service.json"
    )
    previous_deployment_path = lifecycle._backup_file(
        runtime.deployment_receipt, backup, "deployment.json"
    )
    previous_plugin_path = lifecycle._backup_file(
        runtime.desktop_plugin, backup, "desktop-plugin.js"
    )
    previous_receipt_path = lifecycle._backup_file(
        runtime.desktop_receipt, backup, "desktop-receipt.json"
    )
    next_config = lifecycle.build_service_config(
        runtime, _release(runtime, f"0.1.0-{label}")
    )
    _desktop_receipt(
        runtime,
        f"0.1.0-{label}",
        f"{label} desktop".encode(),
        previous_plugin=previous_plugin_path,
        previous_receipt=previous_receipt_path,
    )
    transaction = {
        "schema": 1,
        "plugin": "document-reader",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "operation": "install",
        "phase": "desktop_deployed",
        "new_release_id": f"0.1.0-{label}",
        "new_config_sha256": lifecycle.sha256_json(next_config),
        "new_deployment_sha256": None,
        "previous_config": previous_config_path,
        "previous_config_sha256": lifecycle.sha256_file(Path(previous_config_path)),
        "previous_deployment": previous_deployment_path,
        "previous_deployment_sha256": lifecycle.sha256_file(Path(previous_deployment_path)),
        "previous_desktop_plugin": previous_plugin_path,
        "previous_desktop_plugin_sha256": lifecycle.sha256_file(Path(previous_plugin_path)),
        "previous_desktop_receipt": previous_receipt_path,
        "previous_desktop_receipt_sha256": lifecycle.sha256_file(Path(previous_receipt_path)),
        "new_desktop_plugin_sha256": lifecycle.sha256_bytes(
            f"{label} desktop".encode()
        ),
        "previous_task_exists": previous_task_exists,
        "previous_service_running": previous_service_running,
        "started_at": "2026-08-10T00:00:00Z",
    }
    lifecycle.atomic_write_json(runtime.transaction_journal, transaction)


@pytest.mark.parametrize(
    ("kind", "relative", "active"),
    [
        ("default", Path("."), "default"),
        ("named", Path("profiles") / "research", "research"),
        ("custom", Path("custom-home"), "custom"),
    ],
)
def test_call_time_profile_binding_default_named_custom(monkeypatch, tmp_path, kind, relative, active):
    root = (tmp_path / "family").resolve()
    home = (root / relative).resolve()
    monkeypatch.setattr(profile_runtime, "_call_time_home", lambda: home)
    monkeypatch.setattr(profile_runtime, "_call_time_profile_name", lambda selected: active)
    monkeypatch.setattr(profile_runtime, "_default_profile_root", lambda selected: root)
    runtime = profile_runtime.resolve_profile_runtime()
    assert runtime.profile_name == active
    assert runtime.home == home
    assert runtime.plugin_root == home / "document-reader"
    assert runtime.data_root == runtime.plugin_root / "data"


def test_call_time_profile_label_cannot_attest_another_home(monkeypatch, tmp_path):
    root = (tmp_path / "family").resolve()
    home = root / "profiles" / "alpha"
    monkeypatch.setattr(profile_runtime, "_call_time_home", lambda: home)
    monkeypatch.setattr(profile_runtime, "_call_time_profile_name", lambda selected: "beta")
    monkeypatch.setattr(profile_runtime, "_default_profile_root", lambda selected: root)
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="disagrees"):
        profile_runtime.resolve_profile_runtime()


def test_two_profiles_have_disjoint_identity_storage_task_port_and_tokens(tmp_path):
    root = tmp_path / "hermes"
    default = _runtime(root, "default")
    named = _runtime(root / "profiles" / "research", "research")
    default_token = profile_runtime.ensure_profile_token(default)
    named_token = profile_runtime.ensure_profile_token(named)
    assert default.data_root != named.data_root
    assert default.task_name != named.task_name
    assert default.owner_id != named.owner_id
    assert default.port != named.port
    assert default.token_file != named.token_file
    assert default_token != named_token


def test_service_token_creation_reopens_and_attests_exact_created_identity(
    monkeypatch, tmp_path
):
    runtime = _runtime(tmp_path / "hermes")
    monkeypatch.setattr(profile_runtime.os.path, "samestat", lambda left, right: False)
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="path changed"):
        profile_runtime.ensure_profile_token(runtime)
    # A path that no longer attests the created descriptor is never returned or
    # blindly unlinked. The test owns this unchanged stand-in and removes it.
    assert runtime.token_file.exists()
    runtime.token_file.unlink()


def test_service_token_creation_rejects_foreign_acl_postcondition(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path / "hermes")

    def reject_foreign_acl(path):
        raise profile_runtime.ProfileRuntimeError("foreign ACL principal")

    monkeypatch.setattr(
        profile_runtime, "_validate_windows_secret_acl", reject_foreign_acl
    )
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="foreign ACL"):
        profile_runtime.ensure_profile_token(runtime)
    assert not runtime.token_file.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL publication contract")
def test_private_atomic_publish_rehardens_acl_when_replace_drops_it(
    monkeypatch, tmp_path
):
    path = (tmp_path / "engine.token").resolve()
    hardened: set[Path] = set()
    harden_calls: list[Path] = []
    real_replace = profile_runtime.os.replace

    def record_harden(selected):
        selected = Path(selected)
        harden_calls.append(selected)
        hardened.add(selected)

    def drop_acl_on_replace(source, destination):
        real_replace(source, destination)
        # GitHub's hosted Windows temp filesystem exposed this behavior: the
        # published path inherited from its parent even though the sibling
        # temporary file had already been protected.
        hardened.discard(Path(source))
        hardened.discard(Path(destination))

    def require_protected(selected):
        if Path(selected) not in hardened:
            raise profile_runtime.ProfileRuntimeError(
                "service token ACL inheritance is not disabled"
            )

    monkeypatch.setattr(
        profile_runtime, "_harden_windows_secret_acl", record_harden
    )
    monkeypatch.setattr(
        profile_runtime, "_validate_windows_secret_acl", require_protected
    )
    monkeypatch.setattr(profile_runtime.os, "replace", drop_acl_on_replace)

    profile_runtime.write_private_single_line(
        path, "profile-secret-value", minimum=16, maximum=2048
    )

    assert path in hardened
    assert len(harden_calls) == 2
    assert harden_calls[0] != path
    assert harden_calls[1] == path


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL publication contract")
def test_private_atomic_publish_removes_its_file_when_final_acl_hardening_fails(
    monkeypatch, tmp_path
):
    path = (tmp_path / "engine.token").resolve()
    calls = 0

    def fail_public_hardening(selected):
        nonlocal calls
        calls += 1
        if Path(selected) == path:
            raise profile_runtime.ProfileRuntimeError("published ACL rejected")

    monkeypatch.setattr(
        profile_runtime, "_harden_windows_secret_acl", fail_public_hardening
    )

    with pytest.raises(profile_runtime.ProfileRuntimeError, match="published ACL"):
        profile_runtime.atomic_write_bytes(
            path, b"profile-secret-value\n", mode=0o600
        )

    assert calls == 2
    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL publication contract")
def test_private_atomic_publish_restores_exact_existing_journal_on_acl_failure(
    monkeypatch, tmp_path
):
    path = (tmp_path / "transaction.json").resolve()
    previous = b'{"phase":"prepared","profile":"work"}\n'
    replacement = b'{"phase":"token_written","profile":"work"}\n'
    profile_runtime.atomic_write_bytes(path, previous, mode=0o600)
    previous_info = path.lstat()

    def reject_published_acl(selected):
        if Path(selected) == path:
            raise profile_runtime.ProfileRuntimeError("published ACL rejected")

    monkeypatch.setattr(
        profile_runtime, "_harden_windows_secret_acl", reject_published_acl
    )

    with pytest.raises(profile_runtime.ProfileRuntimeError, match="published ACL"):
        profile_runtime.atomic_write_bytes(path, replacement, mode=0o600)

    assert path.read_bytes() == previous
    assert os.path.samestat(previous_info, path.lstat())
    assert not path.with_name(f".{path.name}.private-backup").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL publication contract")
def test_private_atomic_publish_fails_closed_on_crash_backup_residue(tmp_path):
    path = (tmp_path / "transaction.json").resolve()
    previous = b'{"phase":"prepared","profile":"work"}\n'
    profile_runtime.atomic_write_bytes(path, previous, mode=0o600)
    previous_info = path.lstat()
    backup = path.with_name(f".{path.name}.private-backup")
    os.link(path, backup, follow_symlinks=False)

    with pytest.raises(
        profile_runtime.ProfileRuntimeError,
        match="unresolved private-file backup",
    ):
        profile_runtime.atomic_write_bytes(
            path, b'{"phase":"token_written","profile":"work"}\n', mode=0o600
        )

    assert path.read_bytes() == previous
    assert os.path.samestat(previous_info, path.lstat())
    assert os.path.samestat(previous_info, backup.lstat())


def test_remote_mcp_consent_is_explicit_atomic_and_profile_bound(tmp_path):
    root = tmp_path / "hermes"
    default = _runtime(root, "default")
    named = _runtime(root / "profiles" / "research", "research")
    default_config = engine_config.configure_engine(
        default,
        api_base="https://default.example/v1",
        model="default-model",
        token="D" * 32,
    )
    named_config = engine_config.configure_engine(
        named,
        api_base="https://named.example/v1",
        model="named-model",
        token="N" * 32,
        allow_remote_mcp_ocr=True,
    )
    assert default_config["allow_remote_mcp_ocr"] is False
    assert named_config["allow_remote_mcp_ocr"] is True
    assert engine_config.validate_engine_config(default)[1] == "D" * 32
    assert engine_config.validate_engine_config(named)[1] == "N" * 32
    assert default.engine_config_file != named.engine_config_file
    assert default.engine_token_file != named.engine_token_file

    parser = __import__("argparse").ArgumentParser()
    cli.setup_parser(parser)
    args = parser.parse_args(
        [
            "configure",
            "--api-base",
            "https://named.example/v1",
            "--model",
            "named-model",
            "--allow-remote-mcp-ocr",
        ]
    )
    assert args.allow_remote_mcp_ocr is True


def test_private_token_rejects_file_symlink_and_parent_reparse(tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    outside = tmp_path / "outside.token"
    outside.write_text("A" * 64 + "\n", encoding="utf-8")
    try:
        runtime.token_file.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="reparse|link"):
        profile_runtime.validate_token_file(runtime.token_file)
    runtime.token_file.unlink()

    outside_dir = tmp_path / "outside-config"
    outside_dir.mkdir()
    (outside_dir / "engine.token").write_text("B" * 32 + "\n", encoding="utf-8")
    runtime.config_dir.rmdir()
    try:
        runtime.config_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory reparse points are unavailable: {exc}")
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="reparse|link"):
        profile_runtime.read_private_single_line(
            runtime.engine_token_file, minimum=16, maximum=2048
        )


@pytest.mark.parametrize("kind", ["service", "engine"])
def test_private_token_read_refuses_swap_during_acl_attestation(
    monkeypatch, tmp_path, kind
):
    runtime = _runtime(tmp_path / "hermes")
    if kind == "service":
        path, original, replacement = runtime.token_file, "A" * 64, "B" * 64
        minimum, maximum = 43, 128
    else:
        path, original, replacement = runtime.engine_token_file, "C" * 32, "D" * 32
        minimum, maximum = 16, 2048
    profile_runtime.atomic_write_bytes(
        path, (original + "\n").encode(), mode=0o600
    )

    def swap_during_acl(selected):
        assert selected == path
        try:
            selected.write_text(replacement + "\n", encoding="utf-8")
        except PermissionError as exc:
            raise profile_runtime.ProfileRuntimeError(
                "open token handle prevented path swap"
            ) from exc

    monkeypatch.setattr(
        profile_runtime, "_validate_windows_secret_acl", swap_during_acl
    )
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="changed|prevented"):
        profile_runtime.read_private_single_line(
            path, minimum=minimum, maximum=maximum
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_service_and_engine_tokens_reject_windows_junction_parent(tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    outside = tmp_path / "junction-target"
    outside.mkdir()
    (outside / "service.token").write_text("A" * 64 + "\n", encoding="utf-8")
    (outside / "engine.token").write_text("B" * 32 + "\n", encoding="utf-8")
    runtime.config_dir.rmdir()
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(runtime.config_dir), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("junction creation is unavailable")
    try:
        with pytest.raises(profile_runtime.ProfileRuntimeError, match="reparse"):
            profile_runtime.validate_token_file(runtime.token_file)
        with pytest.raises(profile_runtime.ProfileRuntimeError, match="reparse"):
            profile_runtime.read_private_single_line(
                runtime.engine_token_file, minimum=16, maximum=2048
            )
    finally:
        os.rmdir(runtime.config_dir)


@pytest.mark.parametrize(
    "api_base",
    ["https://example.test/v1\rInjected", "https://example.test/v1\tbad", "https://exa mple.test/v1"],
)
def test_engine_api_base_rejects_control_and_whitespace(tmp_path, api_base):
    runtime = _runtime(tmp_path / "hermes")
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="api_base"):
        engine_config.configure_engine(
            runtime,
            api_base=api_base,
            model="ocr-model",
            token="S" * 32,
        )


def test_engine_ca_bundle_rejects_final_and_parent_links(tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    outside = tmp_path / "outside-ca.pem"
    outside.write_text("certificate", encoding="utf-8")
    final_link = runtime.config_dir / "linked-ca.pem"
    try:
        final_link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="regular profile config file"):
        engine_config._validate_ca_bundle(runtime, "linked-ca.pem")

    final_link.unlink()
    outside_dir = tmp_path / "outside-ca-dir"
    outside_dir.mkdir()
    (outside_dir / "bundle.pem").write_text("certificate", encoding="utf-8")
    parent_link = runtime.config_dir / "certificates"
    parent_link.symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="regular profile config file"):
        engine_config._validate_ca_bundle(runtime, "certificates/bundle.pem")


def test_broken_engine_transaction_is_incomplete_authority(tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    transaction = runtime.config_dir / "engine-transaction.json"
    try:
        transaction.symlink_to(runtime.config_dir / "missing-transaction.json")
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    assert os.path.lexists(transaction)
    with pytest.raises(profile_runtime.ProfileRuntimeError, match="reparse|link|inspect"):
        engine_config.recover_engine_configuration(runtime)


def test_task_attestation_refuses_foreign_and_missing():
    with pytest.raises(lifecycle.LifecycleError, match="foreign"):
        lifecycle._attest_task_result(
            {"exists": True, "action_matches": False}, allow_absent=True
        )
    with pytest.raises(lifecycle.LifecycleError, match="missing"):
        lifecycle._attest_task_result(
            {"exists": False, "action_matches": False}, allow_absent=False
        )


def test_failed_rollback_start_restores_exact_preoperation_authority(monkeypatch, tmp_path):
    runtime, current, _, _, _ = _installed_pair(tmp_path)
    tasks = FakeTasks(lifecycle._task_spec(runtime, current))
    tasks.fail_start_once = True
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    with pytest.raises(lifecycle.LifecycleError, match="was recovered"):
        manager.rollback(start=True)
    assert lifecycle.read_bounded_json(runtime.config_file)["release_id"] == "0.1.0-new"
    assert lifecycle.read_bounded_json(runtime.deployment_receipt)["release_id"] == "0.1.0-new"
    assert lifecycle.read_bounded_json(runtime.desktop_receipt)["release_id"] == "0.1.0-new"
    assert tasks.current == lifecycle._task_spec(runtime, current)
    assert not runtime.transaction_journal.exists()


def test_successful_rollback_commits_previous_authority_before_task(monkeypatch, tmp_path):
    runtime, current, _, previous, previous_deployment = _installed_pair(tmp_path)
    tasks = FakeTasks(lifecycle._task_spec(runtime, current))
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    result = manager.rollback(start=False)
    assert result["release"] == previous_deployment["release_id"]
    assert lifecycle.read_bounded_json(runtime.config_file) == previous
    assert lifecycle.read_bounded_json(runtime.deployment_receipt)["release_id"] == "0.1.0-old"
    assert lifecycle.read_bounded_json(runtime.desktop_receipt)["release_id"] == "0.1.0-old"
    assert tasks.current == lifecycle._task_spec(runtime, previous)
    assert not runtime.transaction_journal.exists()


def test_failed_uninstall_remove_recovers_receipts_desktop_and_task(monkeypatch, tmp_path):
    runtime, current, _, _, _ = _installed_pair(tmp_path)
    tasks = FakeTasks(lifecycle._task_spec(runtime, current))
    tasks.fail_remove_once = True
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    with pytest.raises(lifecycle.LifecycleError, match="was recovered"):
        manager.uninstall()
    assert lifecycle.read_bounded_json(runtime.deployment_receipt)["release_id"] == "0.1.0-new"
    assert lifecycle.read_bounded_json(runtime.desktop_receipt)["release_id"] == "0.1.0-new"
    assert tasks.current == lifecycle._task_spec(runtime, current)
    assert not runtime.transaction_journal.exists()


def test_successful_uninstall_preserves_documents_and_runtime(monkeypatch, tmp_path):
    runtime, current, _, _, _ = _installed_pair(tmp_path)
    document = runtime.inbox / "keep.pdf"
    document.write_bytes(b"document")
    tasks = FakeTasks(lifecycle._task_spec(runtime, current))
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    result = manager.uninstall()
    assert result["removed"] is True
    assert document.read_bytes() == b"document"
    assert runtime.config_file.is_file() and runtime.token_file.is_file()
    assert not runtime.deployment_receipt.exists()
    assert not runtime.desktop_receipt.exists()
    assert not runtime.desktop_plugin.exists()
    assert tasks.current is None
    assert not runtime.transaction_journal.exists()


def test_interrupted_install_receipt_phase_recovers_forward(monkeypatch, tmp_path):
    runtime, previous_config, _, _, _ = _installed_pair(tmp_path)
    backup = runtime.install_dir / "backups" / "install-forward"
    previous_config_path = lifecycle._backup_file(
        runtime.config_file, backup, "service.json"
    )
    previous_deployment_path = lifecycle._backup_file(
        runtime.deployment_receipt, backup, "deployment.json"
    )
    previous_plugin_path = lifecycle._backup_file(
        runtime.desktop_plugin, backup, "desktop-plugin.js"
    )
    previous_receipt_path = lifecycle._backup_file(
        runtime.desktop_receipt, backup, "desktop-receipt.json"
    )
    new_config = lifecycle.build_service_config(runtime, _release(runtime, "0.1.0-next"))
    lifecycle.atomic_write_json(runtime.config_file, new_config)
    new_desktop = _desktop_receipt(
        runtime,
        "0.1.0-next",
        b"next desktop",
        previous_plugin=previous_plugin_path,
        previous_receipt=previous_receipt_path,
    )
    deployment = _deployment(
        runtime,
        new_config,
        new_desktop["installed_sha256"],
        previous_config=previous_config_path,
        previous_deployment=previous_deployment_path,
    )
    lifecycle.atomic_write_json(runtime.deployment_receipt, deployment)
    transaction = {
        "schema": 1,
        "plugin": "document-reader",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "operation": "install",
        "phase": "receipt_committed",
        "new_release_id": "0.1.0-next",
        "new_config_sha256": lifecycle.sha256_file(runtime.config_file),
        "new_deployment_sha256": lifecycle.sha256_file(runtime.deployment_receipt),
        "previous_config": previous_config_path,
        "previous_config_sha256": lifecycle.sha256_file(Path(previous_config_path)),
        "previous_deployment": previous_deployment_path,
        "previous_deployment_sha256": lifecycle.sha256_file(Path(previous_deployment_path)),
        "previous_desktop_plugin": previous_plugin_path,
        "previous_desktop_plugin_sha256": lifecycle.sha256_file(Path(previous_plugin_path)),
        "previous_desktop_receipt": previous_receipt_path,
        "previous_desktop_receipt_sha256": lifecycle.sha256_file(Path(previous_receipt_path)),
        "new_desktop_plugin_sha256": new_desktop["installed_sha256"],
        "previous_task_exists": True,
        "previous_service_running": False,
        "started_at": "2026-08-10T00:00:00Z",
    }
    lifecycle.atomic_write_json(runtime.transaction_journal, transaction)
    tasks = FakeTasks()
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    recovered = manager.recover(start=False)["service"]
    assert recovered["direction"] == "forward"
    assert recovered["release"] == "0.1.0-next"
    assert tasks.current == lifecycle._task_spec(runtime, new_config)
    assert not runtime.transaction_journal.exists()


def test_interrupted_install_desktop_phase_rolls_back_exact_snapshot(monkeypatch, tmp_path):
    runtime, current, _, _, _ = _installed_pair(tmp_path)
    backup = runtime.install_dir / "backups" / "install-rollback"
    previous_config_path = lifecycle._backup_file(
        runtime.config_file, backup, "service.json"
    )
    previous_deployment_path = lifecycle._backup_file(
        runtime.deployment_receipt, backup, "deployment.json"
    )
    previous_plugin_path = lifecycle._backup_file(
        runtime.desktop_plugin, backup, "desktop-plugin.js"
    )
    previous_receipt_path = lifecycle._backup_file(
        runtime.desktop_receipt, backup, "desktop-receipt.json"
    )
    next_config = lifecycle.build_service_config(runtime, _release(runtime, "0.1.0-crash"))
    _desktop_receipt(
        runtime,
        "0.1.0-crash",
        b"crash desktop",
        previous_plugin=previous_plugin_path,
        previous_receipt=previous_receipt_path,
    )
    transaction = {
        "schema": 1,
        "plugin": "document-reader",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "operation": "install",
        "phase": "desktop_deployed",
        "new_release_id": "0.1.0-crash",
        "new_config_sha256": lifecycle.sha256_json(next_config),
        "new_deployment_sha256": None,
        "previous_config": previous_config_path,
        "previous_config_sha256": lifecycle.sha256_file(Path(previous_config_path)),
        "previous_deployment": previous_deployment_path,
        "previous_deployment_sha256": lifecycle.sha256_file(Path(previous_deployment_path)),
        "previous_desktop_plugin": previous_plugin_path,
        "previous_desktop_plugin_sha256": lifecycle.sha256_file(Path(previous_plugin_path)),
        "previous_desktop_receipt": previous_receipt_path,
        "previous_desktop_receipt_sha256": lifecycle.sha256_file(Path(previous_receipt_path)),
        "new_desktop_plugin_sha256": lifecycle.sha256_bytes(b"crash desktop"),
        "previous_task_exists": True,
        "previous_service_running": False,
        "started_at": "2026-08-10T00:00:00Z",
    }
    lifecycle.atomic_write_json(runtime.transaction_journal, transaction)
    tasks = FakeTasks(lifecycle._task_spec(runtime, current))
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    recovered = manager.recover(start=False)["service"]
    assert recovered["direction"] == "rollback"
    assert lifecycle.read_bounded_json(runtime.config_file)["release_id"] == "0.1.0-new"
    assert lifecycle.read_bounded_json(runtime.desktop_receipt)["release_id"] == "0.1.0-new"
    assert tasks.current == lifecycle._task_spec(runtime, current)
    assert not runtime.transaction_journal.exists()


def test_upgrade_crash_after_desktop_plugin_before_receipt_restores_snapshot(
    monkeypatch, tmp_path
):
    runtime, current, _, _, _ = _installed_pair(tmp_path)
    _write_interrupted_install(
        runtime,
        label="plugin-only",
        previous_task_exists=True,
        previous_service_running=False,
    )
    transaction = lifecycle.read_bounded_json(runtime.transaction_journal)
    lifecycle.atomic_write_bytes(
        runtime.desktop_receipt,
        Path(transaction["previous_desktop_receipt"]).read_bytes(),
        mode=0o600,
    )
    tasks = FakeTasks(lifecycle._task_spec(runtime, current))
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    recovered = manager.recover(start=False)["service"]
    assert recovered["direction"] == "rollback"
    assert lifecycle.read_bounded_json(runtime.desktop_receipt)["release_id"] == "0.1.0-new"
    assert runtime.desktop_plugin.read_bytes() == b"new desktop"


@pytest.mark.parametrize("receipt_written", [False, True])
def test_fresh_install_desktop_partial_states_restore_empty_snapshot(
    monkeypatch, tmp_path, receipt_written
):
    runtime = _runtime(tmp_path / "hermes")
    profile_runtime.write_private_single_line(
        runtime.token_file, "A" * 64, minimum=43, maximum=128
    )
    next_config = lifecycle.build_service_config(
        runtime, _release(runtime, "0.1.0-fresh-crash")
    )
    new_bytes = b"fresh desktop"
    new_hash = lifecycle.sha256_bytes(new_bytes)
    transaction = {
        "schema": 1,
        "plugin": "document-reader",
        "profile": runtime.profile_name,
        "profile_fingerprint": runtime.fingerprint,
        "owner_id": runtime.owner_id,
        "operation": "install",
        "phase": "service_stopped",
        "new_release_id": "0.1.0-fresh-crash",
        "new_config_sha256": lifecycle.sha256_json(next_config),
        "new_deployment_sha256": None,
        "previous_config": None,
        "previous_config_sha256": None,
        "previous_deployment": None,
        "previous_deployment_sha256": None,
        "previous_desktop_plugin": None,
        "previous_desktop_plugin_sha256": None,
        "previous_desktop_receipt": None,
        "previous_desktop_receipt_sha256": None,
        "new_desktop_plugin_sha256": new_hash,
        "previous_task_exists": False,
        "previous_service_running": False,
        "started_at": "2026-08-10T00:00:00Z",
    }
    lifecycle.atomic_write_json(runtime.transaction_journal, transaction)
    lifecycle.atomic_write_bytes(runtime.desktop_plugin, new_bytes, mode=0o644)
    if receipt_written:
        lifecycle.atomic_write_json(
            runtime.desktop_receipt,
            {
                "schema": 1,
                "plugin": "document-reader",
                "version": "0.1.0",
                "profile": runtime.profile_name,
                "profile_fingerprint": runtime.fingerprint,
                "owner_id": runtime.owner_id,
                "release_id": "0.1.0-fresh-crash",
                "installed_sha256": new_hash,
                "source_sha256": new_hash,
                "installed_at": "2026-08-10T00:00:00Z",
                "previous_plugin": None,
                "previous_receipt": None,
            },
        )
    manager = lifecycle.LifecycleManager(
        tmp_path, task_backend=FakeTasks(), runtime=runtime
    )
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    recovered = manager.recover(start=False)["service"]
    assert recovered["direction"] == "rollback"
    assert not runtime.desktop_plugin.exists()
    assert not runtime.desktop_receipt.exists()
    assert not runtime.config_file.exists()
    assert not runtime.deployment_receipt.exists()


@pytest.mark.parametrize(
    ("prior_task", "prior_service", "expected_task", "expected_starts"),
    [
        (False, False, False, 0),
        (True, False, True, 0),
        (True, True, True, 1),
    ],
)
def test_install_recovery_restores_exact_prior_task_and_running_state(
    monkeypatch,
    tmp_path,
    prior_task,
    prior_service,
    expected_task,
    expected_starts,
):
    runtime, current, _, _, _ = _installed_pair(tmp_path)
    _write_interrupted_install(
        runtime,
        label=f"state-{int(prior_task)}-{int(prior_service)}",
        previous_task_exists=prior_task,
        previous_service_running=prior_service,
    )
    tasks = FakeTasks(lifecycle._task_spec(runtime, current) if prior_task else None)
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "loopback_port_open", lambda port: False)
    monkeypatch.setattr(
        lifecycle,
        "_wait_for_health",
        lambda config, token, timeout: {"status": "ok"},
    )
    recovered = manager.recover(start=True)["service"]
    assert recovered["direction"] == "rollback"
    assert (tasks.current is not None) is expected_task
    assert tasks.start_calls == expected_starts
    assert lifecycle.read_bounded_json(runtime.config_file)["release_id"] == "0.1.0-new"


def test_actual_failed_no_start_upgrade_restarts_previously_running_service(
    monkeypatch, tmp_path
):
    runtime, current, _, _, _ = _installed_pair(tmp_path)
    source_desktop = tmp_path / "desktop-plugin" / "document-reader" / "plugin.js"
    source_desktop.parent.mkdir(parents=True)
    source_desktop.write_bytes(b"upgrade desktop")
    next_release = _release(runtime, "0.1.0-upgrade-failure")
    tasks = FakeTasks(lifecycle._task_spec(runtime, current))
    tasks.fail_install_once = True
    manager = lifecycle.LifecycleManager(tmp_path, task_backend=tasks, runtime=runtime)
    monkeypatch.setattr(lifecycle, "recover_engine_configuration", lambda selected: {})
    monkeypatch.setattr(lifecycle, "validate_engine_config", lambda selected: ({}, "E" * 32))
    monkeypatch.setattr(lifecycle, "ensure_profile_token", lambda selected: "A" * 64)
    monkeypatch.setattr(
        lifecycle, "stage_release", lambda selected, source, provision: next_release
    )
    listener_states = iter((True, False))
    monkeypatch.setattr(
        lifecycle, "loopback_port_open", lambda port: next(listener_states)
    )
    monkeypatch.setattr(
        lifecycle,
        "attest_health",
        lambda config, token: {"pid": 42, "started_at": "before"},
    )
    monkeypatch.setattr(lifecycle, "_shutdown_attested", lambda config, token: None)
    monkeypatch.setattr(
        lifecycle,
        "_wait_for_health",
        lambda config, token, timeout: {"status": "ok"},
    )
    with pytest.raises(lifecycle.LifecycleError, match="was rolled back"):
        manager.install(provision=True, start=False)
    assert lifecycle.read_bounded_json(runtime.config_file)["release_id"] == "0.1.0-new"
    assert tasks.current == lifecycle._task_spec(runtime, current)
    assert tasks.start_calls == 1
    assert not runtime.transaction_journal.exists()


def test_status_is_serialized_by_profile_lock(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    observed = []

    @contextmanager
    def recording_lock(selected, **kwargs):
        observed.append(selected.fingerprint)
        yield

    monkeypatch.setattr(lifecycle, "profile_install_lock", recording_lock)
    result = lifecycle.LifecycleManager(
        tmp_path, runtime=runtime, task_backend=FakeTasks()
    ).status()
    assert result["installed"] is False
    assert observed == [runtime.fingerprint]


@pytest.mark.parametrize(("task_present", "listener_open"), [(True, False), (False, True)])
def test_missing_config_reports_and_refuses_orphan_task_or_listener(
    monkeypatch, tmp_path, task_present, listener_open
):
    runtime = _runtime(tmp_path / "hermes")
    tasks = FakeTasks(object() if task_present else None)
    manager = lifecycle.LifecycleManager(
        tmp_path, runtime=runtime, task_backend=tasks
    )
    monkeypatch.setattr(
        lifecycle, "loopback_port_open", lambda port: listener_open
    )
    status = manager.status()
    assert status["installed"] is False
    assert status["recovery_required"] is True
    assert status["task"]["exists"] is task_present
    assert status["listener_open"] is listener_open
    with pytest.raises(lifecycle.LifecycleError, match="missing"):
        manager.uninstall()


def test_legacy_adoption_rejects_nesting_collision_links_and_broad_files(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    with pytest.raises(lifecycle.LifecycleError, match="narrow tree"):
        lifecycle._legacy_files(runtime.plugin_root, runtime)

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "document.pdf").write_bytes(b"pdf")
    try:
        (legacy / "linked.pdf").symlink_to(legacy / "document.pdf")
    except OSError:
        pass
    else:
        with pytest.raises(lifecycle.LifecycleError, match="link/reparse"):
            lifecycle._legacy_files(legacy, runtime)
        (legacy / "linked.pdf").unlink()

    plan = lifecycle.stage_legacy_documents(
        legacy, runtime, runtime.install_dir / "legacy-stage"
    )
    destination = Path(plan[0]["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"foreign")
    with pytest.raises(lifecycle.LifecycleError, match="collision"):
        lifecycle.publish_legacy_documents(plan, runtime)

    monkeypatch.setattr(lifecycle, "MAX_LEGACY_FILES", 0)
    with pytest.raises(lifecycle.LifecycleError, match="bounded"):
        lifecycle._legacy_files(legacy, runtime)


def test_legacy_inbox_size_matches_service_upload_boundary(tmp_path):
    runtime = _runtime(tmp_path / "hermes")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    boundary = legacy / "boundary.pdf"
    with boundary.open("wb") as handle:
        handle.truncate(lifecycle.MAX_LEGACY_INPUT_BYTES)
    assert lifecycle._legacy_files(legacy, runtime) == [boundary]

    with boundary.open("r+b") as handle:
        handle.truncate(lifecycle.MAX_LEGACY_INPUT_BYTES + 1)
    with pytest.raises(lifecycle.LifecycleError, match="size is unsafe"):
        lifecycle._legacy_files(legacy, runtime)

    processed = legacy / "processed"
    processed.mkdir()
    boundary.unlink()
    retained_output = processed / "large-output.xlsx"
    with retained_output.open("wb") as handle:
        handle.truncate(lifecycle.MAX_LEGACY_INPUT_BYTES + 1)
    assert lifecycle._legacy_files(legacy, runtime) == [retained_output]
