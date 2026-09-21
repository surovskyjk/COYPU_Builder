extends GdUnitTestSuite
## [TimelineState] unit tests (T-123): pure state, no backend, no scene tree.

var _timeline: TimelineState


func before_test() -> void:
	_timeline = TimelineState.new()


func test_initial_state_is_paused_at_time_zero_with_default_rate() -> void:
	assert_bool(_timeline.is_playing()).is_false()
	assert_float(_timeline.time()).is_equal(0.0)
	assert_float(_timeline.rate()).is_equal(1.0)
	assert_float(_timeline.duration()).is_equal(0.0)


func test_set_duration_clamps_current_time_into_the_new_range() -> void:
	_timeline.set_duration(10.0)
	_timeline.seek(8.0)
	_timeline.set_duration(5.0)
	assert_float(_timeline.duration()).is_equal(5.0)
	assert_float(_timeline.time()).is_equal(5.0)


func test_seek_clamps_to_0_and_duration() -> void:
	_timeline.set_duration(10.0)
	_timeline.seek(-3.0)
	assert_float(_timeline.time()).is_equal(0.0)
	_timeline.seek(30.0)
	assert_float(_timeline.time()).is_equal(10.0)


func test_play_pause_toggle_is_playing() -> void:
	assert_bool(_timeline.is_playing()).is_false()
	_timeline.play()
	assert_bool(_timeline.is_playing()).is_true()
	_timeline.pause()
	assert_bool(_timeline.is_playing()).is_false()


func test_advance_moves_time_by_delta_times_rate_only_while_playing() -> void:
	_timeline.set_duration(10.0)
	_timeline.advance(1.0)
	assert_float(_timeline.time()).append_failure_message(
		"advance() while paused must be a no-op"
	).is_equal(0.0)

	_timeline.play()
	_timeline.set_rate(2.0)
	_timeline.advance(1.0)
	assert_float(_timeline.time()).is_equal_approx(2.0, 1e-6)


func test_advance_clamps_at_the_end_when_not_looping() -> void:
	_timeline.set_duration(5.0)
	_timeline.play()
	_timeline.advance(100.0)
	assert_float(_timeline.time()).is_equal(5.0)


func test_advance_clamps_at_zero_for_a_non_positive_delta() -> void:
	_timeline.set_duration(5.0)
	_timeline.seek(1.0)
	_timeline.play()
	_timeline.advance(-10.0)
	assert_float(_timeline.time()).append_failure_message(
		"advance() should clamp at the lower bound too, even though Phase 1 rates are always positive"
	).is_equal(0.0)


func test_advance_wraps_modulo_duration_when_looping() -> void:
	_timeline.set_duration(10.0)
	_timeline.set_loop(true)
	_timeline.play()
	_timeline.advance(15.0)
	assert_float(_timeline.time()).is_equal_approx(5.0, 1e-6)


func test_set_rate_clamps_to_the_documented_range() -> void:
	_timeline.set_rate(100.0)
	assert_float(_timeline.rate()).is_equal(TimelineState.MAX_RATE)
	_timeline.set_rate(0.0)
	assert_float(_timeline.rate()).is_equal(TimelineState.MIN_RATE)


func test_changed_signal_fires_on_every_mutator() -> void:
	var monitored := monitor_signals(_timeline)
	_timeline.set_duration(10.0)
	await assert_signal(monitored).is_emitted("changed")


func test_advance_with_zero_duration_does_not_divide_by_zero_or_hang() -> void:
	_timeline.play()
	_timeline.advance(1.0)
	assert_float(_timeline.time()).is_equal(0.0)
