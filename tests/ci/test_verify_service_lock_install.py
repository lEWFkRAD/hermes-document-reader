import json
from pathlib import Path

import pytest

from scripts.verify_service_lock_install import (
    VerificationError,
    parse_lock,
    verify_report,
)


ROOT = Path(__file__).resolve().parents[2]
LOCKS = {
    "3.11": ("windows-cpython-311-x86_64", ROOT / "install/locks/windows-cpython-311-x86_64.txt"),
    "3.14": ("windows-cpython-314-x86_64", ROOT / "install/locks/windows-cpython-314-x86_64.txt"),
}


@pytest.mark.parametrize(("minor", "contract"), LOCKS.items())
def test_service_lock_parser_is_lane_bound_and_crlf_tolerant(tmp_path, minor, contract):
    target, source = contract
    packages = parse_lock(source, target)
    assert len(packages) == 32
    assert packages["pip"].version == "26.1.2"
    assert packages["setuptools"].version == "83.0.0"

    crlf = tmp_path / source.name
    crlf.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    assert parse_lock(crlf, target) == packages
    wrong_target = LOCKS["3.14" if minor == "3.11" else "3.11"][0]
    with pytest.raises(VerificationError, match="wrong target"):
        parse_lock(source, wrong_target)


def _report(packages):
    installs = []
    for name, package in packages.items():
        digest = sorted(package.hashes)[0]
        wheel_name = f"{name.replace('-', '_')}-{package.version}-py3-none-any.whl"
        installs.append(
            {
                "download_info": {
                    "url": f"https://files.example.invalid/{wheel_name}",
                    "archive_info": {"hashes": {"sha256": digest}},
                },
                "metadata": {"name": name, "version": package.version},
            }
        )
    return {
        "version": "1",
        "pip_version": "26.1.2",
        "environment": {
            "implementation_name": "cpython",
            "python_version": "3.11",
            "sys_platform": "win32",
            "platform_system": "Windows",
            "platform_machine": "AMD64",
        },
        "install": installs,
    }


def test_pip_report_binds_every_selected_wheel_and_hash(tmp_path):
    target, source = LOCKS["3.11"]
    packages = parse_lock(source, target)
    report = _report(packages)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    runtime = {"python": "3.11"}

    selected = verify_report(report_path, packages, runtime)
    assert [item["name"] for item in selected] == sorted(packages)

    report["install"][0]["download_info"]["archive_info"]["hashes"]["sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(VerificationError, match="unhashed artifact"):
        verify_report(report_path, packages, runtime)


def test_pip_report_rejects_sdists_and_incomplete_inventory(tmp_path):
    target, source = LOCKS["3.11"]
    packages = parse_lock(source, target)
    report = _report(packages)
    report["install"][0]["download_info"]["url"] = "https://files.example.invalid/source.tar.gz"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    runtime = {"python": "3.11"}
    with pytest.raises(VerificationError, match="did not install from a wheel"):
        verify_report(report_path, packages, runtime)

    report = _report(packages)
    report["install"].pop()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(VerificationError, match="differs from lock"):
        verify_report(report_path, packages, runtime)
