# Security Policy

## Supported versions

Security fixes are released for the latest minor version line. Older lines are
not patched — upgrade to the latest release to receive fixes.

| Version | Supported |
| --- | --- |
| 1.2.x | Yes |
| < 1.2 | No |

## Reporting a vulnerability

**Please do not report security vulnerabilities in public issues, pull
requests, or discussions.**

Report privately through GitHub's
[private vulnerability reporting](https://github.com/intuit/opa-py-wasm/security/advisories/new)
(*Security* → *Report a vulnerability*). If that is unavailable to you, open a
regular issue that says only that you have a security report and asks for a
private channel — no details.

Please include, as far as you can:

- the affected version, plus your Python and `wasmtime` versions;
- a description of the issue and its impact;
- steps or a minimal policy/input that reproduces it;
- any suggested fix.

We aim to acknowledge a report within 3 business days and to keep you updated as
we work on a fix. We will credit you in the advisory unless you ask us not to.

## Scope

This SDK evaluates OPA policies compiled to WebAssembly, in-process, on the
`wasmtime` runtime. Issues that are in scope include:

- escaping the Wasm sandbox, or reaching host resources from guest policy code;
- bypassing the configured resource limits (`max_memory_pages`,
  `eval_timeout_seconds`, input/data/result byte caps) to hang or exhaust the
  host;
- returning an incorrect policy decision — for example a decision that reads as
  "allow" when the policy denies;
- leaking one evaluation's data or input into another across the instance pool;
- memory-safety or lifecycle faults in the native `Store`/`Memory`/`Instance`
  handling.

Out of scope:

- vulnerabilities in [`wasmtime`](https://github.com/bytecodealliance/wasmtime)
  or [OPA](https://github.com/open-policy-agent/opa) themselves — please report
  those upstream (we will happily help you route a report);
- insecure Rego written by a policy author, which is a property of the policy
  and not of this SDK;
- host builtins registered by a consumer via `register_builtin`, which run as
  ordinary Python with the host's privileges. Bounding what a builtin can do,
  and enforcing its own timeout, is the caller's responsibility — this is
  documented behavior, not a vulnerability.

## A note on resource limits

`eval_timeout_seconds` bounds guest **Wasm** execution using Wasmtime epoch
interruption. It does not interrupt a blocking Python host builtin. A builtin
that blocks indefinitely will hold its instance indefinitely; that is expected
and documented in [`docs/thread_safety.md`](docs/thread_safety.md).
