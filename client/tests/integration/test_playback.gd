extends GdUnitTestSuite
## Live backend integration test for playback (T-123): `import.coypu` on the Kralupy fixture -> fetch the
## resulting run's alignment/run tables and trainset -> build a real [TrainsetNode] -> bind a
## [PlaybackController] to a [TimelineState] -> play. Drives the real `Backend`/`Session` autoloads
## directly (same convention as `test_run_import.gd`/`test_track_corridor.gd`), since `Session`'s
## coroutines call `Backend.request` internally rather than a test-owned `BackendFixture`.
##
## Tests run in declaration order and share the trainset/controller built in the first test (same
## convention as the other integration suites). `after()` always calls `Backend.shutdown()` and frees the
## nodes this suite added to the tree, even when a test above failed or timed out, so it cannot leak a
## backend process or a dangling [PlaybackController] still ticking `_process`.

const _READY_TIMEOUT_SEC := 15.0
const _POLL_STEP_SEC := 0.05
const _COYPU_FIXTURE_RELATIVE_PATH := "../backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu"

var _trainset_node: TrainsetNode
var _playback: PlaybackController
var _timeline: TimelineState


func before() -> void:
	Backend.start()
	await _await_state(Backend.State.READY, _READY_TIMEOUT_SEC)


func after() -> void:
	if _playback != null:
		_playback.unbind()
		_playback.queue_free()
	if _trainset_node != null:
		_trainset_node.queue_free()
	Backend.shutdown()


func _await_state(target: Backend.State, timeout_sec: float) -> bool:
	var elapsed := 0.0
	while Backend.state() != target and elapsed < timeout_sec:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	return Backend.state() == target


func _require_ready() -> bool:
	if Backend.state() != Backend.State.READY:
		fail("backend never reached READY")
		return false
	return true


func test_import_run_and_build_wires_a_bound_playback_controller() -> void:
	if not _require_ready():
		return

	var project := await Backend.request("project.new", {})
	assert_str(project.type).is_equal("res")

	var fixture_path := ProjectSettings.globalize_path("res://").path_join(_COYPU_FIXTURE_RELATIVE_PATH)
	var imported := await Session.import_coypu(fixture_path)
	assert_bool(imported).append_failure_message("Session.import_coypu failed").is_true()
	if not imported:
		return

	var runs := Session.runs()
	assert_int(runs.size()).is_greater(0)
	if runs.is_empty():
		return
	var run_summary: Dictionary = runs[0]
	var alignment_id: String = run_summary.get("alignment_id", "")
	var run_id: String = run_summary.get("run_id", "")
	var trainset_id: String = run_summary.get("trainset_id", "")
	assert_str(trainset_id).append_failure_message(
		"import.coypu should attach a trainset to the run (handle_import_coypu's _trainset_for_vehicle)"
	).is_not_empty()

	var table := await Session.fetch_alignment_table(alignment_id)
	assert_that(table).is_not_null()
	var run := await Session.fetch_run_table(run_id)
	assert_that(run).is_not_null()
	var trainset_dto: Variant = await Session.fetch_trainset(trainset_id)
	assert_that(trainset_dto).is_not_null()
	if table == null or run == null or trainset_dto == null:
		return

	_trainset_node = TrainsetNode.build(trainset_dto as Dictionary)
	get_tree().root.add_child(_trainset_node)
	assert_int(_trainset_node.car_count()).is_greater(0)

	_timeline = TimelineState.new()
	_playback = PlaybackController.new()
	get_tree().root.add_child(_playback)
	_playback.bind(table, run, _trainset_node, _timeline)

	# task_123 AC9 / "start paused": bind() must not itself start playback.
	assert_bool(_timeline.is_playing()).is_false()
	assert_float(_timeline.duration()).append_failure_message(
		"bind() should set the timeline's duration from the run table"
	).is_equal_approx(run.duration(), 1e-6)


func test_playing_advances_lead_station_and_poses_the_consist_with_finite_transforms() -> void:
	if not _require_ready():
		return
	if _playback == null:
		fail("no bound controller -- see the wiring test above")
		return

	_timeline.play()
	for _i in 30:
		await get_tree().process_frame

	assert_bool(_timeline.is_playing()).is_true()
	assert_float(_timeline.time()).append_failure_message(
		"30 processed frames while playing should have advanced the timeline"
	).is_greater(0.0)
	assert_float(_playback.lead_station()).append_failure_message(
		"lead_station() should track the run table's station_at(timeline.time())"
	).is_greater(0.0)
	assert_bool(is_finite(_playback.current_speed())).is_true()

	for i in _trainset_node.car_count():
		var car := _trainset_node.car(i)
		assert_bool(_is_finite_v3(car.body().transform.origin)).append_failure_message(
			"car %d body transform is not finite after playback" % i
		).is_true()
		assert_bool(_is_finite_v3(car.bogie_front().transform.origin)).is_true()
		assert_bool(_is_finite_v3(car.bogie_rear().transform.origin)).is_true()

	_timeline.pause()


## task_123 AC4 / F7: the run's last station overshoots the alignment end, so seeking to the run's final
## second must still leave every transform finite rather than producing NaNs off the end of the table.
func test_seeking_to_the_end_of_the_run_keeps_every_transform_finite() -> void:
	if not _require_ready():
		return
	if _playback == null:
		fail("no bound controller -- see the wiring test above")
		return

	_timeline.seek(_timeline.duration())
	await get_tree().process_frame

	for i in _trainset_node.car_count():
		var car := _trainset_node.car(i)
		assert_bool(_is_finite_v3(car.body().transform.origin)).append_failure_message(
			"car %d body transform is not finite at the end of the run" % i
		).is_true()


func _is_finite_v3(v: Vector3) -> bool:
	return is_finite(v.x) and is_finite(v.y) and is_finite(v.z)
