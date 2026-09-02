# Policy exposing multiple entrypoints.
# entrypoints: authz/allow, authz/deny, authz/roles
package authz

default allow := false

allow if input.action == "read"

deny if input.action == "delete"

roles := data.roles
