extends GdUnitTestSuite
## TerrainLod unit tests (T-135, Part A): pure selection over synthetic listings, no backend and no scene
## tree. The listings mirror `terrain.tiles` shapes: `lists[level][Vector2i(ix, iy)] = {edge_m, tile_origin}`,
## with `tile_origin = (ix * edge, 0, -iy * edge)` in Godot axes (row 0 south, so north is -z).


static func _tile(edge: float, ix: int, iy: int) -> Dictionary:
	return {"edge_m": edge, "tile_origin": Vector3(ix * edge, 0.0, -iy * edge)}


## Every tile of every level that covers a square of `l0_side` x `l0_side` level-0 tiles from (0, 0).
static func _square_lists(l0_side: int) -> Dictionary:
	var lists := {}
	for level in 5:
		var edge := 512.0 * (1 << level)
		var span := l0_side >> level
		var map := {}
		for iy in span:
			for ix in span:
				map[Vector2i(ix, iy)] = _tile(edge, ix, iy)
		lists[level] = map
	return lists


func test_camera_near_a_corner_refines_only_nearby_tiles() -> void:
	var lists := _square_lists(64)
	var selected := TerrainLod.select_tiles(Vector3(256.0, 0.0, -256.0), lists)

	# The level-0 tile under the camera is kept at full detail ...
	assert_array(selected).contains(Vector3i(0, 0, 0))
	# ... while the far corner of the 32 km square stays at level 4 (24.6 km away, beyond 1.5 x 8192 m).
	assert_array(selected).contains(Vector3i(4, 3, 3))
	# Refinement is local: the level-0 tiles selected are a small fraction of the 4096 that exist.
	var level0 := 0
	for tile in selected:
		if tile.x == 0:
			level0 += 1
	assert_int(level0).append_failure_message("%d level-0 tiles selected for a camera over one" % level0).is_between(
		1, 64
	)


func test_moving_away_coarsens() -> void:
	var lists := _square_lists(64)
	var near := TerrainLod.select_tiles(Vector3(256.0, 0.0, -256.0), lists)
	var far := TerrainLod.select_tiles(Vector3(100000.0, 0.0, 0.0), lists)

	var near_min := 4
	for tile in near:
		near_min = mini(near_min, tile.x)
	assert_int(near_min).is_equal(0)

	# Every tile is more than 1.5 edges away from a camera 100 km east, so the whole listing is level 4.
	assert_int(far.size()).is_equal(16)
	for tile in far:
		assert_int(tile.x).is_equal(4)


func test_tiles_absent_from_the_lists_are_never_selected() -> void:
	var lists := _square_lists(64)
	# Two holes that a refined parent reaches: the level-0 child (1, 0) of the level-1 tile (0, 0), and the
	# level-2 child (1, 1) of the level-3 tile (0, 0). The camera sits in the level-0 tile (0, 0), so both
	# parents are refined and both holes are visited.
	lists[0].erase(Vector2i(1, 0))
	lists[2].erase(Vector2i(1, 1))
	var selected := TerrainLod.select_tiles(Vector3(256.0, 0.0, -256.0), lists)

	for tile in selected:
		assert_bool(lists[tile.x].has(Vector2i(tile.y, tile.z))).append_failure_message(
			"%s is not in the listing" % tile
		).is_true()
		# Nothing under the missing level-2 tile (1, 1) may appear: no tile is invented for its children.
		var under_hole := tile.x < 2 and (tile.y >> (2 - tile.x)) == 1 and (tile.z >> (2 - tile.x)) == 1
		assert_bool(under_hole).append_failure_message("%s lies under the missing level-2 tile" % tile).is_false()
	# The level-0 tile (1, 0) is missing, so it is not selected and nothing replaces it.
	assert_bool(selected.has(Vector3i(0, 1, 0))).is_false()


func test_parent_stays_when_no_child_is_listed() -> void:
	# A level-4 tile with a listed level-3 tile under it, and nothing listed below level 3: the level-3 tile is
	# the finest one the backend has, so it is selected rather than replaced by nothing.
	var lists := {
		4: {Vector2i(0, 0): _tile(8192.0, 0, 0)},
		3: {Vector2i(0, 0): _tile(4096.0, 0, 0)},
	}
	var selected := TerrainLod.select_tiles(Vector3(0.0, 0.0, 0.0), lists)
	assert_array(selected).is_equal([Vector3i(3, 0, 0)])

	var only_coarse := {4: {Vector2i(0, 0): _tile(8192.0, 0, 0)}}
	assert_array(TerrainLod.select_tiles(Vector3(0.0, 0.0, 0.0), only_coarse)).is_equal([Vector3i(4, 0, 0)])


func test_split_distance_is_one_and_a_half_edges() -> void:
	# Level-1 tile (0, 0) is 1024 m wide and spans x from 0 to 1024: it is refined within 1536 m, not beyond.
	var lists := _square_lists(64)
	var inside := TerrainLod.select_tiles(Vector3(-1535.0, 0.0, -512.0), lists)
	assert_bool(inside.has(Vector3i(1, 0, 0))).append_failure_message("refined at 1535 m expected").is_false()
	assert_bool(inside.has(Vector3i(0, 0, 0))).is_true()
	var outside := TerrainLod.select_tiles(Vector3(-1537.0, 0.0, -512.0), lists)
	assert_bool(outside.has(Vector3i(1, 0, 0))).append_failure_message("kept at 1537 m expected").is_true()
	assert_bool(outside.has(Vector3i(0, 0, 0))).is_false()


func test_camera_south_of_the_grid_measures_to_the_south_edge() -> void:
	# Row 0 is south and Godot z points south, so the grid spans z from -32768 to 0 and a camera at z = +1700 is
	# 1700 m south of level-1 tile (0, 0), beyond 1.5 * 1024 m: the tile is kept whole.
	var lists := _square_lists(64)
	var south := TerrainLod.select_tiles(Vector3(512.0, 0.0, 1700.0), lists)
	assert_bool(south.has(Vector3i(1, 0, 0))).is_true()
	# 1400 m south it is within 1536 m, so the tile is refined.
	var nearer := TerrainLod.select_tiles(Vector3(512.0, 0.0, 1400.0), lists)
	assert_bool(nearer.has(Vector3i(1, 0, 0))).is_false()
	assert_bool(nearer.has(Vector3i(0, 0, 0))).is_true()


func test_camera_east_of_a_tile_measures_to_its_far_edge() -> void:
	# x = 2424 is 1400 m east of the level-1 tile (0, 0) (x from 0 to 1024), within 1536 m, so it is refined.
	var lists := _square_lists(64)
	var selected := TerrainLod.select_tiles(Vector3(2424.0, 0.0, -512.0), lists)
	assert_bool(selected.has(Vector3i(1, 0, 0))).is_false()
	assert_bool(selected.has(Vector3i(0, 1, 0))).is_true()


func test_selection_is_deterministic_and_sorted() -> void:
	var lists := _square_lists(64)
	var camera := Vector3(1000.0, 0.0, -3000.0)
	var first := TerrainLod.select_tiles(camera, lists)
	var second := TerrainLod.select_tiles(camera, lists)
	assert_array(first).is_equal(second)

	var previous_distance := -1.0
	var previous: Vector3i = Vector3i(-1, -1, -1)
	for tile in first:
		var info: Dictionary = lists[tile.x][Vector2i(tile.y, tile.z)]
		var distance := TerrainLod.distance_to_tile(camera, info["edge_m"], info["tile_origin"])
		assert_float(distance).is_greater_equal(previous_distance)
		if distance == previous_distance:
			assert_bool(_key_less(previous, tile)).append_failure_message(
				"tie at %.3f m not ordered by (level, ix, iy): %s before %s" % [distance, previous, tile]
			).is_true()
		previous_distance = distance
		previous = tile


static func _key_less(a: Vector3i, b: Vector3i) -> bool:
	if a.x != b.x:
		return a.x < b.x
	if a.y != b.y:
		return a.y < b.y
	return a.z < b.z
