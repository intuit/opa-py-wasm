# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.1] - 2026-09-16

First public release on [PyPI](https://pypi.org/project/opa-py-wasm/). No
library code changed in this release; `1.2.0` and earlier were published
internally only.

### Added
- Automated release pipeline (`.github/workflows/release.yml`): pushing a
  `vX.Y.Z` tag re-runs the full lint and test matrix, refuses to publish if the
  tag disagrees with the version in `pyproject.toml`, and uploads to PyPI via
  Trusted Publishing (OIDC — no stored API token).
- [`RELEASING.md`](RELEASING.md) documenting the release loop, the versioning
  policy for the `wasmtime` pin and the OPA Wasm ABI, the TestPyPI dry run, and
  the yank procedure.
- [`SECURITY.md`](SECURITY.md) with a private vulnerability reporting path and
  an explicit scope.
- `.pre-commit-config.yaml` mirroring the `tox.ini` lint environment, and
  Dependabot coverage for `pip` and `github-actions`.

## [1.2.0] - 2026-08-12

### Fixed
- Fixed wasm instantiation memory sizing and a pool warm-up race.

## [1.1.0] - 2026-07-22

### Fixed
- **Pool no longer leaks a native `Store` on a close-during-borrow race.** When a
  borrower drew a live idle instance in the narrow window between `close()`
  flipping the closed flag and its drain loop running (so the drain never saw
  that instance), the instance was dropped without being closed and its native
  `Store`/`Memory`/`Instance` was freed only at GC. `_take_slot` now closes the
  instance it drops on the closed path, honouring the free-resources-promptly
  guarantee.
- **Pool shutdown flag hardened for free-threaded Python.** The pool's closed
  flag is now a `threading.Event` rather than a plain `bool`, so the reads on the
  `borrow`/`release` hot paths are safely published across threads without
  holding the close lock — correct on both the GIL and a free-threaded (no-GIL,
  3.13t+) interpreter.

## [1.0.0] - 2026-07-22

First functional release: an in-process, thread-safe SDK for evaluating OPA
policies compiled to WebAssembly, built on `wasmtime.py`.

Added resource limits, instance poisoning, and deterministic lifecycle
management; a Java-parity API with runnable examples and a coverage floor; a
thread-safe instance pool with data updates and builtins; single-instance
policy evaluation; the OPA ABI adapter and memory codec; and the Wasmtime
runtime foundation.

### Added
- **`OpaWasmPolicy` facade** — load a compiled policy from bytes/file/stream,
  `set_data`, and `evaluate` / `evaluate_raw`. It owns a bounded, thread-safe
  pool of Wasm instances (one `Store`/`Memory`/`Instance` each), so it is
  constructed once and called concurrently from many threads with no shared
  mutable Wasm state. Entrypoints selectable by name or id; `entrypoints`,
  `builtins`, and `abi_version` metadata exposed. Context-manager support.
- **`set_data`** is O(1) and non-blocking: it swaps an immutable, pre-serialised
  JSON snapshot under a short lock; pooled instances lazily reload on their next
  borrow, so no in-flight evaluation sees stale data.
- **Custom host builtins** via `register_builtin(name, fn)`, plus default
  builtins `sprintf`, `json.is_valid`, and (with the `opa-py-wasm[yaml]` extra)
  `yaml.is_valid` / `yaml.marshal` / `yaml.unmarshal`. Dangerous builtins
  (`http.send`, …) are not registered by default.
- **Resource limits** in `PolicyConfig`: per-instance `max_memory_pages`,
  per-eval `eval_timeout_seconds` (Wasmtime epoch interruption; bounds guest
  Wasm execution, not Python host builtins — a blocking builtin must enforce its
  own timeout), and input/data/result byte caps. A runaway or memory-bomb policy
  traps with a typed error instead of hanging or exhausting host RAM. A
  `max_memory_pages` below the instantiation minimum is rejected with a clear
  `OpaInvalidPolicyError` at construction.
- **Typed error hierarchy** (`OpaWasmError` and subclasses); every failure path
  raises a specific type.
- **`UNDEFINED` sentinel** — `evaluate()` distinguishes an undefined decision
  (`UNDEFINED`) from a policy that returns JSON `null` (`None`). `evaluate_raw`
  returns the full OPA result set.

### Reliability & safety
- **Instance poisoning:** any failure after guest execution begins (trap,
  `opa_abort`, builtin error, timeout, mid-eval memory error) discards the
  instance; the pool rebuilds a clean replacement. No corrupt heap is reused.
- **ABI validation at load:** required exports and their signatures are checked,
  the ABI version is validated, and a module lacking the `opa_eval` fast path
  (ABI < 1.2) is rejected up front.
- **Bounds- and pointer-checked** guest memory access; NaN/Infinity rejected
  (`allow_nan=False`); malformed result shapes raise by default
  (`strict_result_shape`).
- **Deterministic lifecycle:** `close()` wakes threads blocked waiting for an
  instance, drops and frees idle instances (explicit native `close()` on each,
  not GC), and keeps the eval-timeout ticker alive until in-flight evaluations
  finish — so an evaluation closed concurrently still times out. A failed or
  partial construction closes its native Store and tears down the factory ticker
  thread, leaking nothing. After `close()`, `set_data`/`register_builtin` raise
  `OpaPolicyClosedError`.
- **Per-eval deadline is re-armed each evaluation and cleared after success**, so
  an instance left idle in the pool longer than `eval_timeout_seconds` never
  carries a stale, expired deadline into its next use. A failed lazy data refresh
  poisons the instance, so the pool discards and rebuilds it rather than
  circulating one left without data.
- Verified against real compiled OPA fixtures (ABI 1.3, OPA CLI 1.18.2).
  Coverage floor of 90% (suite sits at ~99%); soak, concurrency-stress,
  lifecycle-race, resource-limit, and native-`opa` differential tests included.

### Behavior changes on upgrade
These `PolicyConfig` defaults harden the SDK but **change observed behavior** for
callers who upgrade without overriding config. To preserve the previous behavior,
set the field explicitly:
- `eval_timeout_seconds=30.0` (previously effectively unbounded): a policy that
  runs longer than 30s now traps with `OpaEvaluationError`. Pass `None` to
  disable the deadline.
- `max_memory_pages=4096` (256 MiB/instance; previously unbounded): a policy that
  grows past the cap now traps instead of exhausting host RAM. Pass `None` to
  disable the cap.
- `strict_result_shape=True` (previously lenient): a non-empty result whose shape
  is not `[{"result": ...}]` now raises rather than collapsing to `UNDEFINED`.
  Set `False` for the old lenient behavior.
- `evaluate()` returns the `UNDEFINED` sentinel (not `None`) for an undefined
  decision; a defined JSON `null` still returns `None`. Callers that treated an
  undefined decision as `None` must switch to comparing against `UNDEFINED`.

### Notes
- Bundle `.tar.gz` loading is out of scope (as in `opa-java-wasm`); the SDK
  takes a compiled wasm module.
- See `docs/parity_with_opa_java_wasm.md` for the functional parity comparison.

## [0.1.0] - 2026-07-13

Initial project scaffolding.

<!--
Link references. Public tag history starts at v1.2.1: releases 0.1.0 through
1.2.0 were cut internally before open-sourcing and have no tags in this
repository, so they are not linkable as compare ranges.
-->
[Unreleased]: https://github.com/intuit/opa-py-wasm/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/intuit/opa-py-wasm/releases/tag/v1.2.1
