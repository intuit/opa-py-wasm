# Releasing

How a new version of `opa-py-wasm` gets to [PyPI](https://pypi.org/project/opa-py-wasm/).

Releases are cut by maintainers. Everything except the version bump and the tag
is automated by [`.github/workflows/release.yml`](.github/workflows/release.yml).

> **PyPI uploads are immutable.** A published version can be *yanked* but never
> replaced or re-uploaded. That is why the release workflow re-runs the full
> test matrix and refuses to publish when the tag and `pyproject.toml` disagree.

## The release loop

1. **Land your changes** on `master` with CI green. Every user-visible change
   should have added a bullet under `## [Unreleased]` in
   [`CHANGELOG.md`](CHANGELOG.md) as part of its own PR.

2. **Open a release-prep PR** that does exactly two things:
   - sets `version` in `pyproject.toml` to the new release version
     (e.g. `1.2.1.dev1` → `1.2.1`);
   - renames the `## [Unreleased]` heading in `CHANGELOG.md` to
     `## [1.2.1] - YYYY-MM-DD`, adds a fresh empty `## [Unreleased]` above it,
     and adds the version's compare link to the footer.

3. **Merge it** and wait for CI to pass on `master`.

4. **Tag and push.** The tag must be `v` + the exact version:

   ```shell
   git switch master && git pull
   git tag v1.2.1
   git push origin v1.2.1
   ```

5. **Approve the publish.** The `pypi` environment requires a reviewer, so the
   run pauses before upload. Approving it publishes to PyPI and creates the
   GitHub Release with the changelog section as its body.

6. **Verify** in a clean environment:

   ```shell
   python3 -m venv /tmp/verify && /tmp/verify/bin/pip install opa-py-wasm==1.2.1
   cd /tmp && /tmp/verify/bin/python -c "import opapywasm; print(opapywasm.__version__)"
   ```

7. **Reopen the version for development** with a follow-up PR setting
   `version = "1.2.2.dev0"`.

## Versioning policy

The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
`__version__` is not stored in the source — `opapywasm/__init__.py` reads it
from the installed package metadata, so `pyproject.toml` is the single source of
truth and the workflow's version gate keeps the tag honest.

Beyond the usual API-compatibility rules, two things specific to this library
determine the bump:

**The `wasmtime` pin.** The dependency is deliberately constrained to the 46.x
line, because the SDK couples to `wasmtime.py` internals — epoch-interruption
wiring, `Store`/`Memory` API shapes, and the trap-message text matched in
`evaluator._is_epoch_interrupt`.

| Change | Bump |
| --- | --- |
| Widening the range to admit a new, verified `wasmtime` major | **minor** |
| Adopting a `wasmtime` major that changes this SDK's behavior or drops support for the old one | **major** |

**The OPA Wasm ABI.** The SDK targets ABI 1.3 (fixtures built with OPA CLI
1.18.2); see [`docs/opa_abi.md`](docs/opa_abi.md).

| Change | Bump |
| --- | --- |
| Adding support for a newer ABI while keeping the current one working | **minor** |
| Dropping support for an ABI version, so policies that used to load no longer do | **major** |

Dropping a supported Python version is a **major** bump. Adding one is a
**minor**.

## Dry run against TestPyPI

Before a first release, or any release you want to rehearse, run the
**Release** workflow manually (`workflow_dispatch`) with the *Publish to
TestPyPI* input checked. It runs the same gates and uploads to TestPyPI.

Installing from TestPyPI needs a fallback index, because `wasmtime` is only on
real PyPI:

```shell
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  opa-py-wasm==1.2.1
```

TestPyPI versions are immutable too, so bump the `.devN` suffix for each
rehearsal rather than trying to overwrite.

## If a release is bad

You cannot delete or replace it. Instead:

1. **Yank it** on PyPI (*Manage project* → *Releases* → *Yank*). A yanked
   version disappears from new dependency resolutions but still installs for
   anyone who pins it exactly, so it never breaks an existing lockfile.
2. **Fix forward** with a new patch version, following the loop above.

Only use *Delete* for a version that no one could have installed — deletion
permanently burns the version number, since PyPI will not accept a re-upload.

## One-time setup

Already done for this repository; recorded here for reference or if the project
moves.

**On PyPI** — *Manage project* → *Publishing* → add a Trusted Publisher:

| Field | Value |
| --- | --- |
| Owner | `intuit` |
| Repository | `opa-py-wasm` |
| Workflow | `release.yml` |
| Environment | `pypi` |

Repeat on [TestPyPI](https://test.pypi.org/) with environment `testpypi`.

Trusted Publishing uses OIDC, so **no API token is stored in this repository**
and there is nothing to rotate. PyPI also records build provenance
automatically for uploads made this way.

**On GitHub** — *Settings* → *Environments*, create `pypi` and `testpypi`. On
`pypi`, add required reviewers so every publish needs a human approval, and
restrict deployment to tags matching `v*`.
