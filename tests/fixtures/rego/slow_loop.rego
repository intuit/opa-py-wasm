# Policy that does a very large amount of work, used to exercise the per-eval
# execution-time limit (eval_timeout_seconds / epoch interruption). Summing over
# a large numbers.range dominates evaluation time without needing an unbounded
# loop construct (which Rego does not offer directly).
# entrypoint: authz/result
package authz

result := sum([n | some n in numbers.range(1, 50000000)])
