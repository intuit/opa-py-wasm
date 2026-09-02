# Policy whose decision depends on external data.
# allow is true iff data.roles[input.user] == "admin".
# entrypoint: authz/allow
package authz

default allow := false

allow if {
	data.roles[input.user] == "admin"
}
