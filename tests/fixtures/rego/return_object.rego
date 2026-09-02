# Policy returning an object result.
# entrypoint: authz/result
package authz

result := {
	"allowed": input.action == "read",
	"user": input.user,
}
