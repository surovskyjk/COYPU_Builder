# LandXML 1.2 dialects in the COYPU ecosystem

All three producers write `Alignments/Alignment/CoordGeom` with `Line`, `Spiral`, `Curve` children carrying
point elements (`Start`, `PI`, `Center`, `End`) as text `"t1 t2"`, a `Profile/ProfAlign` and a `Cant` block.
Builder's reader (`backend/src/coypu_builder/io/landxml`) handles every dialect through one code path plus a
few detected parameters.

| | Third-party rail CAD export (`Application name="Rail"`) | COYPU Feeder 1.1 | COYPU 2.0 |
|---|---|---|---|
| `Application name` | `Rail` | `COYPU Feeder` | `COYPU` |
| `CoordinateSystem` | absent | `epsgCode="5514"` | `desc="EPSG:5514"`, name/datums |
| Decimals | 6 | 6 | 4 |
| `Line` | Start/End | Start/End | + `length` attr |
| `Spiral` | `length radiusStart radiusEnd rot spiType constant` + PI | same (PI approximate, chord relation) | same, no `constant` |
| `Curve` | `radius rot crvType` + Center | same | + `length` attr |
| Vertical | `PVI`, `CircCurve length radius(signed)` | `PVI` + `ParaCurve length` at the same station | `PVI`, `CircCurve length radius` |
| Cant | `gauge="1.435" superelevationBase="1.5" rotationPoint="insideRail" stationType="centreline"` | same, `appliedCant="0"` placeholders | `gauge="1435" rotationPoint="insideRail" equilibriumConstant speed`; `appliedCant` = |mm|, per-station `speed` |

## Coordinate tokens

- The specification order is `northing easting`.
- Czech Křovák practice writes the positive engineering values X (southing) and Y (westing). With a Křovák
  project CRS (EPSG 5514/2065/5513/8353): positive tokens ⇒ `E = −t2, N = −t1`; negative tokens are true
  EPSG:5514 ⇒ `E = t2, N = t1`. (Feeder writes `N E` and strips signs when `force_positive`; COYPU reads
  `E = −t2, N = −t1` unconditionally.)
- Other CRSs: spec order `(N, E)` by default, overridable with `coordinate_order="EN"`. COYPU itself reads
  non-Křovák files as `(E, N)`, so files that round-trip through COYPU in UTM may need the override.

## Conversion rules (Builder)

- Positions come from each element's own `Start`; only headings are chained. Lines and curves take exact
  headings from their geometry; a spiral inherits the previous element's end heading when it starts on that
  element's end (within 5 cm), otherwise its Start→PI tangent.
- Element length: `length` attribute > chord (Line) / swept angle in `rot` direction (Curve, reflex allowed) /
  `length` (Spiral). Stations follow `staStart` attributes; the alignment's `staStart` is the absolute origin.
- Vertical curves: `ParaCurve` and `CircCurve` are both evaluated as the symmetric parabola; a `PVI` and a curve
  at the same station collapse to one PVI with a curve.
- Cant: `appliedCant` as |mm|; gauge/superelevation base auto-detected as metres when < 10; `rotationPoint`
  → `RotationPivot`.
- The conversion report records max end-point closure, max heading jump and spiral PI mismatch; the Kralupy
  fixture closes to < 2 mm and matches COYPU's dense polyline within 2 cm (COYPU discretises from the
  approximate PI).
