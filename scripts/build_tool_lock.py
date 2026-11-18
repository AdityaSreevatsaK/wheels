"""Resolve and verify build-tool wheels before either untrusted build starts.

Only the coordinator calls resolve(). It reads a verified PyPI sdist as *data*,
does not execute its backend, and resolves wheels-only dependencies. Dynamic
requirements unsupported by this strict resolver cause the pair to be skipped.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

try:
    from . import build_lab
except ImportError:  # executed directly by scripts/gap_wheels.py
    import build_lab

PYPI = "https://pypi.org/pypi/"
FILES = "https://files.pythonhosted.org/packages/"
MAX_DOWNLOAD = 48_000_000
MAX_PROJECT = 131_072
PINNED_PIP = "pip==26.3"  # the builder itself must also be a hash-verified wheel
SHA = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
WHEEL = re.compile(r"[A-Za-z0-9._+!-]{1,200}\.whl\Z")
SDIST = re.compile(r"[A-Za-z0-9._-]{1,120}\.(?:tar\.gz|zip)\Z")


class LockError(ValueError):
    pass


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _from_pypi(url: str) -> bytes:
    if not (url.startswith(FILES) or url.startswith(PYPI)):
        raise LockError("a download is not a PyPI-hosted artifact")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wheelgap-build-lock/1"}),
                                timeout=40) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise LockError("a PyPI artifact exceeds the download limit")
    return data


def _metadata(name: str, version: str) -> dict:
    if not (NAME.fullmatch(canonical(name)) and re.fullmatch(r"[A-Za-z0-9._+!-]{1,100}", version)):
        raise LockError("invalid package name or version")
    try:
        data = json.loads(_from_pypi(f"{PYPI}{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json"))
        if not isinstance(data, dict) or not isinstance(data.get("urls"), list):
            raise LockError("PyPI metadata is not an object with files")
        return data
    except (OSError, ValueError, TimeoutError) as exc:
        raise LockError(f"cannot read PyPI release metadata: {exc}") from exc


def _project_requirements(archive: bytes, filename: str) -> list[str]:
    """Read only the one top-level pyproject.toml; never extract or execute archive members."""
    candidates = []
    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                for member in z.infolist():
                    if len(Path(member.filename).parts) == 2 and member.filename.endswith("/pyproject.toml"):
                        if member.file_size > MAX_PROJECT:
                            raise LockError("pyproject.toml is too large")
                        candidates.append(z.read(member, pwd=None))
        else:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar:
                    if len(Path(member.name).parts) == 2 and member.name.endswith("/pyproject.toml"):
                        if not member.isfile() or member.size > MAX_PROJECT:
                            raise LockError("unsafe pyproject.toml member")
                        file = tar.extractfile(member)
                        if file is None:
                            raise LockError("cannot read project metadata")
                        candidates.append(file.read(MAX_PROJECT + 1))
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
        raise LockError(f"cannot read sdist: {exc}") from exc
    if len(candidates) != 1:
        raise LockError("the sdist must have exactly one top-level pyproject.toml")
    try:
        meta = tomllib.loads(candidates[0].decode("utf-8"))
        system = meta["build-system"]
        reqs = system["requires"]
        if not isinstance(reqs, list) or not reqs or not all(isinstance(r, str) and r.strip() for r in reqs):
            raise LockError("build-system.requires must be a nonempty string list")
        if not isinstance(system.get("build-backend"), str) or not system["build-backend"]:
            raise LockError("no static PEP 517 build backend")
        # These go into a pip requirements file: never allow an sdist to add index options,
        # includes, URLs or lines that change the resolver's trust boundary.
        if any(
            "\n" in req or "\r" in req or req.lstrip().startswith(("-", "#"))
            or any(part in req for part in ("@", "://", "\\", "--", "/"))
            or re.fullmatch(r"\s*[A-Za-z0-9][A-Za-z0-9._-]{0,120}.*", req) is None
            for req in reqs
        ):
            raise LockError("unsupported or unsafe PEP 508 build requirement")
        return reqs
    except (UnicodeError, KeyError, tomllib.TOMLDecodeError, TypeError) as exc:
        raise LockError(f"unsupported pyproject.toml build requirements: {exc}") from exc


def _platforms(target: str) -> list[str]:
    if "win-arm64" in target:
        return ["win_arm64"]
    if "win-amd64" in target:
        return ["win_amd64"]
    if "musl-aarch64" in target:
        return [f"musllinux_1_{n}_aarch64" for n in (2, 1)]
    if "linux-x86_64" in target:
        return [f"manylinux_2_{n}_x86_64" for n in range(28, 4, -1)] + [
            "manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"
        ]
    raise LockError("unknown target platform")


def _archive_info(item: dict, filename: str) -> dict:
    if not isinstance(item, dict) or item.get("filename") != filename:
        raise LockError("PyPI file metadata mismatch")
    url, dig = item.get("url"), item.get("digests")
    if not isinstance(url, str) or not url.startswith(FILES):
        raise LockError("non-PyPI wheel location")
    if not isinstance(dig, dict) or not isinstance(dig.get("sha256"), str) or not SHA.fullmatch(dig["sha256"]):
        raise LockError("wheel has no valid PyPI SHA-256")
    if urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] != filename:
        raise LockError("wheel URL and filename differ")
    return {"url": url, "sha256": dig["sha256"]}


def validate(lock: object) -> bool:
    if not isinstance(lock, dict) or set(lock) != {
        "schema", "name", "version", "target", "sdist", "sdist_url", "sdist_sha256", "wheels"
    } or lock["schema"] != 1:
        return False
    if not (isinstance(lock["name"], str) and NAME.fullmatch(lock["name"])
            and isinstance(lock["version"], str) and build_lab.VERSION.fullmatch(lock["version"])
            and lock["target"] in build_lab.TARGETS and isinstance(lock["sdist"], str)
            and SDIST.fullmatch(lock["sdist"]) and isinstance(lock["sdist_url"], str)
            and lock["sdist_url"].startswith(FILES) and isinstance(lock["sdist_sha256"], str)
            and SHA.fullmatch(lock["sdist_sha256"])):
        return False
    if not isinstance(lock["wheels"], list) or not 0 < len(lock["wheels"]) <= 100:
        return False
    names = set()
    for item in lock["wheels"]:
        if not isinstance(item, dict) or set(item) != {"name", "version", "filename", "url", "sha256"}:
            return False
        name, version, filename, url, sha = (item[k] for k in ("name", "version", "filename", "url", "sha256"))
        if not (isinstance(name, str) and NAME.fullmatch(name) and name not in names
                and isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9._+!-]{1,100}", version)
                and isinstance(filename, str) and WHEEL.fullmatch(filename)
                and isinstance(url, str) and url.startswith(FILES)
                and urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] == filename
                and isinstance(sha, str) and SHA.fullmatch(sha)):
            return False
        names.add(name)
    return True


def resolve(pair: dict) -> dict:
    """Resolve one pair from PyPI without running the sdist. Fail closed on unsupported requirements."""
    name, version, target = pair["name"], pair["version"], pair["target"]
    release = _metadata(name, version)
    sdists = [x for x in release["urls"] if isinstance(x, dict)
              and x.get("packagetype") == "sdist" and isinstance(x.get("filename"), str)
              and SDIST.fullmatch(x["filename"])]
    if len(sdists) != 1:
        raise LockError("expected exactly one PyPI sdist")
    meta = _archive_info(sdists[0], sdists[0]["filename"])
    archive = _from_pypi(meta["url"])
    if hashlib.sha256(archive).hexdigest() != meta["sha256"]:
        raise LockError("PyPI sdist SHA-256 mismatch")
    requirements = _project_requirements(archive, sdists[0]["filename"])
    python_version = target.split()[0].removesuffix("t")
    digits = python_version.replace(".", "")
    abi = f"cp{digits}{'t' if target.split()[0].endswith('t') else ''}"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "requires.txt").write_text("\n".join([*requirements, PINNED_PIP]) + "\n", encoding="utf-8")
        cmd = [sys.executable, "-m", "pip", "--isolated", "download", "--index-url", "https://pypi.org/simple/",
               "--only-binary=:all:", "--no-cache-dir", "--disable-pip-version-check",
               "--python-version", python_version, "--implementation", "cp", "--abi", abi]
        for platform in _platforms(target):
            cmd += ["--platform", platform]
        cmd += ["-r", str(root / "requires.txt"), "-d", str(root / "wheels")]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=False)
        if run.returncode:
            raise LockError("wheel-only build dependency resolution failed: " + (run.stderr or run.stdout)[-450:])
        wheels = sorted((root / "wheels").glob("*.whl"))
        if not wheels:
            raise LockError("build dependency resolver produced no wheels")
        locked = []
        for file in wheels:
            parts = file.name.split("-")
            if len(parts) not in (5, 6):
                raise LockError("invalid resolved wheel filename")
            project = canonical(parts[0])
            ver = parts[1]
            data = _metadata(project, ver)
            choices = [x for x in data["urls"] if isinstance(x, dict) and x.get("filename") == file.name]
            if len(choices) != 1:
                raise LockError("resolved wheel is not in PyPI's release listing")
            wheel_meta = _archive_info(choices[0], file.name)
            if hashlib.sha256(file.read_bytes()).hexdigest() != wheel_meta["sha256"]:
                raise LockError("downloaded wheel SHA-256 mismatch")
            locked.append({"name": project, "version": ver, "filename": file.name, **wheel_meta})
    if "pip" not in {w["name"] for w in locked}:
        raise LockError("the pinned build pip was not resolved")
    result = {"schema": 1, "name": canonical(name), "version": version, "target": target,
              "sdist": sdists[0]["filename"], "sdist_url": meta["url"],
              "sdist_sha256": meta["sha256"], "wheels": sorted(locked, key=lambda x: x["name"])}
    if not validate(result):
        raise LockError("resolved tool lock is malformed")
    return result


def lock_plan(jobs: list[dict]) -> tuple[list[dict], dict]:
    """Keep only pairs whose build tools were locked. The coordinator never dispatches skipped pairs."""
    pairs = {str(j["id"]): j for j in jobs}
    locks = {}
    for pid, job in pairs.items():
        try:
            lock = resolve(job)
            locks[pid] = lock
            print(f"locked {job['name']} {job['version']} {job['target']}: {len(lock['wheels'])} tool wheels",
                  file=sys.stderr)
        except (LockError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"skipping unpinned {job['name']} {job['version']} {job['target']}: {exc}",
                  file=sys.stderr)
    filtered = [{**j, "lock_sha256": digest(locks[str(j["id"])])}
                for j in jobs if str(j["id"]) in locks]
    return filtered, {"schema": 1, "pairs": locks}


def load_lock(bundle: Path, pair_id: str, expected: str, job: dict) -> dict:
    """The build may use only the exact coordinator lock its job was planned with."""
    if not SHA.fullmatch(expected) or not pair_id.isdigit() or not bundle.is_file() or bundle.stat().st_size > 4_000_000:
        raise LockError("missing or invalid coordinator tool-lock bundle")
    data = json.loads(bundle.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema", "pairs"} or data["schema"] != 1:
        raise LockError("invalid lock bundle")
    lock = data["pairs"].get(pair_id)
    if not validate(lock) or digest(lock) != expected:
        raise LockError("tool-lock digest mismatch or malformed lock")
    if (lock["name"], lock["version"], lock["target"]) != (
        canonical(job["name"]), job["version"], job["target"]
    ):
        raise LockError("tool lock was created for a different job")
    return lock


def download_locked(item: dict, path: Path) -> None:
    data = _from_pypi(item["url"])
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise LockError(f"SHA-256 mismatch: {path.name}")
    path.write_bytes(data)


def create_build_environment(lock: dict, work: Path) -> tuple[Path, list[dict]]:
    """Install only coordinator-chosen, hash-verified wheels into a clean PEP 517 build environment."""
    import venv

    root = work / "backend-venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(root)
    py = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    wheel_dir = work / "locked-wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    for item in lock["wheels"]:
        download_locked(item, wheel_dir / item["filename"])
    reqs = work / "build-requirements.txt"
    reqs.write_text("".join(f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}\n"
                            for w in lock["wheels"]), encoding="utf-8")
    cmd = [str(py), "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
           "--require-hashes", "--only-binary=:all:", "--find-links", str(wheel_dir), "-r", str(reqs)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        raise LockError("hash-pinned build dependency install failed: " + (result.stderr or result.stdout)[-500:])
    installed = subprocess.run([str(py), "-m", "pip", "list", "--format=json"], capture_output=True,
                               text=True, timeout=40, check=True)
    got = {canonical(x["name"]): x["version"].lower() for x in json.loads(installed.stdout)}
    if not any(w["name"] == "pip" and w["version"] == PINNED_PIP.split("==")[1] for w in lock["wheels"]):
        raise LockError("build pip must be present at the pinned version")
    if any(got.get(w["name"]) != w["version"].lower() for w in lock["wheels"]):
        raise LockError("installed build requirements differ from the coordinator lock")
    if any(n not in {w["name"] for w in lock["wheels"]} | {"setuptools"} for n in got):
        raise LockError("unexpected build environment package")
    return py, lock["wheels"]
