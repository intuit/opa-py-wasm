# Policy whose decision depends on per-request input.
# allow is true iff input.user == "alice" and input.action == "read".
# entrypoint: authz/allow
package authz

default allow := false

allow if {
	input.user == "alice"
	input.action == "read"
}
