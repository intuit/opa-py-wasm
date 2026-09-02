# Policy exercising the default host builtins the SDK ships with.
# entrypoint: authz/result
package authz

result := {
	"greeting": sprintf("hello %s", [input.name]),
	"valid_json": json.is_valid(input.doc),
	"valid_yaml": yaml.is_valid(input.doc),
}
