extends GdUnitTestSuite
## Live backend integration test for CameraManager (T-124): headless, follows a real run. Imports the
## Kralupy `.coypu` fixture (same flow as `test_playback.gd`/`test_track_corridor.gd`), builds a real
## [TrackCorridor] + [TrainsetNode] + [PlaybackController], binds a [CameraManager] to both, plays back,
## and cycles through all three camera modes asserting each produces finite, changing transforms that
## track the consist (AC9).
##
## Also records each mode's average per-frame update cost (AC7) via `print()` rather than asserting it
## against the strict 16.6 ms budget: headless mode has no GPU rendering cost at all, and a shared,
## possibly virtualized CI runner cannot promise a wall-clock frame budget deterministically. This test's
## own assertion instead guards against an outright stall (a frame taking orders of magnitude longer than
## budget) -- the task's closing report carries the real, windowed measurement from `tools\run_dev.ps1`.
##
## Tests run in declaration order and share the corridor/playback/camera manager built in the first test
## (same convention as the other integration suites). `after()` always tears everything down, even when a
## test above failed or timed out, so this suite cannot leak a backend process.

const _READY_TIMEOUT_SEC := 15.0
const _POLL_STEP_SEC := 0.05
const _COYPU_FIXTURE_RELATIVE_PATH := "../backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu"
const _STALL_BUDGET_MS := 500.0  # generous: guards against a hang, not a strict frame-time SLA

var _corridor: TrackCorridor
var _trainset_node: TrainsetNode
var _playback: PlaybackController
var _timeline: TimelineState
var _table: AlignmentTable
var _camera_manager: CameraManager


func before() -> void:
	Backend.start()
	await _await_state(Backend.State.READY, _READY_TIMEOUT_SEC)


func after() -> void:
	if _camera_manager != null:
		_camera_manager.queue_free()
	if _playback != null:
		_playback.unbind()
		_playback.queue_free()
	if _trainset_node != null:
		_trainset_node.queue_free()
	if _corridor != null:
		_corridor.queue_free()
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


func test_build_corridor_and_playback_and_bind_camera_manager() -> void:
	if not _require_ready():
		return

	var project := await Backend.request("project.new", {})
	assert_str(project.type).is_equal("res")

	var fixture_path := ProjectSettings.globalize_path("res://").path_join(_COYPU_FIXTURE_RELATIVE_PATH)
	var imported := await Session.import_coypu(fixture_path)
	assert_bool(imported).append_failure_message("Session.import_coypu failed").is_true()
	if not imported:
		return

	var alignments := Session.alignments()
	assert_int(alignments.size()).is_greater(0)
	if alignments.is_empty():
		return
	var alignment_id: String = alignments[0]["alignment_id"]

	_table = await Session.fetch_alignment_table(alignment_id)
	assert_that(_table).is_not_null()
	if _table == null:
		return

	_corridor = TrackCorridor.new()
	get_tree().root.add_child(_corridor)
	Session.layers().define(TrackCorridor.LAYER_ID, "Track")
	await _corridor.build(alignment_id, _table)
	assert_int(_corridor.chunk_count()).append_failure_message("no chunks were built").is_greater(0)

	var runs := Session.runs()
	assert_int(runs.size()).is_greater(0)
	if runs.is_empty():
		return
	var run_summary: Dictionary = runs[0]
	var run_id: String = run_summary.get("run_id", "")
	var trainset_id: String = run_summary.get("trainset_id", "")

	var run := await Session.fetch_run_table(run_id)
	var trainset_dto: Variant = await Session.fetch_trainset(trainset_id)
	assert_that(run).is_not_null()
	assert_that(trainset_dto).is_not_null()
	if run == null or trainset_dto == null:
		return

	_trainset_node = TrainsetNode.build(trainset_dto as Dictionary)
	get_tree().root.add_child(_trainset_node)
	assert_int(_trainset_node.car_count()).is_greater(0)

	_timeline = TimelineState.new()
	_playback = PlaybackController.new()
	get_tree().root.add_child(_playback)
	_playback.bind(_table, run, _trainset_node, _timeline)

	_camera_manager = CameraManager.new()
	get_tree().root.add_child(_camera_manager)
	_camera_manager.bind_corridor(_corridor, _table)
	_camera_manager.bind_subject(_trainset_node, _playback)

	assert_bool(_camera_manager.active_camera().current).append_failure_message(
		"CameraManager should start with exactly one current camera"
	).is_true()

	# Seek well past the run's very start before playing: `TrainsetKinematics.pose` clamps a car's bogie
	# pivots into the alignment's station range (trainset-chain.md), so for the first several seconds --
	# while the lead car's own pivot-to-pivot span hasn't yet cleared station 0 -- the whole consist's pose
	# is frozen at the clamped start, which would make every camera mode's transform look falsely static.
	_timeline.seek(30.0)
	_timeline.play()


## AC9: each mode produces finite, changing camera transforms that track the consist, over a real run.
## AC7: also records the average per-frame update cost for this mode via print() (see header).
func test_each_mode_tracks_the_consist_with_finite_changing_transforms() -> void:
	if not _require_ready():
		return
	if _camera_manager == null:
		fail("no camera manager -- see the setup test above")
		return

	for mode in [CameraManager.Mode.ORBIT, CameraManager.Mode.WAYSIDE, CameraManager.Mode.CAB]:
		_camera_manager.set_mode(mode)
		await get_tree().process_frame

		var first := _camera_manager.active_camera().global_transform
		var moved := false
		var frame_times_usec: Array = []

		for _i in 90:
			var started := Time.get_ticks_usec()
			await get_tree().process_frame
			frame_times_usec.append(Time.get_ticks_usec() - started)

			var xform := _camera_manager.active_camera().global_transform
			assert_bool(_is_finite_v3(xform.origin)).append_failure_message(
				"mode %d produced a non-finite camera position" % mode
			).is_true()
			# Wayside legitimately holds its *position* fixed between re-seats and only eases its
			# orientation (look-at) onto the passing train, so "changed" must cover the whole transform,
			# not just the origin, or a real, working wayside camera would look like it never tracked.
			if not xform.is_equal_approx(first):
				moved = true

		assert_bool(moved).append_failure_message(
			"mode %d's camera transform never changed across 90 frames of real playback" % mode
		).is_true()

		var total_usec: int = 0
		for t in frame_times_usec:
			total_usec += t
		var avg_ms := (float(total_usec) / frame_times_usec.size()) / 1000.0
		print("camera mode %d: avg frame time %.3f ms over %d frames (headless, no GPU cost)" % [
			mode, avg_ms, frame_times_usec.size()
		])
		assert_float(avg_ms).append_failure_message(
			"mode %d averaged %.1f ms/frame -- looks stalled, not just over budget" % [mode, avg_ms]
		).is_less(_STALL_BUDGET_MS)


func _is_finite_v3(v: Vector3) -> bool:
	return is_finite(v.x) and is_finite(v.y) and is_finite(v.z)
