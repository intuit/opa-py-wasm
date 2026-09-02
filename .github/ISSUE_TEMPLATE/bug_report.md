---
name: Bug report
about: Report something that isn't working as expected
title: ''
labels: bug
assignees: ''
---

**Describe the bug**
A clear and concise description of what the bug is.

**To reproduce**
A minimal code sample that reproduces the problem:

```python
from opapywasm import OpaWasmPolicy
# ...
```

If the issue depends on a specific policy, please include a minimal `.rego`
source (or the compiled `.wasm`, if you can share it).

**Expected behavior**
What you expected to happen instead.

**Traceback / output**
```
Paste the full traceback or output here.
```

**Environment**
- `opa-py-wasm` version:
- Python version:
- `wasmtime` version:
- OS:
- OPA version used to compile the policy (if known):

**Additional context**
Anything else that might help — concurrency involved, pool size, resource
limits configured, etc.
