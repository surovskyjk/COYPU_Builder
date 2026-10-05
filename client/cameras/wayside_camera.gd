class_name WaysideCamera
extends CameraRig
## Fixed trackside observer (T-124): sits [constant LATERAL_OFFSET_M] to the side and
## [constant HEIGHT_OFFSET_M] above the rail at one station, offset along [AlignmentTable.sample]'s own
## `left`/`up` (not world axes) so it stays correctly clear of the corridor through curves and gradients,
## and eases its look-at onto the lead car as the train approaches/passes ([constant LOOK_AT_TAU_SEC]).
##
## The observer's own position is never damped: it is physically one fixed vantage point until a re-seat
## picks the next one, so easing the position itself would only blur an intentional jump into a slow,
## meaningless drift rather than removing it. Only the look-at target eases.
##
## Re-seat direction is inferred from the sign of consecutive [method PlaybackController.lead_station]
## readings rather than a `direction()` getter on the controller -- the contract this task inherits
## (`docs/tasks/task_124_cameras.md` Preconditions) only promises `lead_station()`, and inferring the sign
## client-side keeps this rig honest about "cameras read T-123's poses, they never re-derive a station
## from a position": the *station* is always read straight from the controller, never recomputed from a
## world position, only its recent trend is examined.

const LATERAL_OFFSET_M := 8.0
const HEIGHT_OFFSET_M := 2.5

## Chosen to visibly ease rather than snap over a typical multi-second flyby without lagging so far behind
## that the camera is still swinging onto the train well after it has passed out of frame.
const LOOK_AT_TAU_SEC := 0.4

## How far ahead (station metres, along the direction of travel) a re-seat places the next vantage point.
const RESEAT_AHEAD_M := 300.0

## How far behind the observer's own station the lead must travel before a re-seat triggers -- large
## enough that small back-and-forth movement near the seat (e.g. a dwell) can't flicker between re-seats.
const RESEAT_TRIGGER_M := 40.0

var _table: AlignmentTable
var _controller: PlaybackController

var _station := 0.0
var _damped_look_at := Vector3.ZERO
var _has_look_at := false

var _prev_lead_station := 0.0
var _has_prev_lead := false
var _travel_sign := 1.0


func bind_corridor(_corridor: TrackCorridor, table: AlignmentTable) -> void:
	_table = table
	if table != null:
		focus_station(table.station_start())


func bind_subject(_trainset: TrainsetNode, controller: PlaybackController) -> void:
	_controller = controller
	_has_prev_lead = false


func station() -> float:
	return _station


func damped_look_at() -> Vector3:
	return _damped_look_at


func focus_station(s: float) -> void:
	if _table == null:
		return
	_station = clampf(s, _table.station_start(), _table.station_end())
	_place_observer()
	_has_look_at = false


func activated() -> void:
	_has_look_at = false


func update(delta: float) -> void:
	if _table == null:
		return
	_place_observer()

	var target := _lead_position()
	if _has_look_at:
		_damped_look_at = CameraRig.damp_vector(_damped_look_at, target, delta, LOOK_AT_TAU_SEC)
	else:
		_damped_look_at = target
		_has_look_at = true
	if not _camera.global_position.is_equal_approx(_damped_look_at):
		_camera.look_at(_damped_look_at, Vector3.UP)

	_maybe_reseat()


func _lead_position() -> Vector3:
	if _controller != null:
		return _table.position_at(_controller.lead_station())
	return _table.position_at(_station)


## `fs.rotation`'s basis columns are (right, up, back) with `right = -left` (`Origin.gd`,
## `docs/data-contracts/coordinate-conventions.md`), so `left = -basis.x`.
func _place_observer() -> void:
	var fs := _table.sample(_station)
	var basis := Basis(fs.rotation)
	var left := -basis.x
	var up := basis.y
	_camera.global_position = fs.position + left * LATERAL_OFFSET_M + up * HEIGHT_OFFSET_M


func _maybe_reseat() -> void:
	if _controller == null:
		return
	var lead := _controller.lead_station()
	if _has_prev_lead:
		var delta_station := lead - _prev_lead_station
		if absf(delta_station) > 1e-4:
			_travel_sign = signf(delta_station)
	_has_prev_lead = true
	_prev_lead_station = lead

	var passed := (
		(_travel_sign > 0.0 and lead > _station + RESEAT_TRIGGER_M)
		or (_travel_sign < 0.0 and lead < _station - RESEAT_TRIGGER_M)
	)
	if not passed:
		return

	var next_station := clampf(
		_station + _travel_sign * RESEAT_AHEAD_M, _table.station_start(), _table.station_end()
	)
	if next_station != _station:
		focus_station(next_station)
