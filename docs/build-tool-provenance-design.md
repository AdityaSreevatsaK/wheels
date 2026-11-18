# Build-tool provenance proposal (issue #4)

**Status:** Design draft only. The existing build-and-publish pipeline is unchanged. Do not describe a pair as
supply-chain-pinned until the implementation and tests described here are complete.

## Current boundary

`scripts/gap_wheels.py:plan` emits two jobs per (name, version, target). The trusted coordinator dispatches
`.github/workflows/gap-wheel-build.yml` twice. In each untrusted build, `run_build` downloads the PyPI sdist and
runs `pip wheel` with build isolation. PEP 517's build backend and its dependencies resolve separately on both jobs,
without a frozen list of wheel bytes. `verify` compares the two output hashes and sdist hashes, but not the build
backend or toolchain. `cmd_stage` and `cmd_publish` publish that verdict.

Current pins for Linux images, Rust, `auditwheel` and `patchelf` are valuable, but do not pin PEP 517 build tools.
Windows also floats to the current GitHub-hosted runner image and CPython patch release.

## Proposed fail-closed contract

1. **One lock for each pair, before dispatch.** Add a trusted `lock-build-tools` step after planning. It downloads
   the exact PyPI sdist as data, checks its PyPI SHA-256, and parses `pyproject.toml` without importing or executing
   code from the sdist. Resolve `[build-system].requires` for the target's interpreter and platform from wheels
   only; recursively freeze all installed build-time packages, including transitive requirements. Use one
   version/filename/SHA-256 record per selected wheel. Backend hook requirements that cannot be established
   without executing sdist code are *not guessed*: the pair is skipped until a safe locking mechanism exists.
2. **Immutable transfer.** Store the pair's lock as a coordinator artifact with a content SHA-256. The dispatcher
   passes only the coordinator run ID, pair ID and digest to both build runs. Each build fetches that one artifact
   before executing the sdist, checks the digest, and reads only names, versions and hashed files from a bounded
   schema. Reject missing or oversized locks, unknown package names, unexpected URLs and duplicate filenames.
   The build's GitHub token remains read-only; never expose a coordinator write token to build steps.
3. **Identical inputs, isolated installation.** Download the exact locked wheels, verify every SHA-256, and install
   them into a fresh build venv without dependency resolution. Build with `pip wheel --no-build-isolation --no-deps`
   using that venv's Python. If a PEP 517 hook asks for an undeclared requirement, fail the build, do not fall back
   to ambient PyPI resolution. Verify the downloaded sdist hash again before unpacking or running its code.
4. **Record the actual environment.** Each build's `result.json` names the lock digest and each wheel's name,
   version and SHA-256, plus the exact `sys.version`, runner image identifier and platform-specific compiler
   versions. On Windows, locate the MSVC compiler selected for the build and record both `cl.exe` and `link.exe`
   versions, not just environment-variable guesses; retain the fact that GitHub-hosted Windows images themselves
   cannot be pinned. Linux records its image digest and Rust version too.
5. **Trusted comparison.** Extend `RESULT_KEYS`, `problems` and `verify` together. Both results must reference
   the coordinator's exact lock and use identical build dependencies and relevant toolchain versions. Incomplete,
   malformed, missing or inconsistent provenance means **rejected**, even if the two wheels have identical bytes.
   Propagate validated provenance through manifest, staging and publication to `data/gap-wheels.json`. Keep legacy
   index entries readable but do not permit new unpinned entries to be published. Bound the new JSON sizes.
6. **Threat model.** Document that a matching pair under the same locked tools proves repeatability under those
   inputs, not sdist trustworthiness, independent source reproduction or absence of malicious upstream code.

## Required isolated tests (no real publish)

- Lock resolution with mocked PyPI responses and a small trusted wheel set; exact version/hash matching.
- Reject missing `pyproject.toml`, dynamic/unsupported backends, unsafe tar members and wheel-only resolution
  failures without evaluating sdist code. Ensure no builder runs when the lock is absent.
- Reject hash mismatch before package code executes; refuse ambient package resolution and unexpected extra tools.
- Feed two otherwise identical fake wheels to `verify`; differing tool lock, Python patch version, MSVC version
  or runner image must fail. Both matching locks and toolchains should verify.
- Verify malformed, oversized and forged provenance is rejected in result, staging and publish paths.
- Windows and Linux smoke runs that demonstrate the selected backend and wheel versions in both output records.
- Confirm a workflow re-run with an unchanged coordinator lock never silently obtains newer build-backend files.

## Roll-out

Land as a single coordinated pipeline change (or a staged series with explicit **no-publish** gating until complete).
Never merge an intermediate change that merely relaxes or bypasses the current verifications. Require maintainer
review of the lock artifact access and the supply-chain trust boundary before the first real publish.

This proposal is intentionally conservative: some legitimate packages will be skipped if their dynamic PEP 517
requirements cannot be pinned safely. That is preferable to publishing a wheel whose toolchain is unknown.
