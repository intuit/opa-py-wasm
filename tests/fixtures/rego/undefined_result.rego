# Policy whose entrypoint rule is undefined unless input.enabled is true,
# producing an empty result set (undefined decision) otherwise.
# entrypoint: authz/allow
package authz

allow if input.enabled == true
