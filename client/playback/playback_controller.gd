class_name PlaybackController
extends Node
## Drives one [TrainsetNode] from one [TimelineState] and [RunTable] every frame (T-123, ADR 0007):
## `_process` advances the timeline, reads the lead station off the run table's own time axis (never
## re-integrates speed itself -- the backend already did that, stops included), poses the consist via
## [TrainsetKinematics.pose] and writes the result straight into the [Car] nodes. No [method Backend.request]
## and no `await` on this path, and the pose array is pre-sized once in [method bind] and reused every
## frame after that.

var _table: AlignmentTable
var _run: RunTable
var _trainset_node: TrainsetNode
var _timeline: TimelineState
var _trainset: Dictionary
var _direction := 1
var _poses: Array = []

var _lead_station := 0.0
var _speed := 0.0
var _bound := false
var _was_playing := false


## `trainset_node`'s `TrainsetDTO` (with `cars`/`coupling_gap_m`, needed by [TrainsetKinematics.pose])
## is read back from `Session.trainset(trainset_node.trainset_id())` -- the cache T-122's
## `Session.fetch_trainset`/`create_trainset` already populated before the node was built -- since
## [TrainsetNode] itself keeps no reference to the dictionary it was assembled from. Sets the timeline's
## duration from `run.duration()` and pre-sizes the reused pose array to `trainset_node.car_count()`.
func bind(table: AlignmentTable, run: RunTable, trainset_node: TrainsetNode, timeline: TimelineState) -> void:
	unbind()

	_table = table
	_run = run
	_trainset_node = trainset_node
	_timeline = timeline
	_direction = run.direction() if run != null else 1

	var dto: Variant = Session.trainset(trainset_node.trainset_id())
	_trainset = dto if dto is Dictionary else {}

	_poses.clear()
	for i in trainset_node.car_count():
		_poses.append(TrainsetKinematics.CarPose.new())

	_timeline.set_duration(run.duration() if run != null else 0.0)
	_was_playing = _timeline.is_playing()
	_timeline.changed.connect(_on_timeline_changed)
	_bound = true


func unbind() -> void:
	if _timeline != null and _timeline.changed.is_connected(_on_timeline_changed):
		_timeline.changed.disconnect(_on_timeline_changed)

	_bound = false
	_table = null
	_run = null
	_trainset_node = null
	_timeline = null
	_trainset = {}
	_poses.clear()
	_lead_station = 0.0
	_speed = 0.0


func lead_station() -> float:
	return _lead_station


func current_speed() -> float:
	return _speed


func _process(delta: float) -> void:
	if not _bound:
		return

	_timeline.advance(delta)
	var t := _timeline.time()
	_lead_station = _run.station_at(t)
	_speed = _run.speed_at(t)

	TrainsetKinematics.pose(_table, _trainset, _lead_station, _direction, _poses)
	for i in _trainset_node.car_count():
		var car_pose: TrainsetKinematics.CarPose = _poses[i]
		_trainset_node.car(i).apply_pose(car_pose.body, car_pose.bogie_front, car_pose.bogie_rear)


## `TimelineState.changed` fires on every mutation (time advancing, seek, play/pause, rate, loop) without
## saying which -- `playback_time_changed` mirrors every one of those (a future scrub bar wants every tick);
## `playback_state_changed` only fires when [method TimelineState.is_playing] actually flips, so a UI's
## play/pause button doesn't get spurious signals for pure time movement.
func _on_timeline_changed() -> void:
	EventBus.playback_time_changed.emit(_timeline.time())
	var playing := _timeline.is_playing()
	if playing != _was_playing:
		_was_playing = playing
		EventBus.playback_state_changed.emit()
