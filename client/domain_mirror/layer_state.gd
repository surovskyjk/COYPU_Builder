class_name LayerState
extends RefCounted
## Pure visibility/opacity state for the layers panel (T-142) and whatever renderers subscribe to it
## (T-121 track, T-132 vehicles, …): no scene tree, no rendering, just a small map plus a change signal.
## An undefined layer reads back as visible and fully opaque, so a subscriber can query a layer id before
## anyone has called [method define] without special-casing "unknown".

signal layer_changed(layer_id: String)

var _layers: Dictionary = {}     # layer_id -> {"name": String, "visible": bool, "opacity": float}
var _order: Array[String] = []


func define(layer_id: String, name: String, visible: bool = true, opacity: float = 1.0) -> void:
	if not _layers.has(layer_id):
		_order.append(layer_id)
	_layers[layer_id] = {"name": name, "visible": visible, "opacity": opacity}
	layer_changed.emit(layer_id)


func is_visible(layer_id: String) -> bool:
	var entry: Dictionary = _layers.get(layer_id, {})
	return entry.get("visible", true)


func opacity(layer_id: String) -> float:
	var entry: Dictionary = _layers.get(layer_id, {})
	return entry.get("opacity", 1.0)


func set_visible(layer_id: String, visible: bool) -> void:
	if not _layers.has(layer_id):
		return
	_layers[layer_id]["visible"] = visible
	layer_changed.emit(layer_id)


func set_opacity(layer_id: String, opacity: float) -> void:
	if not _layers.has(layer_id):
		return
	_layers[layer_id]["opacity"] = opacity
	layer_changed.emit(layer_id)


func layers() -> Array[Dictionary]:
	var out: Array[Dictionary] = []
	for layer_id: String in _order:
		var entry: Dictionary = _layers[layer_id]
		out.append({
			"layer_id": layer_id,
			"name": entry["name"],
			"visible": entry["visible"],
			"opacity": entry["opacity"],
		})
	return out
