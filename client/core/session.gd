extends Node
## Autoload `Session`: the client's read-only mirror of backend document state. T-103 populates only
## what `project.get`/`project.new`/`import.landxml` return on the `result` field; T-115 adds runs,
## layers and the entity registry — do not anticipate that here.

var _project_id := ""
var _crs := ""
var _alignments: Array[Dictionary] = []


func project_id() -> String:
	return _project_id


func crs() -> String:
	return _crs


## `AlignmentSummary` dictionaries, as they arrive on the wire (alignment_id, name, mode, station_start,
## station_end, length, warnings).
func alignments() -> Array[Dictionary]:
	return _alignments


## `result` is a `ProjectInfoResult` dictionary (project.new/project.get/import.landxml's enclosing
## project). Also updates Origin's base point and emits EventBus signals.
func set_project_info(result: Dictionary) -> void:
	_project_id = result.get("project_id", "")
	var crs_value: Variant = result.get("crs")
	_crs = crs_value if crs_value is String else ""

	_alignments.clear()
	for entry: Variant in result.get("alignments", []):
		if entry is Dictionary:
			_alignments.append(entry)

	var base: Variant = result.get("base_point")
	if base is Dictionary:
		Origin.set_base_point(
			base.get("easting", 0.0), base.get("northing", 0.0), base.get("height", 0.0)
		)

	EventBus.project_changed.emit()
	EventBus.alignments_changed.emit()
