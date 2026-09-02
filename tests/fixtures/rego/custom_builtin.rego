# Policy invoking a user-registered custom builtin.
# The host must register "my.custom_builtin" for this to evaluate.
# entrypoint: authz/result
package authz

result := my.custom_builtin(input.value)
