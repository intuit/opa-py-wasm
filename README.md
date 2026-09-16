# opa-py-wasm

[![Python versions](https://img.shields.io/pypi/pyversions/opa-py-wasm?logo=python&logoColor=white&cacheSeconds=3600)](https://pypi.org/project/opa-py-wasm/)
[![CI](https://github.com/intuit/opa-py-wasm/actions/workflows/ci.yml/badge.svg)](https://github.com/intuit/opa-py-wasm/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)
[![PyPI](https://img.shields.io/pypi/v/opa-py-wasm?cacheSeconds=3600)](https://pypi.org/project/opa-py-wasm/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](https://github.com/intuit/opa-py-wasm/blob/main/LICENSE)

`opa-py-wasm` is an in-process, thread-safe Python SDK for evaluating
[Open Policy Agent](https://www.openpolicyagent.org/) policies compiled to
WebAssembly, built on [`wasmtime.py`](https://github.com/bytecodealliance/wasmtime-py).
It requires no OPA server, no OPA binary, no subprocess, and no network at
runtime. It is functionally comparable to StyraOSS `opa-java-wasm`, with a
Pythonic API and explicit thread-safety guarantees.

> **Import name:** the package installs as `opa-py-wasm` and is imported as
> `opapywasm`.

## Installation

```shell
pip install opa-py-wasm
```

Or with [uv](https://docs.astral.sh/uv/):

```shell
uv add opa-py-wasm
```

Requires Python 3.10 or newer. The only runtime dependency is
[`wasmtime`](https://pypi.org/project/wasmtime/).

## Usage

```python
from opapywasm import UNDEFINED, OpaWasmPolicy, PolicyConfig

policy = OpaWasmPolicy.from_wasm_file(
    "policy.wasm",
    config=PolicyConfig(
        pool_size=8,
        default_entrypoint="authz/allow",
        eval_timeout_seconds=30.0,   # per-eval deadline (epoch interruption)
        max_memory_pages=4096,       # per-instance linear-memory cap (256 MiB)
    ),
)

policy.set_data({"roles": {"alice": "admin", "bob": "viewer"}})

decision = policy.evaluate({"user": "alice", "action": "delete"})   # simplified decision
raw = policy.evaluate_raw({"user": "alice", "action": "delete"})    # full OPA result set

# `evaluate` distinguishes an undefined decision from a defined JSON null:
if decision is UNDEFINED:
    ...          # policy produced no result
elif decision is None:
    ...          # policy result was JSON null

# Custom host builtins (receive decoded Python args, return JSON-compatible values):
policy.register_builtin("my.custom", lambda x: {"ok": True})
```

`OpaWasmPolicy` is a bounded, thread-safe pool — construct it once and call
`evaluate` from many threads. Runnable scripts are in [`examples/`](https://github.com/intuit/opa-py-wasm/tree/main/examples):
`basic_authz.py`, `pooled_eval.py`, `custom_builtin.py`, `yaml_builtin.py`.

For the YAML default builtins, install the optional extra:

```shell
uv add "opa-py-wasm[yaml]"
```

Start with the [**user guide**](https://github.com/intuit/opa-py-wasm/blob/main/docs/guide.md) — an end-to-end walkthrough
covering compiling a policy, result semantics, concurrency, data updates, memory
behaviour, host builtins, and a production adoption checklist.

See [`docs/`](https://github.com/intuit/opa-py-wasm/tree/main/docs) for the focused references: architecture, thread-safety,
the OPA Wasm ABI, the `opa-java-wasm` parity table, and
[capacity-planning guidance](https://github.com/intuit/opa-py-wasm/blob/main/docs/capacity_planning.md) for sizing pool size
and concurrency.

### Test fixtures (dev only)

Compiled `.wasm` fixtures are committed under `tests/fixtures/wasm`, so the test
suite needs no OPA CLI. To regenerate them from the Rego sources in
`tests/fixtures/rego`, install the [OPA CLI](https://www.openpolicyagent.org/docs/latest/#running-opa)
and run:

```shell
python scripts/build_fixtures.py           # all fixtures
python scripts/build_fixtures.py allow_true # a single fixture
```

## Local Development

### uv

This library uses [uv to manage Python dependencies](https://docs.astral.sh/uv/getting-started/features/#projects).  
`brew` is The easiest way to install on macOS:

```shell
brew install uv
```

For additional installation options (e.g. setting the PATH, installing a specific version, etc),
see the installation docs:  
https://docs.astral.sh/uv/getting-started/installation/

## Python Versions Supported

Python 3.10 through 3.14 are supported and tested in CI.

To change which versions are supported and tested:
- Update "envlist" in "tox" section of [tox.ini](/tox.ini)
- Update "Supported Python versions" badge in [README.md](/README.md)
- Update "project.requires-python" in [pyproject.toml](/pyproject.toml) (if needed)
- Update "tool.black.target-version" in [pyproject.toml](/pyproject.toml) (optional)
- Update the matrix in [.github/workflows/ci.yml](/.github/workflows/ci.yml)

### Virtual Environment

Create by running:

```shell
uv sync --all-extras
```

Run a command from the virtual environment, like code formatting:

```shell
uv run black .
```

To activate the virtual environment:

```shell
source .venv/bin/activate
```

For more information, refer to [uv's documentation](https://docs.astral.sh/uv/pip/environments/#using-a-virtual-environment).

### Type checking (mypy)

This library supports Python type [annotation](https://peps.python.org/pep-0484/).
Types will be checked as part of the test suite (see below).
For more information, see the [mypy documentation](https://mypy.readthedocs.io/en/stable/getting_started.html#dynamic-vs-static-typing).

### Testing

To run the test suite locally:
```shell
uv run tox
```

Coverage is enforced on every CI run by `fail_under = 90` in
[`tool.coverage.report`](https://github.com/intuit/opa-py-wasm/blob/main/pyproject.toml); the suite currently sits at 99%. The
coverage badge at the top of this file is static — if you change coverage
materially, update the percentage in that badge along with your change.

## Releasing

Releases are cut by maintainers by pushing a `vX.Y.Z` tag, which triggers an
automated build and publish to PyPI. See [`RELEASING.md`](https://github.com/intuit/opa-py-wasm/blob/main/RELEASING.md) for
the process and the versioning policy. Published versions are listed on
[PyPI](https://pypi.org/project/opa-py-wasm/), and each release's changes are in
[`CHANGELOG.md`](https://github.com/intuit/opa-py-wasm/blob/main/CHANGELOG.md).

## Contributing

Contributions are welcome — see the [Contribution Guidelines](https://github.com/intuit/opa-py-wasm/blob/main/CONTRIBUTING.md)
and our [Code of Conduct](https://github.com/intuit/opa-py-wasm/blob/main/CODE_OF_CONDUCT.md).

## Support

Please open a [GitHub issue](https://github.com/intuit/opa-py-wasm/issues) for
bugs and feature requests, or start a
[discussion](https://github.com/intuit/opa-py-wasm/discussions) for questions.

## License

Released under the [MIT License](https://github.com/intuit/opa-py-wasm/blob/main/LICENSE).
