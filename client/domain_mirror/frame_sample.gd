class_name FrameSample
extends RefCounted
## One row of an [AlignmentTable] evaluated at a station — exact when the station lands on a baked row,
## linearly/[method Quaternion.slerp] interpolated between the two bracketing rows otherwise. Built fresh
## by [method AlignmentTable.sample]; the hot path ([method AlignmentTable.position_at]) skips this
## allocation entirely.

var station: float = 0.0
var position: Vector3 = Vector3.ZERO      # Godot axes, relative to the project base point (ADR 0004)
var rotation: Quaternion = Quaternion.IDENTITY
var roll: float = 0.0
var pitch: float = 0.0
var cant_mm: float = 0.0
var curvature: float = 0.0
var gradient: float = 0.0
var elevation: float = 0.0
var segment_index: int = 0
