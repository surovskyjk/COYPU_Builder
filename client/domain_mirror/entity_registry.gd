class_name EntityRegistry
extends RefCounted
## Flat id -> (kind, data) store for whatever the backend attaches to the scene (trainsets, stops,
## gizmos, …). Pure bookkeeping: no scene nodes, no signals, no validation of `kind` or `data`'s shape —
## callers own that.

var _entries: Dictionary = {}         # entity_id -> {"kind": String, "data": Dictionary}
var _ids_by_kind: Dictionary = {}     # kind -> Array[String]


func put(entity_id: String, kind: String, data: Dictionary) -> void:
	if _entries.has(entity_id):
		var previous_kind: String = _entries[entity_id]["kind"]
		if previous_kind != kind:
			_unindex(previous_kind, entity_id)
	_entries[entity_id] = {"kind": kind, "data": data}
	_index(kind, entity_id)


func get_entity(entity_id: String) -> Dictionary:     # empty Dictionary when absent
	if not _entries.has(entity_id):
		return {}
	return _entries[entity_id]["data"]


func of_kind(kind: String) -> Array[String]:
	if not _ids_by_kind.has(kind):
		return [] as Array[String]
	return (_ids_by_kind[kind] as Array[String]).duplicate()


func clear() -> void:
	_entries.clear()
	_ids_by_kind.clear()


func _index(kind: String, entity_id: String) -> void:
	if not _ids_by_kind.has(kind):
		_ids_by_kind[kind] = [] as Array[String]
	var ids: Array[String] = _ids_by_kind[kind]
	if not ids.has(entity_id):
		ids.append(entity_id)


func _unindex(kind: String, entity_id: String) -> void:
	if not _ids_by_kind.has(kind):
		return
	var ids: Array[String] = _ids_by_kind[kind]
	ids.erase(entity_id)
