extends GdUnitTestSuite
## HeightfieldMesh unit tests (T-135, Part A): the pure array builder, checked directly. The headless dummy
## renderer does not round-trip mesh data, so nothing here goes through a MeshInstance3D.

const SAMPLES := 16641
const SIDE := 129
const TOP_VERTICES := 16641


func _filled(value: float) -> PackedFloat32Array:
	var heights := PackedFloat32Array()
	heights.resize(SAMPLES)
	heights.fill(value)
	return heights


## h = 0.25 c + 0.5 r: an exact plane with dh/dc = 0.25 and dh/dr = 0.5 per index.
func _plane() -> PackedFloat32Array:
	var heights := PackedFloat32Array()
	heights.resize(SAMPLES)
	for r in SIDE:
		for c in SIDE:
			heights[r * SIDE + c] = 0.25 * c + 0.5 * r
	return heights


func test_vertex_positions_follow_the_contract_mapping() -> void:
	var heights := PackedFloat32Array()
	heights.resize(SAMPLES)
	for i in SAMPLES:
		heights[i] = float(i % 7) * 0.125 + float(i / SIDE) * 0.5
	var mesh := HeightfieldMesh.build(heights, 512.0, 1.0)

	var vertices: PackedVector3Array = mesh["vertices"]
	for r in SIDE:
		for c in SIDE:
			var i := r * SIDE + c
			var expected := Vector3(float(c) * 512.0 / 128.0, heights[i], -float(r) * 512.0 / 128.0)
			assert_vector(vertices[i]).append_failure_message(
				"vertex (r=%d, c=%d) is %s, expected %s" % [r, c, vertices[i], expected]
			).is_equal(expected)


func test_nan_quads_are_omitted_and_no_array_holds_nan() -> void:
	var heights := _filled(1.0)
	var hole := 60 * SIDE + 60
	heights[hole] = NAN
	var mesh := HeightfieldMesh.build(heights, 512.0, 2.0)

	# The NaN sample touches four quads, all of which are omitted: 2 * (128 * 128 - 4) top triangles.
	var indices: PackedInt32Array = mesh["indices"]
	var top_triangles := 0
	for t in range(0, indices.size(), 3):
		var max_index := maxi(indices[t], maxi(indices[t + 1], indices[t + 2]))
		if max_index < TOP_VERTICES:
			top_triangles += 1
		assert_bool(indices[t] == hole or indices[t + 1] == hole or indices[t + 2] == hole).is_false()
	assert_int(top_triangles).is_equal(2 * (128 * 128 - 4))

	for key in ["vertices", "normals", "uvs"]:
		var array: Variant = mesh[key]
		for v in array.size():
			var value: Variant = array[v]
			var components := [value.x, value.y] if value is Vector2 else [value.x, value.y, value.z]
			for component: float in components:
				assert_bool(is_nan(component)).append_failure_message(
					"%s[%d] holds NaN" % [key, v]
				).is_false()
	for i in indices.size():
		assert_int(indices[i]).is_between(0, (mesh["vertices"] as PackedVector3Array).size() - 1)


func test_plane_normals_are_exact() -> void:
	# step = edge / 128 = 1 m, so the slopes are 0.25 along x and 0.5 along -z (north is -z).
	var mesh := HeightfieldMesh.build(_plane(), 128.0, 1.0)
	var normals: PackedVector3Array = mesh["normals"]
	var expected := Vector3(-0.25, 1.0, 0.5).normalized()
	for i in SAMPLES:
		assert_vector(normals[i]).is_equal_approx(expected, Vector3.ONE * 1e-6)


func test_skirts_on_four_edges_at_the_right_depth() -> void:
	var edge := 512.0
	var skirt := 2.0
	var mesh := HeightfieldMesh.build(_filled(10.0), edge, skirt)
	var vertices: PackedVector3Array = mesh["vertices"]
	var indices: PackedInt32Array = mesh["indices"]

	# Four edges, 128 segments each, two new vertices (a top copy and a bottom) per segment end: 4 * 128 * 4.
	assert_int(vertices.size()).is_equal(SAMPLES + 4 * 128 * 4)

	var bottoms := 0
	for i in range(SAMPLES, vertices.size()):
		var v := vertices[i]
		if is_equal_approx(v.y, 8.0):
			bottoms += 1
			var on_edge := (
				is_equal_approx(v.x, 0.0)
				or is_equal_approx(v.x, edge)
				or is_equal_approx(v.z, 0.0)
				or is_equal_approx(v.z, -edge)
			)
			assert_bool(on_edge).append_failure_message("bottom vertex %s is not on a tile edge" % v).is_true()
		else:
			assert_float(v.y).is_equal_approx(10.0, 1e-6)
	assert_int(bottoms).is_equal(4 * 128 * 2)

	# Godot draws a triangle whose vertices run clockwise seen from the front, and Plane(a, b, c) takes its points in
	# that order. Every skirt triangle must face away from the tile centre, so the strip is seen from outside.
	var centre := Vector3(edge / 2.0, 0.0, -edge / 2.0)
	for t in range(0, indices.size(), 3):
		if indices[t] < SAMPLES and indices[t + 1] < SAMPLES and indices[t + 2] < SAMPLES:
			continue
		var a := vertices[indices[t]]
		var normal := Plane(a, vertices[indices[t + 1]], vertices[indices[t + 2]]).normal
		var outward := (a + vertices[indices[t + 1]] + vertices[indices[t + 2]]) / 3.0 - centre
		outward.y = 0.0
		assert_bool(normal.dot(outward) > 0.0).append_failure_message(
			"skirt triangle %d is back-facing seen from outside" % t
		).is_true()


func test_uvs_put_the_north_west_corner_at_v_zero() -> void:
	var mesh := HeightfieldMesh.build(_filled(0.0), 512.0, 1.0)
	var uvs: PackedVector2Array = mesh["uvs"]
	var north_west := 128 * SIDE + 0
	var south_east := 0 * SIDE + 128
	var north_east := 128 * SIDE + 128
	assert_vector(uvs[north_west]).is_equal(Vector2(0.0, 0.0))
	assert_vector(uvs[south_east]).is_equal(Vector2(1.0, 1.0))
	assert_vector(uvs[north_east]).is_equal(Vector2(1.0, 0.0))


func test_winding_faces_up() -> void:
	var mesh := HeightfieldMesh.build(_plane(), 512.0, 1.0)
	var vertices: PackedVector3Array = mesh["vertices"]
	var indices: PackedInt32Array = mesh["indices"]
	var checked := 0
	for t in range(0, indices.size(), 3):
		if indices[t] >= TOP_VERTICES or indices[t + 1] >= TOP_VERTICES or indices[t + 2] >= TOP_VERTICES:
			continue
		var normal := Plane(vertices[indices[t]], vertices[indices[t + 1]], vertices[indices[t + 2]]).normal
		assert_bool(normal.y > 0.0).append_failure_message(
			"triangle %d is back-facing seen from above (Godot's front face is clockwise)" % t
		).is_true()
		checked += 1
	assert_int(checked).is_equal(2 * 128 * 128)


func test_skirts_skip_nan_edge_vertices() -> void:
	var heights := _filled(10.0)
	heights[0 * SIDE + 64] = NAN  # one NaN sample on the south edge
	var mesh := HeightfieldMesh.build(heights, 512.0, 2.0)
	var vertices: PackedVector3Array = mesh["vertices"]
	# The two south-edge segments that touch the NaN sample get no skirt: 4 new vertices each.
	assert_int(vertices.size()).is_equal(SAMPLES + 4 * 128 * 4 - 2 * 4)
	for i in range(SAMPLES, vertices.size()):
		assert_bool(is_nan(vertices[i].y)).is_false()
		# no skirt vertex hangs from the NaN sample's placeholder at x = 64 * 4 m on the south edge
		var at_hole := is_equal_approx(vertices[i].x, 256.0) and is_equal_approx(vertices[i].z, 0.0)
		assert_bool(at_hole).append_failure_message("a skirt vertex sits at the NaN sample: %s" % vertices[i]).is_false()


## The interior fast path in HeightfieldMesh.build must match the reference _normal bit for bit, including
## every case that falls back to it (NaN centre, NaN neighbour, tile edges).
func test_fast_normals_match_the_reference() -> void:
	var rng := RandomNumberGenerator.new()
	rng.seed = 7
	var heights := PackedFloat32Array()
	heights.resize(SAMPLES)
	for i in SAMPLES:
		heights[i] = rng.randf_range(-5.0, 5.0)
		if rng.randf() < 0.1:
			heights[i] = NAN
	var step := 512.0 / 128.0
	var mesh := HeightfieldMesh.build(heights, 512.0, 1.0)
	var normals: PackedVector3Array = mesh["normals"]
	var mismatches := 0
	var nan_normals := 0
	for r in SIDE:
		for c in SIDE:
			var i := r * SIDE + c
			var got := normals[i]
			if is_nan(got.x) or is_nan(got.y) or is_nan(got.z):
				nan_normals += 1
			var reference := HeightfieldMesh._normal(heights, r, c, step)
			if got.x != reference.x or got.y != reference.y or got.z != reference.z:
				mismatches += 1
	assert_int(nan_normals).append_failure_message("%d normals hold NaN" % nan_normals).is_equal(0)
	assert_int(mismatches).append_failure_message(
		"%d normals differ from the reference _normal" % mismatches
	).is_equal(0)
