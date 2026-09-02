# Policy whose decision is a defined JSON null (distinct from undefined).
# Used to prove evaluate() distinguishes null from an undefined decision.
# entrypoint: authz/result
package authz

result := null
