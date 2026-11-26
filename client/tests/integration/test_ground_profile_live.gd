extends GdUnitTestSuite
## Live backend test for the Profile tab (T-137 B). A real backend opens the Kralupy `.coypu` fixture with
## `profile_idealised`, imports the synthetic cutting (`client/tests/fixtures/terrain/synthetic_tin.xml`, EPSG:5514), and
## fetches `alignment.ground_profile` at 10 m through [Session]. The synthetic surface covers only part of the line, so the
## profile has ground on some rows and no ground on the rest. Nothing here reads the user's data folder.

const _READY_TIMEOUT_SEC := 15.0
const _POLL_STEP_SEC := 0.05
const _COYPU := "backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu"
const _TIN := "client/tests/fixtures/terrain/synthetic_tin.xml"


func before() -> void:
	Session.set_ground_profile_request_override(Callable())
	Backend.start()
	await _await_state(Backend.State.READY, _READY_TIMEOUT_SEC)


func after() -> void:
	Backend.shutdown()


func after_test() -> void:
	Session.set_ground_profile_request_override(Callable())


## One test, as the steps share one backend project: the profile is fetched once and checked, then drawn.
func test_ground_profile_over_the_live_backend() -> void:
	if Backend.state() != Backend.State.READY:
		fail("backend never reached READY")
		return
	var coypu := ProjectSettings.globalize_path("res://").path_join("../" + _COYPU).simplify_path()
	var tin := ProjectSettings.globalize_path("res://").path_join("../" + _TIN).simplify_path()
	# As the app does at start-up: a new project, then the archive into it.
	var project := await Backend.request("project.new", {})
	assert_str(project.type).append_failure_message("project.new failed: %s" % str(project.error)).is_equal("res")
	assert_bool(await Session.import_coypu(coypu, {"profile_idealised": true})).is_true()
	var alignments := Session.alignments()
	assert_int(alignments.size()).is_greater(0)
	if alignments.is_empty():
		return
	var alignment_id := String(alignments[0]["alignment_id"])
	assert_bool(bool(alignments[0].get("profile_idealised", false))).append_failure_message(
		"the alignment entry does not carry profile_idealised"
	).is_true()

	var imported := await Backend.request("import.run", {
		"path": tin, "kind": "terrain", "settings": {"horizontal_crs": "EPSG:5514"}, "name": "Synthetic cutting",
	}, 90.0)
	assert_str(imported.type).append_failure_message("the terrain import failed: %s" % str(imported.error)).is_equal("res")
	if imported.type != "res":
		return
	# The import dialog announces a terrain import this way, and the profile cache follows it.
	EventBus.sources_changed.emit("terrain")

	var profile := await Session.fetch_ground_profile(alignment_id, 10.0)
	assert_bool(profile.is_empty()).append_failure_message("the profile failed: %s" % Session.ground_profile_error()).is_false()
	if profile.is_empty():
		return

	var stations: PackedFloat64Array = profile["station"]
	var flags: PackedByteArray = profile["flags"]
	var ground: PackedFloat64Array = profile["ground_h"]
	assert_int(stations.size()).is_equal(int(profile["row_count"]))
	for i in range(1, stations.size()):
		assert_bool(stations[i] > stations[i - 1]).append_failure_message("station %d is not increasing" % i).is_true()

	var with_ground := 0
	var without_ground := 0
	for i in stations.size():
		if (flags[i] & 1) == 0 and not is_nan(ground[i]):
			with_ground += 1
		else:
			without_ground += 1
	assert_int(with_ground).append_failure_message("no row has ground").is_greater(0)
	assert_int(without_ground).append_failure_message("every row has ground; the synthetic surface should cover part of the line").is_greater(0)
	var volume := float(profile["cut_volume_m3"]) + float(profile["fill_volume_m3"])
	assert_float(volume).append_failure_message("the cut and fill volumes are both zero").is_greater(0.0)
	assert_int(int(profile["terrain_revision"])).is_greater_equal(0)

	var panel := ProfilePanel.new()
	panel.custom_minimum_size = Vector2(900.0, 240.0)
	add_child(panel)
	auto_free(panel)
	await get_tree().process_frame
	panel.show_profile(profile, alignment_id)
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.READY)
	assert_bool(panel.plot_rect().size.x > 0.0 and panel.plot_rect().size.y > 0.0).is_true()
	assert_bool(panel.idealised_note().visible).append_failure_message("the idealised note is not shown").is_true()


func _await_state(target: Backend.State, timeout_sec: float) -> bool:
	var elapsed := 0.0
	while Backend.state() != target and elapsed < timeout_sec:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	return Backend.state() == target
