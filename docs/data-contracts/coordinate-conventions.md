# Coordinate, station and frame conventions

| Quantity | Convention |
|---|---|
| Project CRS | any pyproj-resolvable definition; must be projected and metric for modeling |
| Domain position | float64 `(E, N, H)` in the project CRS |
| Station `s` | absolute metres along the horizontal alignment, LandXML `staStart` basis |
| Heading θ | radians from +E counter-clockwise; curvature κ > 0 = left turn (ccw) |
| LRS offset `y` | metres, positive to the **left** of travel in the track plane |
| LRS offset `z` | metres along the track-plane normal (`up`) |
| Gradient | dimensionless rise/run; pitch = atan(gradient), positive climbing |
| Cant D | |mm|; roll = −sign(κ)·asin(D / base), base = 1500 mm default (rail-head centres) |
| Rotation pivot | `LOW_RAIL` (default): profile elevation = inner rail head; plane centre raised (base/2)·sin|roll| |
| Base point | `(E0, N0, H0)` per project; local = domain − base |
| Godot axes | `x = E−E0`, `y = H−H0`, `z = −(N−N0)`; Basis columns (right, up, back), forward = −Z = tangent |
| Godot quaternion | `(x, y, z, w)` scalar-last |
| Tiles | mesh vertices relative to the tile centre; tile node positioned in world |

Implementation: `backend/src/coypu_builder/domain/crs.py`, `domain/lrs.py`; client `client/core/origin.gd`.
Golden vectors: `shared/golden/origin_mapping.json`, `shared/golden/frame_eval.json`.
