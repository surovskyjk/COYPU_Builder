extends GdUnitTestSuite
## TerrainMaterial (T-135, Part B): the decode of a tile's images, the orientation of the imagery over the heightfield's
## vertices, and the uniforms the shader reads. Pure: no backend, no scene. The decode runs in the job it runs in on a
## worker (`decode_job` is synchronous here; the field only moves it off the main thread).

const _SIDE := 129
const _SAMPLES := 16641
const _IMAGE := 512


## A 512 x 512 JPEG of a green field with a red 16 x 16 marker whose centre is pixel (40, 40), the north-west quadrant.
static func _marked_jpeg() -> PackedByteArray:
	var image := Image.create_empty(_IMAGE, _IMAGE, false, Image.FORMAT_RGB8)
	image.fill(Color(0.1, 0.5, 0.2))
	image.fill_rect(Rect2i(32, 32, 16, 16), Color(1.0, 0.0, 0.0))
	return image.save_jpg_to_buffer(0.95)


static func _zeros(count: int) -> PackedByteArray:
	var bytes := PackedByteArray()
	bytes.resize(count)
	return bytes


static func _flat_heights() -> PackedFloat32Array:
	var heights := PackedFloat32Array()
	heights.resize(_SAMPLES)
	return heights


func test_imagery_marker_lands_on_the_right_vertex() -> void:
	var job := {"jpeg": _marked_jpeg(), "alpha": PackedByteArray(), "source_index": _zeros(_SAMPLES)}
	TerrainMaterial.decode_job(job)
	assert_str(String(job["decode_error"])).is_empty()
	var albedo: Image = job["albedo"]
	var arrays := HeightfieldMesh.build(_flat_heights(), 512.0, 1.0)
	var uvs: PackedVector2Array = arrays["uvs"]
	var vertices: PackedVector3Array = arrays["vertices"]

	# Row 118 of the 129 heights rows is north of the middle (row 0 is south), column 10 is west of it. Its UV is
	# (10 / 128, 1 - 118 / 128), which is the marker's centre (40, 40) once it is scaled to the 512 pixel image: the
	# image's row 0 is north and v = 0 is its top edge.
	var north_west := 118 * _SIDE + 10
	assert_float(uvs[north_west].x).is_equal_approx(10.0 / 128.0, 1e-6)
	assert_float(uvs[north_west].y).is_equal_approx(1.0 - 118.0 / 128.0, 1e-6)
	var pixel := Vector2i(roundi(uvs[north_west].x * _IMAGE), roundi(uvs[north_west].y * _IMAGE))
	assert_vector(Vector2(pixel)).is_equal(Vector2(40.0, 40.0))
	var sample := albedo.get_pixelv(pixel)
	assert_bool(sample.r > 0.8 and sample.g < 0.3).append_failure_message(
		"the marker is not at the north-west vertex's UV: pixel %s is %s" % [str(pixel), str(sample)]
	).is_true()
	# The vertex the other way round, south-east (row 10, column 118), must read the field, not the marker.
	var south_east := 10 * _SIDE + 118
	var far := albedo.get_pixelv(Vector2i(roundi(uvs[south_east].x * _IMAGE), roundi(uvs[south_east].y * _IMAGE)))
	assert_bool(far.r > 0.8).append_failure_message("the marker also lies at the south-east vertex").is_false()
	# The vertex itself is north-west: west of the origin of the tile, and at the north edge (z = -r * edge / 128).
	assert_float(vertices[north_west].x).is_equal_approx(10.0 * 512.0 / 128.0, 1e-4)
	assert_float(vertices[north_west].z).is_equal_approx(-118.0 * 512.0 / 128.0, 1e-4)


func test_alpha_texture_only_when_has_alpha() -> void:
	var opaque := {"jpeg": _marked_jpeg(), "alpha": PackedByteArray(), "source_index": _zeros(_SAMPLES)}
	TerrainMaterial.decode_job(opaque)
	var without := TerrainMaterial.for_images(opaque["albedo"], opaque["alpha_image"], opaque["source_image"])
	assert_bool(without.has_alpha()).is_false()
	assert_object(without.get_shader_parameter("alpha_tex")).is_null()

	# Every pixel transparent is the strongest case for the discard; the blob is zero-filled.
	var cut := {"jpeg": _marked_jpeg(), "alpha": _zeros(_IMAGE * _IMAGE), "source_index": _zeros(_SAMPLES)}
	TerrainMaterial.decode_job(cut)
	assert_str(String(cut["decode_error"])).is_empty()
	var masked := TerrainMaterial.for_images(cut["albedo"], cut["alpha_image"], cut["source_image"])
	assert_bool(masked.has_alpha()).is_true()
	assert_object(masked.get_shader_parameter("alpha_tex")).is_not_null()
	var alpha_texture := masked.get_shader_parameter("alpha_tex") as ImageTexture
	assert_int(alpha_texture.get_width()).is_equal(_IMAGE)
	assert_int(alpha_texture.get_image().get_format()).is_equal(Image.FORMAT_R8)


func test_source_index_texture_is_nearest_filtered_r8() -> void:
	var index := _zeros(_SAMPLES)
	index[0] = 3
	index[_SAMPLES - 1] = TerrainMaterial.NONE_INDEX
	var job := {"jpeg": _marked_jpeg(), "alpha": PackedByteArray(), "source_index": index}
	TerrainMaterial.decode_job(job)
	var source: Image = job["source_image"]
	assert_int(source.get_format()).is_equal(Image.FORMAT_R8)
	assert_int(source.get_width()).is_equal(_SIDE)
	assert_int(source.get_height()).is_equal(_SIDE)
	# Heights row 0 (south) is image row 128 after the flip, so index[0] reads at (0, 128) and the last sample at (128, 0).
	assert_int(roundi(source.get_pixel(0, _SIDE - 1).r * 255.0)).is_equal(3)
	assert_int(roundi(source.get_pixel(_SIDE - 1, 0).r * 255.0)).is_equal(TerrainMaterial.NONE_INDEX)
	# The shader samples the map with nearest filtering: the hint is part of the one declaration of the uniform.
	assert_bool(TerrainMaterial.shared_shader().code.contains("uniform sampler2D source_tex : filter_nearest")).is_true()


func test_a_truncated_jpeg_is_reported_not_drawn() -> void:
	var job := {"jpeg": PackedByteArray([1, 2, 3]), "alpha": PackedByteArray(), "source_index": _zeros(_SAMPLES)}
	TerrainMaterial.decode_job(job)
	assert_str(String(job["decode_error"])).is_not_empty()
	assert_object(job.get("albedo")).is_null()


func test_a_wrong_sized_source_blob_is_reported() -> void:
	var job := {"jpeg": _marked_jpeg(), "alpha": PackedByteArray(), "source_index": _zeros(100)}
	TerrainMaterial.decode_job(job)
	assert_str(String(job["decode_error"])).contains("source index")


func test_overlay_colours_are_distinct_for_the_first_sources() -> void:
	var seen := {}
	for index in 8:
		var colour := TerrainMaterial.overlay_colour(index)
		var key := "%.3f,%.3f,%.3f" % [colour.r, colour.g, colour.b]
		assert_bool(seen.has(key)).append_failure_message("source %d repeats a colour" % index).is_false()
		seen[key] = true


func test_boundaries_flag_is_a_shader_parameter() -> void:
	var job := {"jpeg": _marked_jpeg(), "alpha": PackedByteArray(), "source_index": _zeros(_SAMPLES)}
	TerrainMaterial.decode_job(job)
	var material := TerrainMaterial.for_images(job["albedo"], null, job["source_image"])
	assert_bool(material.boundaries_on()).is_false()
	material.set_boundaries(true)
	assert_bool(material.boundaries_on()).is_true()
	assert_float(float(material.get_shader_parameter("boundary_tint"))).is_equal_approx(0.1, 1e-6)


## The source map is read with the albedo's UV, so its texel for a vertex must be the one at the vertex's UV: row
## 0 of the heights is south, row 0 of the image is north, and the flip happens once, in the decode.
func test_source_map_texel_matches_its_vertex() -> void:
	var r := 20
	var c := 40
	var index := _zeros(_SAMPLES)
	index[r * _SIDE + c] = 7
	var job := {"jpeg": _marked_jpeg(), "alpha": PackedByteArray(), "source_index": index}
	TerrainMaterial.decode_job(job)
	assert_str(String(job["decode_error"])).is_empty()
	var source: Image = job["source_image"]
	var arrays := HeightfieldMesh.build(_flat_heights(), 512.0, 1.0)
	var uv: Vector2 = (arrays["uvs"] as PackedVector2Array)[r * _SIDE + c]
	var texel := Vector2i(roundi(uv.x * (_SIDE - 1)), roundi(uv.y * (_SIDE - 1)))
	assert_int(roundi(source.get_pixelv(texel).r * 255.0)).append_failure_message(
		"the texel %s of vertex (%d, %d) does not hold its source index" % [str(texel), r, c]
	).is_equal(7)


## The boundary-only material makes its source map by the same helper as the draped one, so the same vertex reads the
## same texel through it: this is the path that a second copy of the flip would leave unpinned.
func test_boundary_material_source_map_texel_matches_its_vertex() -> void:
	var r := 20
	var c := 40
	var index := _zeros(_SAMPLES)
	index[r * _SIDE + c] = 7
	var material := TerrainMaterial.for_boundaries(index)
	var source := (material.get_shader_parameter("source_tex") as ImageTexture).get_image()
	var arrays := HeightfieldMesh.build(_flat_heights(), 512.0, 1.0)
	var uv: Vector2 = (arrays["uvs"] as PackedVector2Array)[r * _SIDE + c]
	var texel := Vector2i(roundi(uv.x * (_SIDE - 1)), roundi(uv.y * (_SIDE - 1)))
	assert_int(roundi(source.get_pixelv(texel).r * 255.0)).append_failure_message(
		"the boundary material's texel %s of vertex (%d, %d) does not hold its source index" % [str(texel), r, c]
	).is_equal(7)
