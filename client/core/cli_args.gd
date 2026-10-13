class_name CliArgs
extends RefCounted
## Parses the user args after `--` on the command line (`tools/run_dev.ps1` passes `--attach`,
## `--backend-url` and `--project` this way). Pure and unit-testable: [method parse] takes a
## [PackedStringArray] and returns a [Dictionary]; [method from_cmdline] is the only place that reads
## the real command line.
##
## `--attach` is a switch (no value): attach to an already-running backend, found through its discovery
## record, instead of spawning one (T-126). `--backend-url` alone also means attach. There is no token
## argument: a session token never travels on a command line (ADR 0010).
##
## `--capture <scenario.json>` and `--capture-out <dir>` (T-125) switch the app into scripted screenshot mode;
## see `tools/capture/capture_driver.gd`.

const _SWITCHES := {
	"--attach": "attach",
}

const _FLAGS := {
	"--backend-url": "backend_url",
	"--project": "project",
	"--capture": "capture",
	"--capture-out": "capture_out",
}


## Unknown flags are ignored with a [method push_warning]; a recognised flag given without a following
## value is also ignored (with a warning) rather than consuming the next flag as its value. The value of
## an unrecognised argument is never echoed (it could be a secret passed to an option that no longer
## exists), only the names of unknown flags are (the part before any `=`).
static func parse(args: PackedStringArray) -> Dictionary:
	var result := {}
	var i := 0
	while i < args.size():
		var arg := args[i]
		if _SWITCHES.has(arg):
			result[_SWITCHES[arg]] = true
			i += 1
			continue
		if not _FLAGS.has(arg):
			if arg.begins_with("--"):
				# Only the name: `--name=value` must not echo its value (it could be a secret).
				push_warning("CliArgs: unknown argument '%s'" % arg.get_slice("=", 0))
			else:
				push_warning("CliArgs: unexpected value (not shown)")
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
