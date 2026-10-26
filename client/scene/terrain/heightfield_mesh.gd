class_name HeightfieldMesh
extends RefCounted
## Triangulates one terrain display tile (T-135, Part A) from its 129 x 129 backend heights. Pure static
## functions that build plain arrays, so they run on a `WorkerThreadPool` task; no node, no `ArrayMesh`, no
## scene access (the main thread creates those, see [TerrainTile]).
##
## Connectivity is the client's, over heights the backend owns (ADR 0007). Positions are tile-local: vertex
## `(row r, column c)` sits at `x = c * edge / 128`, `z = -r * edge / 128` and `y = heights[r * 129 + c]`,
## and the tile node is placed at `tile_origin`, which is never folded into the vertices (ADR 0004).
##
## Row 0 is the south edge and column 0 the west edge, so `v = 1 - r / 128` puts the north-west corner at
## v = 0 (imagery row 0 is north). Triangles are clockwise seen from above (+y), which is Godot's front face.

const SIDE := 129
const CELLS := 128
const SAMPLES := 16641


## `heights` is row-major, `SIDE * SIDE` values, NaN where there is no data. Returns
## `{vertices, normals, uvs, indices}` ready for `ArrayMesh.add_surface_from_arrays`, or an empty Dictionary
## when `heights` has the wrong size.
static func build(heights: PackedFloat32Array, edge_m: float, skirt_m: float) -> Dictionary:
	if heights.size() != SAMPLES:
		return {}

	var vertices := PackedVector3Array()
	var normals := PackedVector3Array()
	var uvs := PackedVector2Array()
	vertices.resize(SAMPLES)
	normals.resize(SAMPLES)
	uvs.resize(SAMPLES)
	var step := edge_m / CELLS
	var two_steps := 2.0 * step
	for r in SIDE:
		var interior_row := r > 0 and r < CELLS
		for c in SIDE:
			var i := r * SIDE + c
			var h := heights[i]
			vertices[i] = Vector3(float(c) * edge_m / CELLS, 0.0 if is_nan(h) else h, -float(r) * edge_m / CELLS)
			if interior_row and c > 0 and c < CELLS and _five_finite(heights, i):
				# The same arithmetic as _normal for an interior vertex with all four neighbours, inlined because
				# this loop runs 16641 times per tile on the worker. The test compares them bit for bit.
				normals[i] = Vector3(
					-(heights[i + 1] - heights[i - 1]) / two_steps, 1.0, (heights[i + SIDE] - heights[i - SIDE]) / two_steps
				).normalized()
			else:
				normals[i] = _normal(heights, r, c, step)
			uvs[i] = Vector2(float(c) / CELLS, 1.0 - float(r) / CELLS)

	var indices := PackedInt32Array()
	indices.resize(6 * CELLS * CELLS)
	var k := 0
	for r in CELLS:
		for c in CELLS:
			var i00 := r * SIDE + c
			var i01 := i00 + 1
			var i10 := i00 + SIDE
			var i11 := i10 + 1
			if is_nan(heights[i00]) or is_nan(heights[i01]) or is_nan(heights[i10]) or is_nan(heights[i11]):
				continue
			indices[k] = i00
			indices[k + 1] = i11
			indices[k + 2] = i01
			indices[k + 3] = i00
			indices[k + 4] = i10
			indices[k + 5] = i11
			k += 6
	indices.resize(k)

	if skirt_m > 0.0:
		_add_skirt(heights, vertices, normals, uvs, indices, _edge_ids(true, 0), Vector3(0, 0, 1), skirt_m)
		_add_skirt(heights, vertices, normals, uvs, indices, _edge_ids(true, CELLS), Vector3(0, 0, -1), skirt_m)
		_add_skirt(heights, vertices, normals, uvs, indices, _edge_ids(false, 0), Vector3(-1, 0, 0), skirt_m)
		_add_skirt(heights, vertices, normals, uvs, indices, _edge_ids(false, CELLS), Vector3(1, 0, 0), skirt_m)

	return {"vertices": vertices, "normals": normals, "uvs": uvs, "indices": indices}


## Worker entry point: `job` carries `heights`, `edge_m` and `skirt_m` in and receives `arrays` (the
## [method build] result) and `build_usec` (wall time of the build) out. Only the worker writes the job until
## the main thread has waited for the task.
static func compute_job(job: Dictionary) -> void:
	var started := Time.get_ticks_usec()
	job["arrays"] = build(job["heights"], job["edge_m"], job["skirt_m"])
	job["build_usec"] = Time.get_ticks_usec() - started


## Surface normal at (r, c) from central differences, one-sided at the tile edges and next to a NaN. The
## surface is y = f(x, z) with x = c * step and z = -r * step, so the upward normal is (-f_x, 1, -f_z) with
## f_z = -(d/dr) / step, which gives (-f_x, 1, (d/dr) / step).
static func _normal(heights: PackedFloat32Array, r: int, c: int, step: float) -> Vector3:
	if is_nan(heights[r * SIDE + c]):
		return Vector3.UP
	var gx := _slope(heights, r, c, 0, 1, step)
	var gz := _slope(heights, r, c, 1, 0, step)
	return Vector3(-gx, 1.0, gz).normalized()


## True when sample `i` and its four neighbours (east, west, north, south) are all finite.
static func _five_finite(heights: PackedFloat32Array, i: int) -> bool:
	return (
		not is_nan(heights[i])
		and not is_nan(heights[i + 1])
		and not is_nan(heights[i - 1])
		and not is_nan(heights[i + SIDE])
		and not is_nan(heights[i - SIDE])
	)


## Derivative in metres of the height along the grid direction (dr, dc), where +dr is north and +dc is east.
static func _slope(heights: PackedFloat32Array, r: int, c: int, dr: int, dc: int, step: float) -> float:
	var forward := _sample(heights, r + dr, c + dc)
	var backward := _sample(heights, r - dr, c - dc)
	var here := heights[r * SIDE + c]
	if not is_nan(forward) and not is_nan(backward):
		return (forward - backward) / (2.0 * step)
	if not is_nan(forward):
		return (forward - here) / step
	if not is_nan(backward):
		return (here - backward) / step
	return 0.0


## The height at (r, c), or NaN outside the grid.
static func _sample(heights: PackedFloat32Array, r: int, c: int) -> float:
	if r < 0 or r >= SIDE or c < 0 or c >= SIDE:
		return NAN
	return heights[r * SIDE + c]


## The top-surface vertex indices along one tile edge, in order: row `fixed` when `by_row` (a south or north
## edge), column `fixed` otherwise (a west or east edge).
static func _edge_ids(by_row: bool, fixed: int) -> PackedInt32Array:
	var ids := PackedInt32Array()
	for k in SIDE:
		ids.append(fixed * SIDE + k if by_row else k * SIDE + fixed)
	return ids


static func _add_skirt(
	heights: PackedFloat32Array,
	vertices: PackedVector3Array,
	normals: PackedVector3Array,
	uvs: PackedVector2Array,
	indices: PackedInt32Array,
	edge: PackedInt32Array,
	outward: Vector3,
	skirt_m: float
) -> void:
	for k in edge.size() - 1:
		var a := edge[k]
		var b := edge[k + 1]
		if is_nan(heights[a]) or is_nan(heights[b]):
			continue
		var ta := _duplicate(vertices, normals, uvs, a, 0.0)
		var ba := _duplicate(vertices, normals, uvs, a, skirt_m)
		var tb := _duplicate(vertices, normals, uvs, b, 0.0)
		var bb := _duplicate(vertices, normals, uvs, b, skirt_m)
		_emit_outward(vertices, indices, ta, ba, tb, outward)
		_emit_outward(vertices, indices, ba, bb, tb, outward)


static func _duplicate(
	vertices: PackedVector3Array, normals: PackedVector3Array, uvs: PackedVector2Array, source: int, drop: float
) -> int:
	var at := vertices[source] - Vector3(0.0, drop, 0.0)
	vertices.append(at)
	normals.append(normals[source])
	uvs.append(uvs[source])
	return vertices.size() - 1


## Appends the triangle (a, b, c), swapping b and c when its right-hand normal points along `outward`, so that the
## triangle is clockwise seen from outside, which is Godot's front face.
static func _emit_outward(
	vertices: PackedVector3Array, indices: PackedInt32Array, a: int, b: int, c: int, outward: Vector3
) -> void:
	var pa := vertices[a]
	var face := (vertices[b] - pa).cross(vertices[c] - pa)
	if face.dot(outward) > 0.0:
		indices.append_array([a, c, b])
	else:
		indices.append_array([a, b, c])
