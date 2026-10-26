class_name TerrainLod
extends RefCounted
## Level-of-detail selection for the terrain field (T-135, Part A). Pure static functions: no scene tree, no
## backend, no randomness, so the same camera and the same listing always select the same tiles.
##
## The quadtree starts from every level-4 tile listed and refines toward level 0. A tile is replaced by its
## children while the camera is closer to it than `split_factor * edge_m`, measured to its axis-aligned
## bounding box in the x-z plane. Only children that the listing actually contains are kept, and a parent
## with no listed child stays. Holes in the listing are holes in the output, never invented tiles: the
## backend owns which tiles exist (`docs/data-contracts/terrain-tiles.md`).
##
## Grid row 0 is south and Godot z points south, so a tile's bounding box runs from `tile_origin.x` to
## `tile_origin.x + edge_m` and from `tile_origin.z - edge_m` to `tile_origin.z`.

const MAX_LEVEL := 4
const DEFAULT_SPLIT_FACTOR := 1.5


## `lists` maps a level (int 0..4) to a Dictionary of `Vector2i(ix, iy)` -> `{edge_m: float,
## tile_origin: Vector3}`. Returns `Vector3i(level, ix, iy)` for every selected tile, nearest first, ties
## broken by (level, ix, iy).
static func select_tiles(
	camera: Vector3, lists: Dictionary, split_factor: float = DEFAULT_SPLIT_FACTOR
) -> Array[Vector3i]:
	var found: Array = []
	var top: Dictionary = lists.get(MAX_LEVEL, {})
	for key: Vector2i in top.keys():
		_visit(camera, lists, split_factor, MAX_LEVEL, key, found)
	found.sort_custom(func(a: Array, b: Array) -> bool: return _before(a, b))
	var selected: Array[Vector3i] = []
	for entry: Array in found:
		selected.append(entry[1])
	return selected


## Distance from `camera` to the x-z bounding box of a tile with this edge and origin, in metres.
static func distance_to_tile(camera: Vector3, edge_m: float, tile_origin: Vector3) -> float:
	var dx := maxf(0.0, maxf(tile_origin.x - camera.x, camera.x - (tile_origin.x + edge_m)))
	var dz := maxf(0.0, maxf(tile_origin.z - edge_m - camera.z, camera.z - tile_origin.z))
	return sqrt(dx * dx + dz * dz)


static func _visit(
	camera: Vector3, lists: Dictionary, split_factor: float, level: int, key: Vector2i, out: Array
) -> void:
	var info: Dictionary = lists[level][key]
	var edge_m: float = info["edge_m"]
	var origin: Vector3 = info["tile_origin"]
	var distance := distance_to_tile(camera, edge_m, origin)
	if level > 0 and distance < split_factor * edge_m:
		var child_lists: Dictionary = lists.get(level - 1, {})
		var children: Array[Vector2i] = []
		for dy in 2:
			for dx in 2:
				var child := Vector2i(2 * key.x + dx, 2 * key.y + dy)
				if child_lists.has(child):
					children.append(child)
		if not children.is_empty():
			for child in children:
				_visit(camera, lists, split_factor, level - 1, child, out)
			return
	out.append([distance, Vector3i(level, key.x, key.y)])


static func _before(a: Array, b: Array) -> bool:
	var da: float = a[0]
	var db: float = b[0]
	if da != db:
		return da < db
	var ka: Vector3i = a[1]
	var kb: Vector3i = b[1]
	if ka.x != kb.x:
		return ka.x < kb.x
	if ka.y != kb.y:
		return ka.y < kb.y
	return ka.z < kb.z
