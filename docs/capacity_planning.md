# Capacity planning

Guidance for sizing `pool_size` and concurrency when hosting one or more
`OpaWasmPolicy` instances in a single process, based on load/stress/soak
testing against production-representative Rego policies (not included in
this repo — see "Methodology" below).

## TL;DR

For 1–10 hosted policies (i.e. 1–10 distinct `OpaWasmPolicy`/
`OpaInstancePool` resources in one process):

- **`pool_size=4`, concurrency up to 2× that pool (8 concurrent callers per
  policy)** produced zero `OpaPoolTimeoutError`s at every policy count
  tested, 1 through 10.
- Keep concurrent callers per policy at or below **~2× its configured
  `pool_size`** in general — timeouts start appearing above that, and the
  risk grows with both oversubscription ratio and total hosted-policy count.
- RSS grows roughly linearly with the number of hosted policies (on the
  order of 5–10 MB per additional policy at `pool_size=4`, depending on
  policy complexity). Thread count scales exactly linearly by construction
  (`N policies × pool_size × concurrency ratio`).
- No memory leak was found in any test: RSS was flat after an initial
  warm-up window in both a 20,000-evaluation sequential soak and a
  12-minute, 1.3M-evaluation concurrent soak.

## Pool sizing vs. concurrency (single policy)

Fixing one policy's `pool_size` and increasing concurrent callers against it:

| Concurrency ratio (callers ÷ pool_size) | Timeouts |
|---|---|
| 0.5×, 1×, 2× | None, at any pool_size tested (4, 8, 16) |
| 4× | First timeouts appear |
| 8× | Timeout rate increases further |

The wait-time tail (time blocked in `borrow()`) grows smoothly with
oversubscription; eval time itself (actual Wasm execution once an instance
is acquired) stays flat — contention shows up entirely as queueing, not as
slower evaluation.

**Guidance:** keep concurrent callers per policy at or below ~2× its
`pool_size` to stay in the zero-timeout zone, with default
`borrow_timeout_seconds` (5.0s).

## Scaling to multiple hosted policies

The above was re-verified with multiple distinct policies (1, 2, 3, 5, 7, 10)
hosted and driven **simultaneously**, each with its own pool, across
`pool_size` ∈ {2, 4, 8} and concurrency ratio ∈ {1×, 1.5×, 2×} — 54
combinations total.

Only the single largest combination tested showed any timeouts: **10 hosted
policies, each at `pool_size=8`, pushed to ≥1.5× concurrency simultaneously**
(120–160 total OS threads across the process). Even there, the timeout rate
stayed under 0.4% of requests. Every other combination — including 10 hosted
policies at `pool_size=2` or `pool_size=4` at any ratio tested — had zero
timeouts.

**Guidance:** `pool_size=4` scales cleanly through at least 10 hosted
policies at up to 2× concurrency. If running many policies (8-10+) with
larger pools (`pool_size=8`+), keep concurrency at or below 1× pool_size, or
reduce `pool_size`.

## Endurance / leak testing

- **Sequential soak** (single-threaded, 5,000 evaluations per policy across
  multiple policies, checkpointed every 25%): RSS and thread count flat
  across all checkpoints; GC live-object count did not trend upward.
- **Concurrent soak** (multiple policies driven simultaneously for 12
  minutes, ~1.3M total evaluations, sampled every 20s): all RSS/thread growth
  happened in the first ~20 seconds (thread and pool warm-up); RSS was flat
  for the remaining ~700 seconds. Zero errors throughout.
- **`set_data()` under concurrent load**: worker threads evaluating
  continuously while `set_data()` is called repeatedly on a short interval
  produced zero errors and no torn/mixed-generation reads — consistent with
  the O(1)-swap, lazy-reload contract described in
  [`thread_safety.md`](thread_safety.md).

## Methodology

Every cell splits each call into "wait time" (blocked in
`OpaInstancePool.borrow()`) and "eval time" (the actual
`OpaPolicyInstance.evaluate()` call), by instrumenting at the same boundary
`evaluate_raw()` uses internally. Resource snapshots
(RSS, CPU%, thread count, GC generation counts, live-object count) were taken
via `psutil` and the stdlib `gc` module before/after each run.

Policies used for this testing were production-representative (realistic
Rego complexity, `data` document dependencies, multiple entrypoints) but are
not included in this repository, since they encode real authorization logic.
The test harness itself (a small Rego test-file parser + pytest suites
driving load/stress/endurance/capacity sweeps) is a generic tool that works
against any committed OPA-Wasm policy + its native `opa test` Rego test
file — if you want to reproduce this methodology against your own policies,
the shape to replicate is:

1. Compile your policy with `opa build -t wasm`.
2. Build one `OpaWasmPolicy` per policy under test, sized with a candidate
   `pool_size`.
3. Drive each with N worker threads (`N = pool_size × concurrency_ratio`),
   each issuing evaluations in a tight loop, instrumented at the `borrow()`
   boundary to separate wait time from eval time.
4. Snapshot RSS/thread count/GC stats before and after.
5. Repeat across a grid of `pool_size` × concurrency ratio × number of
   hosted policies, and look for where `OpaPoolTimeoutError` first appears.
