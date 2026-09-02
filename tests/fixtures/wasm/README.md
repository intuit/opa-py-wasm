# Compiled Wasm fixtures

This directory holds OPA policies compiled to WebAssembly (`*.wasm`), generated
from the Rego sources in `../rego` by `scripts/build_fixtures.py` (dev-only,
requires the OPA CLI).

The compiled `.wasm` files are **committed** so the test suite runs without the
OPA CLI. Tests that need them are marked `@pytest.mark.requires_wasm` and are
skipped automatically when this directory is empty.

To (re)generate:

```shell
python scripts/build_fixtures.py
```
