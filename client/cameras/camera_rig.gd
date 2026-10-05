class_name CameraRig
extends Node3D
## Shared base for [OrbitCamera]/[WaysideCamera]/[CabCamera] (T-124): the one `Camera3D` each rig owns
## plus its near/far planes, and the one damping helper every mode's own follow/look-at logic uses. ADR
## 0004: every position handled by a rig is already base-point-relative Godot space -- nothing below (or
## in any subclass) ever calls `Origin.to_godot`; `Origin.from_godot` would only be legitimate for a
## display readout, and none of these rigs performs one. ADR 0007: [method update] is called once per
## frame by [CameraManager] and does local arithmetic only -- no RPC, no `await`, no per-frame allocation.
##
## The `Camera3D` child is built in [method _init], not [method Node3D._ready] -- a rig's transform math
## must be exercisable from a unit test without adding it to a live [SceneTree] first (gdUnit4's
## `client/tests/unit/test_camera_rigs.gd`), and [Node3D.global_transform] resolves through the parent
## chain regardless of tree membership, so nothing here needs the tree at all except the automatic
## `_process`/`_unhandled_input` callbacks [CameraManager] itself uses.
##
## Near/far: 0.05 m lets the cab rig sit close to the car's own geometry without near-clipping; 20000 m
## covers the Kralupy corridor's full ~18 km span at the orbit camera's farthest zoom -- the same `far`
## the temporary T-103 camera already used, so this task does not change what is visible end to end, only
## how the viewpoint is controlled.

const NEAR_M := 0.05
const FAR_M := 20000.0

var _camera: Camera3D


func _init() -> void:
	_camera = Camera3D.new()
	_camera.name = "Camera3D"
	_camera.near = NEAR_M
	_camera.far = FAR_M
	add_child(_camera)


func camera() -> Camera3D:
	return _camera


## `trainset`/`controller` may be null (no run wired yet, or none ever will be for a bare LandXML import)
## -- every rig must tolerate that quietly, since [CameraManager] binds cameras as soon as each dependency
## exists, in whichever order that happens to be.
func bind_subject(_trainset: TrainsetNode, _controller: PlaybackController) -> void:
	pass


func bind_corridor(_corridor: TrackCorridor, _table: AlignmentTable) -> void:
	pass


## Repositions instantly (no easing) -- an explicit "go to this station" command, unlike the continuous
## damped following [method update] performs every frame during playback.
func focus_station(_s: float) -> void:
	pass


## Recomputes this rig's `Camera3D` transform for one frame. Called explicitly by [CameraManager], not
## via this node's own automatic `_process` -- see [CameraManager]'s header for why.
func update(_delta: float) -> void:
	pass


## Mouse/keyboard input specific to this rig (only [OrbitCamera] does anything with it).
## [CameraManager] forwards whatever input its own mode-switch actions did not consume to the active rig
## alone.
func handle_input(_event: InputEvent) -> void:
	pass


## Called by [CameraManager] whenever this rig becomes the active mode, so a rig can snap its own damped
## state to its immediate target instead of visibly easing in from a stale value nobody drove for however
## many frames it spent inactive.
func activated() -> void:
	pass


## Exponential smoothing towards `target`, frame-rate independent: `alpha = 1 - exp(-delta / tau)`. `tau`
## is the time constant (seconds to close ~63% of the remaining gap) -- see each subclass for the value
## it chose and why.
static func damp_vector(current: Vector3, target: Vector3, delta: float, tau: float) -> Vector3:
	if tau <= 0.0:
		return target
	return current.lerp(target, 1.0 - exp(-delta / tau))
