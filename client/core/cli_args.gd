class_name CliArgs
extends RefCounted
## Parses the user args after `--` on the command line (`tools/run_dev.ps1` passes `--backend-url`,
## `--backend-token` and `--project` this way). Pure and unit-testable: [method parse] takes a
## [PackedStringArray] and returns a [Dictionary]; [method from_cmdline] is the only place that reads
## the real command line.

const _FLAGS := {
	"--backend-url": "backend_url",
	"--backend-token": "backend_token",
	"--project": "project",
}


## Unknown flags are ignored with a [method push_warning]; a recognised flag given without a following
## value is also ignored (with a warning) rather than consuming the next flag as its value.
static func parse(args: PackedStringArray) -> Dictionary:
	var result := {}
	var i := 0
	while i < args.size():
		var arg := args[i]
		if not _FLAGS.has(arg):
			push_warning("CliArgs: unknown argument '%s'" % arg)
			i += 1
			continue
		if i + 1 >= args.size() or _FLAGS.has(args[i + 1]):
			push_warning("CliArgs: '%s' given without a value" % arg)
			i += 1
			continue
		result[_FLAGS[arg]] = args[i + 1]
		i += 2
	return result


static func from_cmdline() -> Dictionary:
	return parse(OS.get_cmdline_user_args())
