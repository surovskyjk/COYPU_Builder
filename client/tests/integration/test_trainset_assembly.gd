extends GdUnitTestSuite
## Live backend integration test for the T-122 vehicle scene: `catalogue.vehicles` -> `trainset.create` ->
## [method TrainsetNode.build] against the real `dmu_br650_cd840` and `tram_generic` catalogue entries
## (`shared/catalogue/vehicles/`). Drives the real `Backend`/`Session` autoloads directly (same convention
## as `test_run_import.gd`/`test_track_corridor.gd`), since `Session.create_trainset` calls
## `Backend.request` internally rather than a test-owned `BackendFixture`.
##
## Tests run in declaration order and share the project set up in the first test (same convention as the
## other integration suites). `after()` always calls `Backend.shutdown()`, even when a test above failed or
## timed out, so this suite cannot leak a backend process.

const _READY_TIMEOUT_SEC := 15.0
const _POLL_STEP_SEC := 0.05
const _DMU_KEY := "dmu_br650_cd840"
const _TRAM_KEY := "tram_generic"

var _project_ready := false


func before() -> void:
	Backend.start()
	await _await_state(Backend.State.READY, _READY_TIMEOUT_SEC)


func after() -> void:
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


func test_fetch_catalogue_from_the_live_backend_resolves_the_dmu_and_tram_fixtures() -> void:
	if not _require_ready():
		return

	var project := await Backend.request("project.new", {})
	assert_str(project.type).is_equal("res")
	_project_ready = project.type == "res"

	await Session.fetch_catalogue()
	var catalogue := Session.vehicle_catalogue()
	assert_int(catalogue.size()).append_failure_message("catalogue.vehicles returned nothing").is_greater(0)

	var keys: Array = []
	for entry: Dictionary in catalogue:
		keys.append(entry.get("key", ""))
	assert_array(keys).append_failure_message(
		"expected '%s' in the live catalogue, got %s" % [_DMU_KEY, keys]
	).contains(_DMU_KEY)
	assert_array(keys).append_failure_message(
		"expected '%s' in the live catalogue, got %s" % [_TRAM_KEY, keys]
	).contains(_TRAM_KEY)


func test_trainset_create_and_build_produce_3_cars_6_bogies_12_wheelsets_for_the_dmu() -> void:
	if not _require_ready():
		return
	if not _project_ready:
		fail("no project -- see 'test_fetch_catalogue_from_the_live_backend_resolves_the_dmu_and_tram_fixtures'")
		return

	# task_122 AC1: a three-car dmu_br650_cd840 consist (units=3 of its single-car spec).
	var trainset_dto: Variant = await Session.create_trainset(_DMU_KEY, 3, "Test DMU consist")
	assert_that(trainset_dto).is_not_null()
	if trainset_dto == null:
		return

	var trainset_node: TrainsetNode = auto_free(TrainsetNode.build(trainset_dto))
	assert_int(trainset_node.car_count()).append_failure_message(
		"a 3-unit dmu_br650_cd840 trainset (1 car/unit) should build 3 Car nodes"
	).is_equal(3)

	var bogie_count := 0
	var wheelset_count := 0
	for i in trainset_node.car_count():
		var car := trainset_node.car(i)
		for bogie in [car.bogie_front() as Bogie, car.bogie_rear() as Bogie]:
			bogie_count += 1
			wheelset_count += bogie.wheelset_count()

	assert_int(bogie_count).append_failure_message("expected 2 bogies/car x 3 cars").is_equal(6)
	assert_int(wheelset_count).append_failure_message("expected 2 wheelsets/bogie x 6 bogies").is_equal(12)


func test_trainset_get_returns_the_same_cached_dto_session_create_trainset_populated() -> void:
	if not _require_ready():
		return
	if not _project_ready:
		fail("no project -- see 'test_fetch_catalogue_from_the_live_backend_resolves_the_dmu_and_tram_fixtures'")
		return

	var created: Variant = await Session.create_trainset(_DMU_KEY, 1, "Cache check")
	assert_that(created).is_not_null()
	if created == null:
		return
	var trainset_id := str((created as Dictionary).get("trainset_id", ""))
	assert_str(trainset_id).is_not_empty()

	var fetched: Variant = await Session.fetch_trainset(trainset_id)
	assert_that(fetched).is_not_null()
	assert_bool(Session.trainset(trainset_id) == fetched).append_failure_message(
		"Session.trainset() should return the exact dictionary fetch_trainset just cached"
	).is_true()


## task_122 AC4: the same builder, no branch on mode, also produces the real tram fixture (1000 mm gauge,
## three distinct ~8.5-9 m modules) -- see test_car_mesh_builder.gd for the synthetic-DTO coverage of the
## same requirement that does not need a live backend.
func test_trainset_node_build_is_mode_agnostic_for_the_real_tram_fixture() -> void:
	if not _require_ready():
		return
	if not _project_ready:
		fail("no project -- see 'test_fetch_catalogue_from_the_live_backend_resolves_the_dmu_and_tram_fixtures'")
		return

	var trainset_dto: Variant = await Session.create_trainset(_TRAM_KEY, 1, "Test tram")
	assert_that(trainset_dto).is_not_null()
	if trainset_dto == null:
		return

	var dto := trainset_dto as Dictionary
	assert_float(float(dto.get("gauge_mm", 0.0))).append_failure_message(
		"expected the tram's 1000 mm gauge on the wire"
	).is_equal_approx(1000.0, 1e-6)

	var trainset_node: TrainsetNode = auto_free(TrainsetNode.build(dto))
	assert_int(trainset_node.car_count()).append_failure_message(
		"tram_generic has 3 car modules per unit"
	).is_equal(3)

	for i in trainset_node.car_count():
		var car := trainset_node.car(i)
		assert_int((car.bogie_front() as Bogie).wheelset_count()).is_equal(2)
		assert_int((car.bogie_rear() as Bogie).wheelset_count()).is_equal(2)
