"""Offline security regression tests for the coordinator build-tool lock and publication provenance."""

import copy
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import build_tool_lock, gap_wheels

FILE_URL = "https://files.pythonhosted.org/packages/x/"
TARGET = "3.14 win-arm64"


def project_zip(requirements='["setuptools>=70"]'):
    data = f'[build-system]\nrequires = {requirements}\nbuild-backend = "setuptools.build_meta"\n'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("demo-1.0/pyproject.toml", data)
    return output.getvalue()


def project_lock(name="demo", target=TARGET):
    wheel_name = "setuptools-80.0.0-py3-none-any.whl"
    return {"schema": 1, "name": name, "version": "1.0", "target": target,
            "sdist": f"{name}-1.0.zip", "sdist_url": FILE_URL + f"{name}-1.0.zip",
            "sdist_sha256": "a" * 64,
            "wheels": [{"name": "setuptools", "version": "80.0.0", "filename": wheel_name,
                        "url": FILE_URL + wheel_name, "sha256": "e" * 64}]}


def provenance(lock):
    return {"lock_sha256": build_tool_lock.digest(lock), "packages": gap_wheels.expected_packages(lock),
            "python": "3.14.10", "python_build": "3.14.10 test", "runner_image": "20261118.1",
            "compiler": {"cc": "MSVC compiler 19.0", "link": "MSVC linker 14.0"}}


class ReadSdist(unittest.TestCase):
    def test_read_static_build_system_without_executing_code(self):
        self.assertEqual(build_tool_lock._project_requirements(project_zip(), "demo-1.0.zip"),
                         ["setuptools>=70"])
        stream = io.BytesIO()
        pyproject = b'[build-system]\nrequires=["wheel"]\nbuild-backend="setuptools.build_meta"\n'
        with tarfile.open(mode="w:gz", fileobj=stream) as archive:
            member = tarfile.TarInfo("demo-1.0/pyproject.toml")
            member.size = len(pyproject)
            archive.addfile(member, io.BytesIO(pyproject))
        self.assertEqual(build_tool_lock._project_requirements(stream.getvalue(), "demo-1.0.tar.gz"), ["wheel"])

    def test_refuse_ambiguous_and_unsafe_metadata(self):
        for requirements in ('["--extra-index-url https://evil.example/simple"]',
                             '["somepackage @ https://evil.example/file.whl"]',
                             '["-r ../../other-requirements.txt"]'):
            with self.subTest(requirements=requirements), self.assertRaises(build_tool_lock.LockError):
                build_tool_lock._project_requirements(project_zip(requirements), "demo-1.0.zip")
        with self.assertRaises(build_tool_lock.LockError):
            build_tool_lock._project_requirements(b"not a zip", "demo-1.0.zip")
        with self.assertRaises(build_tool_lock.LockError):
            build_tool_lock._project_requirements(b"", "demo-1.0.tar.gz")


class Coordinator(unittest.TestCase):
    def test_resolve_uses_wheels_only_and_checks_pypi_hash(self):
        source = project_zip()
        wheel = b"fixed wheel bytes"
        filename = "setuptools-80.0.0-py3-none-any.whl"
        sdist_item = {"filename": "demo-1.0.zip", "packagetype": "sdist", "url": FILE_URL + "demo-1.0.zip",
                      "digests": {"sha256": hashlib.sha256(source).hexdigest()}}
        wheel_item = {"filename": filename, "url": FILE_URL + filename,
                      "digests": {"sha256": hashlib.sha256(wheel).hexdigest()}}
        seen = []

        def metadata(name, version):
            self.assertIn((name, version), (("demo", "1.0"), ("setuptools", "80.0.0")))
            return {"urls": [sdist_item] if name == "demo" else [wheel_item]}

        def command(args, **kw):
            seen.append(args)
            (Path(args[-1]) / filename).parent.mkdir(parents=True, exist_ok=True)
            (Path(args[-1]) / filename).write_bytes(wheel)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(build_tool_lock, "_metadata", side_effect=metadata), \
             mock.patch.object(build_tool_lock, "_from_pypi", return_value=source), \
             mock.patch.object(build_tool_lock.subprocess, "run", side_effect=command):
            lock = build_tool_lock.resolve({"name": "demo", "version": "1.0", "target": TARGET})
        self.assertTrue(build_tool_lock.validate(lock))
        self.assertEqual(lock["sdist_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(lock["wheels"][0]["sha256"], hashlib.sha256(wheel).hexdigest())
        self.assertIn("--only-binary=:all:", seen[0])
        self.assertIn("--platform", seen[0])
        self.assertIn("win_arm64", seen[0])
        self.assertNotIn("--no-binary", seen[0])

    def test_resolve_rejects_sdist_hash_before_dependency_resolution(self):
        item = {"filename": "demo-1.0.zip", "packagetype": "sdist",
                "url": FILE_URL + "demo-1.0.zip", "digests": {"sha256": "a" * 64}}
        with mock.patch.object(build_tool_lock, "_metadata", return_value={"urls": [item]}), \
             mock.patch.object(build_tool_lock, "_from_pypi", return_value=project_zip()), \
             mock.patch.object(build_tool_lock.subprocess, "run") as build:
            with self.assertRaisesRegex(build_tool_lock.LockError, "SHA-256 mismatch"):
                build_tool_lock.resolve({"name": "demo", "version": "1.0", "target": TARGET})
            build.assert_not_called()

    def test_lock_plan_dispatches_only_resolvable_pairs(self):
        jobs = [{"name": name, "version": "1.0", "target": TARGET, "id": id, "attempt": attempt}
                for id, name in (("0", "demo"), ("1", "other")) for attempt in (1, 2)]

        def resolver(job):
            if job["name"] == "other":
                raise build_tool_lock.LockError("unsupported backend")
            return project_lock()

        with mock.patch.object(build_tool_lock, "resolve", side_effect=resolver):
            selected, bundle = build_tool_lock.lock_plan(jobs)
        self.assertEqual(len(selected), 2)
        self.assertEqual(set(bundle["pairs"]), {"0"})
        self.assertEqual(selected[0]["lock_sha256"], build_tool_lock.digest(bundle["pairs"]["0"]))

    def test_missing_or_tampered_lock_is_refused(self):
        job = {"name": "demo", "version": "1.0", "target": TARGET}
        lock = project_lock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "locks.json"
            path.write_text(json.dumps({"schema": 1, "pairs": {"0": lock}}))
            got = build_tool_lock.load_lock(path, "0", build_tool_lock.digest(lock), job)
            self.assertEqual(got, lock)
            with self.assertRaises(build_tool_lock.LockError):
                build_tool_lock.load_lock(path, "0", "f" * 64, job)
            with self.assertRaises(build_tool_lock.LockError):
                build_tool_lock.load_lock(path, "0", build_tool_lock.digest(lock),
                                          {**job, "name": "other"})
            with self.assertRaises(build_tool_lock.LockError):
                build_tool_lock.load_lock(path, "1", build_tool_lock.digest(lock), job)

    def test_download_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(build_tool_lock, "_from_pypi", return_value=b"altered"):
            path = Path(folder) / "tool.whl"
            with self.assertRaisesRegex(build_tool_lock.LockError, "SHA-256 mismatch"):
                build_tool_lock.download_locked({"url": FILE_URL + "tool.whl", "sha256": "a" * 64}, path)
            self.assertFalse(path.exists())

    def test_build_environment_installs_only_hash_pinned_wheels(self):
        lock = project_lock()
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            calls = []

            def run(cmd, **kw):
                calls.append(cmd)
                if "list" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, '[{"name":"pip","version":"26.3"},'
                                                       '{"name":"setuptools","version":"80.0.0"}]', "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch("venv.EnvBuilder"), \
                 mock.patch.object(build_tool_lock, "download_locked", side_effect=lambda item, p: p.write_bytes(b"ok")), \
                 mock.patch.object(build_tool_lock.subprocess, "run", side_effect=run):
                py, packages = build_tool_lock.create_build_environment(lock, work)
            self.assertEqual(packages, lock["wheels"])
            self.assertTrue(str(py).endswith("/bin/python") or str(py).endswith("\\Scripts\\python.exe"))
            self.assertIn("--require-hashes", calls[0])
            self.assertIn("--no-deps", calls[0])
            self.assertIn("--no-index", calls[0])
            text = (work / "build-requirements.txt").read_text()
            self.assertIn("setuptools==80.0.0 --hash=sha256:", text)


class TrustedPublication(unittest.TestCase):
    def setUp(self):
        self.lock = project_lock()
        self.jobs = [{"id": "0", "name": "demo", "version": "1.0", "target": TARGET,
                      "attempt": attempt, "runner": "windows-11-arm",
                      "lock_sha256": build_tool_lock.digest(self.lock)} for attempt in (1, 2)]
        self.locks = {"schema": 1, "pairs": {"0": self.lock}}

    def result(self, attempt=1):
        return {"schema": gap_wheels.SCHEMA, "name": "demo", "version": "1.0", "target": TARGET,
                "attempt": attempt, "runner": "windows-11-arm",
                "sdist": "demo-1.0.zip", "sdist_sha256": "a" * 64,
                "wheel": "demo-1.0-cp314-cp314-win_arm64.whl", "sha256": "b" * 64,
                "installs": True if attempt == 1 else None, "tools": provenance(self.lock)}

    def verify(self, first, second, locks=None):
        results = {("0", 1): first, ("0", 2): second}
        return gap_wheels.verify(self.jobs, results,
                                lambda name, version: {"demo-1.0.zip": "a" * 64},
                                locks=self.locks if locks is None else locks)

    def test_provenance_is_kept_in_verified_manifest(self):
        verified, rows = self.verify(self.result(), self.result(2))
        self.assertEqual(rows[0]["verdict"], "verified")
        self.assertEqual(verified[0]["tools"], provenance(self.lock))

    def test_byte_identical_but_different_compiler_is_rejected(self):
        first, second = self.result(), self.result(2)
        second["tools"]["compiler"]["link"] = "MSVC linker 14.9"
        verified, rows = self.verify(first, second)
        self.assertFalse(verified)
        self.assertIn("different toolchains", rows[0]["verdict"])

    def test_forged_lock_or_packages_cannot_be_verified(self):
        first, second = self.result(), self.result(2)
        second["tools"]["packages"][0]["sha256"] = "b" * 64
        verified, rows = self.verify(first, second)
        self.assertFalse(verified)
        self.assertIn("coordinator's build-tool lock", rows[0]["verdict"])
        first, second = self.result(), self.result(2)
        first["tools"]["lock_sha256"] = "c" * 64
        verified, rows = self.verify(first, second)
        self.assertFalse(verified)
        self.assertIn("rejected", rows[0]["verdict"])

    def test_publication_refuses_missing_or_malformed_provenance(self):
        self.assertFalse(gap_wheels.tool_provenance_valid(None))
        self.assertFalse(gap_wheels.tool_provenance_valid({"lock_sha256": "b" * 64}))
        self.assertTrue(gap_wheels.tool_provenance_valid(provenance(self.lock)))
        with self.assertRaises(SystemExit):
            gap_wheels.trusted_staged(json.dumps([{
                "name": "demo", "version": "1.0", "target": TARGET,
                "wheel": "demo-1.0-cp314-cp314-win_arm64.whl", "sha256": "b" * 64,
                "sdist": "demo-1.0.zip", "sdist_sha256": "a" * 64, "builds": [],
                "licence": "MIT", "requires_python": None, "verified": "2026-11-18", "batch": "r"
            }]))

    def test_unpinned_build_never_launches_package_code(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(gap_wheels.subprocess, "run") as runner:
            result = gap_wheels.run_build("demo", "1.0", 1, Path(folder))
            runner.assert_not_called()
            self.assertIsNone(result["wheel"])
            self.assertIsNone(result["tools"])


if __name__ == "__main__":
    unittest.main()
