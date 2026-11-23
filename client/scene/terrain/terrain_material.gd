class_name TerrainMaterial
extends ShaderMaterial
## Draped imagery for one terrain display tile (T-135, Part B): the tile's JPEG as albedo, its alpha blob (when
## some pixel is transparent) as a discard mask, and its source-index map as the source-boundary overlay. The
## shader is written here, not in a `.gdshader` file, and compiled once for every tile.
##
## Decoding is a worker job ([method decode_job]): `Image.load_jpg_from_buffer` runs off the main thread, and
## the textures are made from the decoded images on the main thread by [method for_images]. Nothing here touches
## the scene tree.
##
## Uniforms:
## - `albedo_tex`: the 512 x 512 imagery, row 0 north, so UV (0, 0) is the north-west corner. The JPEG is
##   decoded as stored, no colour conversion.
## - `alpha_tex` and `has_alpha`: the R8 alpha image, set only when the tile has transparent pixels. Texels under
##   0.5 are discarded, not blended, so an unimaged strip shows the scene behind it.
## - `source_tex`: the terrain tile's 129 x 129 source index (R8, 255 = none), nearest filtered. Sample `(uv * 128 +
##   0.5) / 129` puts a vertex at the centre of its texel, so the boundaries fall between height samples.
## - `show_boundaries`, `boundary_tint` (0.10) and `line_colour`: the overlay. Where a source index differs from
##   the one a pixel away (one screen pixel either side, from the derivatives), the pixel takes `line_colour`,
##   which gives a line about two pixels wide. Elsewhere each source's tint is mixed in at `boundary_tint`.
##
## Overlay colours: source index `i` is the hue `fract(i * 0.618034)` (the golden ratio, so neighbouring indices
## differ widely) at saturation 0.55 and value 0.9, in the same HSV-to-RGB formula as [method overlay_colour].
## The line is [constant LINE_COLOUR] (white). Index 255 ("none") is never tinted.

const IMAGE_SIZE := 512
const SOURCE_SIZE := 129
const BOUNDARY_TINT := 0.10
const LINE_COLOUR := Color(1.0, 1.0, 1.0)
const HUE_STEP := 0.618034
const OVERLAY_SATURATION := 0.55
const OVERLAY_VALUE := 0.9
const NONE_INDEX := 255
## The placeholder colour of the neutral tile material (see TerrainTile.neutral_material), for a tile whose
## boundaries show without imagery.
const NEUTRAL_COLOUR := Color(0.46, 0.52, 0.44)

const SHADER_CODE := """
shader_type spatial;

uniform sampler2D albedo_tex : source_color, filter_linear_mipmap, repeat_disable;
uniform sampler2D alpha_tex : filter_linear, repeat_disable;
uniform bool has_alpha = false;
uniform sampler2D source_tex : filter_nearest, repeat_disable;
uniform bool show_boundaries = false;
uniform float boundary_tint = 0.1;
uniform vec3 line_colour = vec3(1.0, 1.0, 1.0);

vec3 hsv_to_rgb(vec3 c) {
	vec4 k = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
	vec3 p = abs(fract(c.xxx + k.xyz) * 6.0 - k.www);
	return c.z * mix(k.xxx, clamp(p - k.xxx, 0.0, 1.0), c.y);
}

float index_at(vec2 uv) {
	return floor(texture(source_tex, (uv * 128.0 + 0.5) / 129.0).r * 255.0 + 0.5);
}

void fragment() {
	// The derivatives come before the discard: after it, in non-uniform flow, they are undefined for the pixels that stay.
	vec2 dx = dFdx(UV);
	vec2 dy = dFdy(UV);
	float alpha = texture(alpha_tex, UV).r;
	if (has_alpha && alpha < 0.5) {
		discard;
	}
	vec3 colour = texture(albedo_tex, UV).rgb;
	if (show_boundaries) {
		float here = index_at(UV);
		bool edge = index_at(UV + dx) != here || index_at(UV - dx) != here
			|| index_at(UV + dy) != here || index_at(UV - dy) != here;
		if (here < 254.5) {
			vec3 tint = hsv_to_rgb(vec3(fract(here * 0.618034), 0.55, 0.9));
			colour = mix(colour, tint, boundary_tint);
		}
		if (edge) {
			colour = line_colour;
		}
	}
	ALBEDO = colour;
	ROUGHNESS = 1.0;
}
"""

static var _shader: Shader


## The one shader resource every tile material shares.
static func shared_shader() -> Shader:
	if _shader == null:
		_shader = Shader.new()
		_shader.code = SHADER_CODE
	return _shader


## The overlay colour of a source index, as the shader draws it (255 has none: see [constant NONE_INDEX]).
static func overlay_colour(index: int) -> Color:
	return Color.from_hsv(fposmod(index * HUE_STEP, 1.0), OVERLAY_SATURATION, OVERLAY_VALUE)


## Worker entry point (`WorkerThreadPool`). `job` carries `jpeg` (PackedByteArray), `alpha` (the alpha blob, empty
## when the tile is opaque) and `source_index` (the 129 x 129 blob) in; it receives `albedo` (Image), `alpha_image`
## (Image or null), `source_image` (Image) and `decode_error` (a sentence, or "" when all decoded).
static func decode_job(job: Dictionary) -> void:
	job["decode_error"] = ""
	job["alpha_image"] = null
	var jpeg: PackedByteArray = job["jpeg"]
	# The start-of-image marker is checked first: the engine decoder logs its own error for any other bytes.
	if jpeg.size() < 4 or jpeg[0] != 0xFF or jpeg[1] != 0xD8:
		job["decode_error"] = "the imagery tile is not a JPEG"
		return
	var albedo := Image.new()
	if albedo.load_jpg_from_buffer(jpeg) != OK or albedo.get_width() != IMAGE_SIZE or albedo.get_height() != IMAGE_SIZE:
		job["decode_error"] = "the imagery tile is not a %d x %d JPEG" % [IMAGE_SIZE, IMAGE_SIZE]
		return
	albedo.generate_mipmaps()
	var alpha_bytes: PackedByteArray = job["alpha"]
	if not alpha_bytes.is_empty():
		if alpha_bytes.size() != IMAGE_SIZE * IMAGE_SIZE:
			job["decode_error"] = "the imagery alpha blob has %d bytes, not %d" % [alpha_bytes.size(), IMAGE_SIZE * IMAGE_SIZE]
			return
		job["alpha_image"] = Image.create_from_data(IMAGE_SIZE, IMAGE_SIZE, false, Image.FORMAT_R8, alpha_bytes)
	var index_bytes: PackedByteArray = job["source_index"]
	if index_bytes.size() != SOURCE_SIZE * SOURCE_SIZE:
		job["decode_error"] = "the source index blob has %d bytes, not %d" % [index_bytes.size(), SOURCE_SIZE * SOURCE_SIZE]
		return
	job["albedo"] = albedo
	job["source_image"] = _source_image(index_bytes)


## The source-index `Image` of a tile: the 129 x 129 blob, flipped once here. The heights are row 0 south and the image
## row 0 north, so the flip makes the map share the albedo's UV. Both the draped and the boundary-only material use it.
static func _source_image(index_bytes: PackedByteArray) -> Image:
	var source := Image.create_from_data(SOURCE_SIZE, SOURCE_SIZE, false, Image.FORMAT_R8, index_bytes)
	source.flip_y()
	return source


## Main thread only: the material for decoded images. `alpha` is null for an opaque tile, which leaves the
## `alpha_tex` parameter unset.
static func for_images(albedo: Image, alpha: Image, source: Image) -> TerrainMaterial:
	var material := TerrainMaterial.new()
	material.shader = shared_shader()
	material.set_shader_parameter("albedo_tex", ImageTexture.create_from_image(albedo))
	material.set_shader_parameter("source_tex", ImageTexture.create_from_image(source))
	material.set_shader_parameter("has_alpha", alpha != null)
	if alpha != null:
		material.set_shader_parameter("alpha_tex", ImageTexture.create_from_image(alpha))
	material.set_shader_parameter("boundary_tint", BOUNDARY_TINT)
	material.set_shader_parameter("line_colour", Vector3(LINE_COLOUR.r, LINE_COLOUR.g, LINE_COLOUR.b))
	material.set_shader_parameter("show_boundaries", false)
	return material


## Main thread only: a material with no imagery, for a tile whose source boundaries are drawn while no imagery is
## shown. Its albedo is one flat neutral texel (the placeholder's colour), and its source map is the tile's own, made
## by [method _source_image] as in [method decode_job], with the overlay on.
static func for_boundaries(index_bytes: PackedByteArray) -> TerrainMaterial:
	var source := _source_image(index_bytes)
	var neutral := Image.create_empty(1, 1, false, Image.FORMAT_RGB8)
	neutral.fill(NEUTRAL_COLOUR)
	var material := for_images(neutral, null, source)
	material.set_boundaries(true)
	return material


func set_boundaries(on: bool) -> void:
	set_shader_parameter("show_boundaries", on)


func boundaries_on() -> bool:
	return bool(get_shader_parameter("show_boundaries"))


func has_alpha() -> bool:
	return bool(get_shader_parameter("has_alpha"))
