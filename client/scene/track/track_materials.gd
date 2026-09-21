class_name TrackMaterials
extends RefCounted
## Three placeholder `StandardMaterial3D`s for the track scene (T-121) -- rail, sleeper, ballast --
## distinguishable by albedo and roughness only, no textures, no shaders. T-144's view modes replace these
## wholesale; the `resource_name`s below are the seam a future swap targets.
##
## Each material is built once and cached (static): dozens of chunks sharing one [Material] instance per
## surface lets Godot batch them, and there is no reason for `rail_left`/`rail_right`/repeated `ballast`
## surfaces across chunks to each own a private copy of the same placeholder look.

static var _rail: StandardMaterial3D
static var _sleeper: StandardMaterial3D
static var _ballast: StandardMaterial3D


static func rail_material() -> StandardMaterial3D:
	if _rail == null:
		_rail = _build("TrackMaterial_Rail_Placeholder", Color(0.30, 0.31, 0.33), 0.7, 0.3)
	return _rail


static func sleeper_material() -> StandardMaterial3D:
	if _sleeper == null:
		_sleeper = _build("TrackMaterial_Sleeper_Placeholder", Color(0.24, 0.16, 0.09), 0.0, 0.9)
	return _sleeper


static func ballast_material() -> StandardMaterial3D:
	if _ballast == null:
		_ballast = _build("TrackMaterial_Ballast_Placeholder", Color(0.45, 0.43, 0.39), 0.0, 1.0)
	return _ballast


## Picks the placeholder material for one `alignment.track_mesh` surface name ("rail_left" | "rail_right"
## | "ballast"); an unrecognised surface falls back to the rail look rather than crashing the chunk build.
static func for_surface(surface: String) -> StandardMaterial3D:
	match surface:
		"rail_left", "rail_right":
			return rail_material()
		"ballast":
			return ballast_material()
		_:
			push_warning("TrackMaterials: unknown surface '%s', falling back to rail material" % surface)
			return rail_material()


static func _build(res_name: String, albedo: Color, metallic: float, roughness: float) -> StandardMaterial3D:
	var mat := StandardMaterial3D.new()
	mat.resource_name = res_name
	mat.albedo_color = albedo
	mat.metallic = metallic
	mat.roughness = roughness
	return mat
