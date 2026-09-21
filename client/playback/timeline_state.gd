class_name TimelineState
extends RefCounted
## Pure playback clock (T-123): time, rate, loop, play/pause, one [signal changed] fired on every mutation.
## Knows nothing about alignments, trainsets or the scene tree -- [PlaybackController] reads [method time]
## each frame and calls [method advance]; the timeline UI (T-141) will call the rest. Unit testable without
## a backend (`client/tests/unit/test_timeline_state.gd`).

signal changed()

const MIN_RATE := 0.25
const MAX_RATE := 16.0

var _duration := 0.0
var _time := 0.0
var _playing := false
var _rate := 1.0
var _loop := false


func set_duration(seconds: float) -> void:
	_duration = maxf(seconds, 0.0)
	_time = clampf(_time, 0.0, _duration)
	changed.emit()


func duration() -> float:
	return _duration


func time() -> float:
	return _time


## Clamped to [0, duration] -- Phase 1 supports no reverse playback, but a scrub bar (T-141) can still
## seek to any point within the run.
func seek(t: float) -> void:
	_time = clampf(t, 0.0, _duration)
	changed.emit()


func play() -> void:
	if _playing:
		return
	_playing = true
	changed.emit()


func pause() -> void:
	if not _playing:
		return
	_playing = false
	changed.emit()


func is_playing() -> bool:
	return _playing


## Clamped to [MIN_RATE, MAX_RATE]. Negative rates (reverse playback) are out of scope for Phase 1.
func set_rate(x: float) -> void:
	_rate = clampf(x, MIN_RATE, MAX_RATE)
	changed.emit()


func rate() -> float:
	return _rate


func set_loop(enabled: bool) -> void:
	if _loop == enabled:
		return
	_loop = enabled
	changed.emit()


## `time += delta * rate`; wraps modulo `duration` when looping, otherwise clamps to [0, duration]. A
## no-op while paused or before [method set_duration] has given the timeline a positive span.
func advance(delta: float) -> void:
	if not _playing or _duration <= 0.0:
		return
	var next := _time + delta * _rate
	if _loop:
		next = fposmod(next, _duration)
	else:
		next = clampf(next, 0.0, _duration)
	_time = next
	changed.emit()
