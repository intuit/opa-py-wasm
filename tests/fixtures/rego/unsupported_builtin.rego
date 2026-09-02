# Policy invoking a builtin the SDK does NOT provide by default (http.send).
# Evaluating this must raise OpaBuiltinError naming the builtin.
# entrypoint: authz/result
package authz

result := http.send({"method": "get", "url": input.url})
