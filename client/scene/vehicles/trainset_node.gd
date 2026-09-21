class_name TrainsetNode
extends Node3D
## An ordered set of [Car]s assembled from a `TrainsetDTO` (T-122). [method build] only builds the tree --
## it assigns no world transforms, so every car sits at the scene origin until T-123 poses it frame by
## frame; assembly and posing are kept separate on purpose (see the task's Contract). Fetching the
## trainset itself (`Session.fetch_trainset`/`create_trainset`) happens once, before [method build] is
## called -- never from `_process` (ADR 0007).

var _trainset_id := ""
var _cars: Array[Car] = []
var _total_length_m := 0.0


## `trainset` is a `TrainsetDTO` dictionary (`trainset_id`, `spec_key`, `name`, `mode`, `gauge_mm`,
## `coupling_gap_m`, `length_m`, `cars`). Cars are built front to back in `trainset["cars"]` order -- the
## same order the wire protocol already guarantees, so no re-sorting happens here. `gauge_mm` is not a
## `CarSpecDTO` field (see [CarMeshBuilder]'s header); it is folded into a copy of each per-car dictionary
## before [method Car.from_spec] so the bogie-frame mesh can be sized from it.
static func build(trainset: Dictionary) -> TrainsetNode:
	var node := TrainsetNode.new()
	node._trainset_id = str(trainset.get("trainset_id", ""))
	node._total_length_m = float(trainset.get("length_m", 0.0))

	var gauge_mm: float = float(trainset.get("gauge_mm", CarMeshBuilder.DEFAULT_GAUGE_MM))
	var car_specs: Array = trainset.get("cars", [])
	for i in car_specs.size():
		var car_spec: Dictionary = (car_specs[i] as Dictionary).duplicate()
		car_spec["gauge_mm"] = gauge_mm
		var car := Car.from_spec(car_spec, i)
		node.add_child(car)
		node._cars.append(car)

	return node


func car_count() -> int:
	return _cars.size()


func car(i: int) -> Car:
	return _cars[i]


func trainset_id() -> String:
	return _trainset_id


func total_length() -> float:
	return _total_length_m
