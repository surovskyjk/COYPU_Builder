# COYPU kinematics, stops, vehicles and the `.coypu` archive

## Kinematics CSV — two contracts

**Batch / strict SI** (`batch_export.py: buildKinematicsCsv`, files `variants/<v>/data/kinematics_V<i>.csv`):

```
stationM,timeS,speedMs,accelMs2,forceTracKN,forceBrakeKN,forceResKN
```
1 m steps from the alignment start, raw floats, station ascending (descending for reversed runs).

**GUI vehicle report** (`gui.py: exportVehicleReport`), header localised through `translations/*.json`:

| EN | CZ | DE |
|---|---|---|
| `stationing [km]` | `staničení [km]` | `Streckenkilometer [km]` |
| `Time [s]` | `Čas [s]` | `Zeit [s]` |
| `Speed [km/h]` | `Rychlost [km/h]` | `Geschwindigkeit [km/h]` |
| `Accel [m/s2]` (not localised) | | |
| `Tractive Force [kN]` | `Trakční síla [kN]` | `Zugkraft [kN]` |
| `Braking Force [kN]` | `Brzdná síla [kN]` | `Bremskraft [kN]` |
| `Resistance [kN]` | `Jízdní odpor [kN]` | `Widerstand [kN]` |

Station in km (3 decimals), speed in km/h (1 decimal), time may be empty when no time series exists.

Semantics (from `vehicle_engine.py`): `tS[i] = tS[i−1] + Δs/v_avg + dwell[i]` — a stop appears as a time jump
at (almost) constant station; a `kinematicsDwellTimesS_i` array exists in projects, the CSV carries only the
jump. Speed is capped at 0.5 m/s for the time integral near stops. Builder normalises both dialects into one
`KinematicsRun` (station m ascending, time s, speed m/s, accel, forces, dwell s, direction).

## Stops CSV

`Station,Dwell Time,Name` — station in km, dwell in s (default 30). Written by Feeder and COYPU alike.

## Vehicle CSV / catalogue

Section rows: `Meta` (`vehicleName, maxSpeedKmh, massTonnes, lengthM, brakeDecelMs2, maxTractiveForceKN,
rotMassFactor`), `Param` (name, rot. mass factor, mass t, length m), `Res` (Davis A, B, C), `Trac` (bands
V_bottom, V_top, b0, b1, b2). Builder's 3D specs (`shared/catalogue/vehicles/*.json`) are keyed by the same
`vehicleName` and add car composition, bogie pivot distance, wheelbase and body dimensions.

## Builder normalisation

`domain/kinematics/run.py: normalise_run` turns either source into one `KinematicsRun`: station in absolute
metres (LandXML `staStart` basis, source row order — a reversed run is never flipped, its `direction` field
is set to `REVERSE` instead), time in seconds ascending from `0.0`, speed as a non-negative magnitude, and
optional force arrays that are `None` (not zero) when the source didn't carry them. A non-monotonic time
column is stable-sorted with a warning rather than rejected; a missing time column is rejected outright
rather than fabricated.

Stops: when the source carries `kinematicsDwellTimesS_i` (the `.coypu` archive), every sample with a
positive dwell is kept as a `Stop` directly — this is what the reader in `io/coypu/archive.py` uses. Neither
CSV dialect has a dwell column, so `read_kinematics_csv` instead detects a dwell as a time delta that exceeds
what the `CREEP_SPEED_MS` (0.5 m/s) floor could explain for that step's station delta, by more than
`DWELL_THRESHOLD_S_DEFAULT` (3.0 s), while that station delta itself stays under `STATION_EPSILON_M_DEFAULT`
(1.5 m). Both CSV dialects in practice sample on COYPU's fixed 1 m simulation grid (`vehicle_engine.py`), so
the station delta at every step — dwelling or not — is exactly one grid step; the real discriminator is the
time delta once the creep-speed floor's own travel time is subtracted out. A `dwell_threshold_s` of 1.0 s (a
plausible-looking default) would misfire on ordinary low-speed rows on that same grid, whose delta can
legitimately reach `grid_step / CREEP_SPEED_MS` (2.0 s for a 1 m grid); 3.0 s was chosen with a margin above
that. A detected or kept stop's name is filled in from a supplied reference (e.g. `read_stops_csv`) when one
lands within `STOP_NAME_MATCH_TOLERANCE_M` (5 m).

`domain/kinematics/table.py: bake_run_table` resamples a `KinematicsRun` onto a uniform time grid
(`RunTable`) by linearly interpolating station, speed, accel and the forces against `time_s`, after
collapsing any duplicate timestamps to their last sample. The grid step is the requested `dt` (default
0.05 s) adjusted very slightly — `dt_actual = duration / round(duration / dt)` — so the whole table stays
perfectly uniform (`i = t / dt_actual` needs no search, ever, not just for all-but-the-last row) and the
final row still lands exactly on the source's last timestamp. A dwell falls out of this scheme automatically
as a flat run of stations at ~0 speed, with no special-casing and no spike, because linear interpolation
between two source samples can't overshoot them.

## `.coypu` archive

ZIP with `project.json` (+ `assets/NNN_<file>.xml` raw imports). numpy arrays are encoded as
`{"__ndarray__": true, "dtype": "...", "values": [...]}`. Keys Builder uses:

- `alignmentsData.landXml`: `stationHorizontal` (km, start/end pairs per element), `geometryType`,
  `keyStations/keyX/keyY/keyTypes`, `alignmentCoordsOriginal` (raw token polylines per element),
  `denseAlignment` (km, lat, lon), `stationVertical` (km), `elevation`, `stationCant` (km), `cant`
- `calculationCache.landXmlDerived`: `stationCantPossible`, `cantPossible`, `stationSpeed{100,130,150,K,TTP}`,
  `speedLimits{…}`, `cDef{…}`
- `calculationCache.dataStorage`: `kinematicsStationM_i`, `kinematicsTimeS_i`, `kinematicsSpeedM_i`,
  `kinematicsAcceleration_i`, `kinematicsForce{Traction,Braking,Resistance}KN_i`, `kinematicsDwellTimesS_i`,
  `num_vehicles`
- `vehicleConfiguration.settingsData.vehicles[i].trainParam = [[name, rotMass, mass_t, length_m]]`
- `stopsData.trainStops = [[km, dwell_s, name], …]`, `projectMetadata.epsgCode`

Reader: `backend/src/coypu_builder/io/coypu/archive.py`.

### Archive format versions

COYPU saves format version 2 and still loads version 1. Builder reads both (`SUPPORTED_FORMAT_VERSIONS = (1, 2)`).
A version that is missing, not an integer, a boolean or below 1 is refused as "not a COYPU project file", which
`import.coypu` reports as `E_BAD_PARAMS`. A higher version is read as far as Builder knows, with the warning
"written by a newer COYPU (format N); only the parts this version knows are read".

Only three kinds of entry are read. Each one is checked against its size in the ZIP directory before anything is
read, inflated in chunks so that a header which understates its size cannot inflate past the limit, and a larger
entry is refused:

| Entry | Limit |
|---|---|
| `project.json` | 256 MB |
| `karto/dataset.json` | 64 MB |
| `assets/*.xml`, all together | 256 MB |

Format 2 keys Builder reads, in addition to the version 1 keys above:

- `projectMetadata.lineNumber`, `lineName`, `lineCategory`. The category is `""`, `regional` or `national`; any other
  value is kept and warned about. `ImportCoypuResult.line` is `null` when all three are empty.
- `stopsData.stopRecords`: `{stopId, stopName, stationingKm, dwellTimeS, municipalityName, population, stopInOs,
  stopInSp, stopInEx, stopInSpIsManual, stopInExIsManual}` per stop. A record without a usable `stationingKm` is
  skipped with a warning. A missing `stopIn*` flag defaults as COYPU does: `stopInOs` true, the others false.
- `stopsData.activeStopPattern`, default `Os`. Any other value is replaced by `Os` with a warning.
- `vehicleConfiguration.settingsData.vehicles[i].directionLabels` `{forward, reverse}`.
- `vehicleConfiguration.settingsData.runReversed`, and the per-vehicle copies that version 1 already carries.
  `run_reversed` reads only `settingsData.runReversed`; the authoritative direction of a run is
  `KinematicsRun.direction`, from its station axis.
- `karto/dataset.json` with `schemaVersion` 1. It is parsed with `NaN` and the other non-finite constants rejected.
  Invalid JSON, another schema version or a non-object gives `karto = None` and a warning. Nothing on the wire uses
  it yet, and Builder never writes it.

Stops of a format 2 archive that has records: `CoypuProject.stop_records` holds every record. The `import.coypu`
result's `stops` lists every record with its `patterns`, and each run's stops are the records of the active pattern
in station order (`CoypuProject.pattern_stops`). With no records, both come from `stopsData.trainStops` and the runs
keep their dwell-derived stops, as in version 1. Format 2 still writes `stopsData.trainStops`, which holds the
active pattern's records (`stops_model.py:291-298`). Run stops from stop records are in ascending station order,
also for a reversed run.

#### Chainage map of an optimised alignment

COYPU saves the optimisation map as `chainageMapBaselineKm` and `chainageMapActiveKm`, in
`calculationCache.landXmlDerived`. Builder also checks `alignmentsData.landXml`. The embedded `assets/*.xml` is the
original geometry, so Builder places every kinematics run on the baseline chainage:

- No map, or a map whose two arrays agree within 1e-6 km everywhere (the identity): nothing changes, and no
  warning is written. Version 1 archives have no map.
- A map whose active chainage is strictly increasing: each run's stations are converted before normalisation,
  `baseline_m = np.interp(station_m / 1000, active_km, baseline_km) * 1000`. The archive gets one warning, which
  names the largest shift `d`, the largest |active − baseline| over the map, in metres.
  Beyond the map's ends the shift of the nearest end is kept (unit slope), so stations never collapse onto one
  value. Inside the map the mapping is linear between the map's nodes, so the mapped dS/dt can differ slightly
  from the stored speed: speed, acceleration and forces are not rescaled.
- A map whose active chainage is not strictly increasing is not applied. The archive gets the warning
  `chainage map is not monotonic; the run is left on COYPU's active chainage`.
- Two arrays of different lengths are not applied either. The archive gets the warning
  `chainage map arrays differ in length; the run is left on COYPU's active chainage`.
- Stop records and `trainStops` are not converted on this path. They were entered on the imported chainage already.
  An optimised archive whose geometry passes the gates of "Optimised alignments (T-129)" is imported from its
  element arrays instead, and then its runs stay on the active chainage and its stops are mapped onto it.

#### Survey of the COYPU source (format 2)

These answers come from the COYPU source, a read-only reference outside this repository. Line numbers are in the
file named before each colon.

1. **Which stops the stored kinematics used.** The records of the active pattern, through the legacy list. The
   sync `stops_model.py:291-298` (`syncTrainStops`) writes `settingsData["trainStops"]` from the active pattern, and
   `stops_model.py:302-314` (`ensureStopSettings`) ends by calling it. Load calls `ensureStopSettings` at
   `project_file.py:154` and `project_file.py:444`. The stops dialog and the ribbon pattern selector call the sync at
   `gui.py:3834` and `gui.py:3864`. So yes, `ensureStopSettings` keeps `trainStops` in step with the active pattern.
   The engine stops on `settingsData.trainStops` (`vehicle_engine.py:378`), and the simulation gets the projected
   copy (`gui.py:1427-1438`, `simulation_runner.py:61`). The stored copy is `calculationCache.dataStorage.simulatedTrainStops`
   (`gui.py:5306`, saved with the rest of `dataStorage` by `project_file.py:288-296`). The Kralupy fixture agrees: its
   dwell samples sit exactly at the six `trainStops` stations. Two caveats. The engine also gives every zero speed-limit
   region the default dwell (`vehicle_engine.py:260-275`), and that is not a stop record. A pattern change made while a
   simulation is running does not re-run it (`gui.py:3851-3854`), so the stored kinematics can belong to the previous
   pattern; `simulatedTrainStops` still records the stops actually used.

2. **Run direction.** Yes. The shared direction is `settingsData.runReversed`, copied onto every vehicle
   (`gui.py:5209-5213`, `simulation_runner.py:45-50`). The flat kinematics arrays hold the viewed direction
   (`gui.py:5215-5216`, `gui.py:5365-5380`). The engine builds the station axis descending when a vehicle is reversed
   (`vehicle_engine.py:127`, `vehicle_engine.py:151`, `np.arange(sEnd, sStart - ds, -ds)`). So a reversed archive has a
   descending station array. The Kralupy fixture has both vehicles not reversed, with ascending stations.

3. **Per-direction results.** Not in the archive as kinematics. `simulationResultsByProfile` holds both directions of
   every tier in memory only (`gui.py:295`, `gui.py:5304`), and the payload has no key for it
   (`project_file.py:205-216`). Only the active tier's viewed direction is kept, as flat keys under
   `calculationCache.dataStorage`: `kinematicsStationM_i`, `kinematicsSpeedM_i`, `kinematicsTimeS_i`,
   `kinematicsAcceleration_i`, `kinematicsForce{Traction,Braking,Resistance}KN_i`, `kinematicsDwellTimesS_i` and
   `kinematicsWarning_i` (`gui.py:5365-5385`), plus the tier in `activeProfileKey` (`gui.py:5429`). The other direction
   is simulated again on demand (`gui.py:5224-5249`). Both directions are kept for travel times: the scenario matrix in
   `dataStorage["scenarioMatrix"]` (`gui.py:3982`, cells carry `direction`, `scenario_matrix.py:191`), and the Karto
   `travelTimes` section (`karto_export.py:335-353`). That is travel-time data, not kinematics.

4. **Optimised alignment.** The embedded `assets/*.xml` is the **baseline**. Each source entry's `rawText` is written
   as its asset (`project_file.py:254`, `project_file.py:328-333`), and it is captured at import (`gui.py:2377`,
   `gui.py:2702`). An optimisation never adds or edits a source entry. It writes only the live `LandXML` keys
   (`gui.py:4936-4958`) and works on an isolated copy (`optimization_runner.py:107`). The chainage map is
   `chainageMapBaselineKm` and `chainageMapActiveKm` (`geometry_engine.py:2622-2624`). It is saved under
   `calculationCache.landXmlDerived` (`project_file.py:55-60`) and projected piecewise linearly with `np.interp`
   (`geometry_engine.py:1344-1354`).

   They **can disagree**. A simulation run after an optimisation reads the optimised `stationHorizontal`
   (`gui.py:4938-4946`, `vehicle_engine.py:380-382`). So its stored stations and `simulatedTrainStops` are on the
   active chainage, while the embedded XML is on the baseline chainage. Stop records are entered on the imported
   chainage and projected only at simulation time (`gui.py:1426-1437`). A Builder run whose stops come from the
   records therefore sits on the baseline chainage, while its station axis sits on the active one, whenever the map
   is not the identity. The optimisation clears every `kinematics*` key (`gui.py:4966`, `gui.py:4866`), so stored arrays always come
   from a run made after the last optimisation and are on the active chainage whenever the map is not the identity.
   Builder applies the map, as the rule under
   "Chainage map of an optimised alignment" says.

#### Optimised alignments (T-129)

COYPU's optimiser re-geometrises the alignment and re-stations it onto the active chainage. The archive keeps the
optimised element arrays under `alignmentsData.landXml`, so Builder can rebuild the optimised alignment from them.
It does so only when the chainage map applies (above) and every gate below passes. Otherwise Builder imports the
embedded original geometry and maps the runs back, as the T-128 rule says. The code is `io/coypu/geometry.py`
(`optimised_alignment`, `elements_from_arrays`).

**Rebuild.**

- Element `i` has kind `geometryType[2i]` (`Line`, `Spiral` or `Curve`), and takes entry `i` of the arrays of its
  kind, counted per kind from zero, as COYPU's element table does (`landxml_exporter.py:75-91`).
- Its points are the raw tokens of the `X` and `Y` arrays, in the order the embedded file writes them (survey 1).
  Every point therefore goes through the same CRS rule as the embedded file.
- Its station start is `stationHorizontal[2i] × 1000` m. Its length is a spiral's `spiralLength`. A line and a curve
  have none, so their length follows from the chord or the swept arc, as in the embedded file and in COYPU's dense
  sampling. The station span `stationHorizontal[2i+1] − stationHorizontal[2i]` is not used: on the Kralupy fixture
  it is up to 0.36 mm away from a curve's arc length, which moves the rebuilt positions by that much.
- Name, EPSG, application and dialect come from the embedded file. The alignment starts at `stationHorizontal[0]`.
- Attributes are the ones the reader consumes: a spiral's `radiusStart`, `radiusEnd` (written `INF` for infinity),
  `rot` and `spiType`, and a curve's `radius` and `rot`.

**Gates.** The first gate that fails names the fallback.

1. *One alignment.* The archive embeds exactly one `<Alignment>` (`assets/*.xml` together). COYPU merges several
   LandXML sources into one parsed geometry, which the archive cannot separate again, so an archive that embeds several
   alignments never uses the optimised geometry. Failure: "the archive embeds several alignments".
2. *Arrays.* Each kind's point arrays, `…StationStart`, and its attribute arrays (`spiralLength`, `spiralRadiusStart`,
   `spiralRadiusEnd`, `spiralRot`, `spiralType`, `curveRadius`, `curveRot`) exist with one entry per element of that
   kind, and `geometryType` lists each element's kind twice. Failure: "the per-type element arrays are missing or do not
   match the element count".
3. *Continuity.* The end point of element `i` lies within 1 mm of the start point of element `i+1`, and the station end
   of `i` within 1 mm of the station start of `i+1`. Failure: "the elements do not chain end to start within 1 mm".
4. *Dense axis.* Each point of `denseAlignment` is taken from (station km, latitude, longitude) back to the project
   CRS, and must lie within 20 mm of the rebuilt axis. The distance is measured to the axis, not to the rebuilt position
   at the point's own station, because the station column is not reliable for curves (survey 2). The axis is sampled
   every 0.1 m and the distance is to the segments that touch the nearest samples. Failure: "the rebuilt axis does not
   match COYPU's dense axis within 20 mm".
5. *Chainage map.* The baseline stations of the map are strictly increasing. Failure: "the chainage map is not strictly
   increasing on the baseline".
6. *Conversion.* An `ArithmeticError`, `IndexError`, `KeyError`, `TypeError` or `ValueError` raised while the elements
   are built or converted to an axis is a failure, not an exception. Failure: "the optimised elements cannot be
   converted to an axis". Non-finite stations fail gate 2, and non-finite dense points fail gate 4.
7. *Coordinate system.* The archive's EPSG code must be known to pyproj. Failure: "the archive names no coordinate
   system, or one that is not known".

The 20 mm limit is chosen for the round trip through pyproj, which alone gives 1.8 mm on the Kralupy fixture, and a
wrongly built element is metres off. The largest deviation on the Kralupy fixture is 1.84 mm; the rebuilt positions
themselves match the embedded file to 1e-9 m at 1 m sampling.

A failing gate gives `optimised geometry not used: <gate>; the run was mapped back to the original geometry`, and the
T-128 map-back warning follows it. The one-alignment gate uses the same format.

**When the optimised geometry is used.**

- The profile is the original file's. Each PVI station is mapped baseline to active with `np.interp`, and beyond the
  map's ends the nearest end's shift is kept. Elevations, curve lengths and radii are unchanged.
- The cant is COYPU's calculation on the optimised geometry: `calculationCache.landXmlDerived.stationCantPossible`
  and `cantPossible` (survey 3). They are already on the active chainage, so they are not mapped. The gauge and the
  rotation point come from the embedded file. If the archive holds no calculation, the original cant is mapped instead.
- Each run keeps its stored stations (no mapping back).
- The stops are the stop records of the active pattern, or the legacy `trainStops`, mapped baseline to active with the
  same `np.interp`. `calculationCache.dataStorage.simulatedTrainStops`, when present, must match the mapped scheduled
  stops within 1 mm. Otherwise the warning is `the stops Builder maps onto the optimised chainage differ from the stops
  COYPU simulated by up to {d} mm`, or, when the counts differ, a count warning.
- The archive gets the notice
  `COYPU optimised this alignment: Builder uses the optimised geometry from the archive ({n} elements; largest deviation
  from COYPU's dense axis {d:.1f} mm), with the original file's profile re-stationed onto it and {cant}`, where `{cant}`
  is `COYPU's calculated cant` when the archive stores `stationCantPossible` and `cantPossible`, and
  `the original cant re-stationed onto it` when it stores none.
- The map-back warning of T-128 is not written.

**Archives that are not optimised** (no applicable map, or the identity) are read exactly as T-128 reads them.

**Survey of the COYPU source (T-129).** Read-only; line numbers are in the file named before each colon.

1. **Token order.** The arrays hold the raw tokens, with no swap and no sign. `readfile.py:176` (Line), `:197`
   (Spiral) and `:224` (Curve) store the first token of each `Start`, `End`, `PI` and `Center` text as X, the second
   as Y. `landxml_exporter.py:264-268` writes X then Y. The Křovák sign is applied later: `readfile.py:30-31` takes
   E = −Y and N = −X, so the first token is the northing value, as the LandXML native order says. The fixture confirms
   it: for all 65 elements every X entry equals the first token of the embedded text, and every Y entry the second,
   with a difference of 0.0 (the swapped pairing differs by about 292 km).
2. **`denseAlignment`.** Each entry is `(station km, latitude, longitude)` in WGS 84 degrees. `readfile.py:687-727`
   builds it (`dense_points.append` at `:708`, sorted by station at `:726`) with the transform of `readfile.py:26-31`.
   The fixture's first entry `(0.0, 50.26208, 14.52270)` is the transform of its first key point. The station column is
   not reliable for curves: all samples of each of the 16 curves carry the curve's start station (784 of the 2434
   points). Cause: COYPU builds before 2026-09-25 set a curve's end station by searching `stationHorizontal` for the
   curve's start station and taking the next entry (`readfile.py` at commit `9ba224d`, 2026-05-27, the curve
   sampling). That start station also appears as the previous element's end, so the end station equals the start
   station and all samples share one station (16 of 16 curves on the fixture, which was saved on 2026-08-23). Commit
   `7019d7f` (2026-09-25, on COYPU's `main`) fixed it with `curveEndStation`, which addresses the element stream, and
   the current source samples each element over its own length. Archives saved by older builds still carry the defect.
   The sample positions lie on the axis, which is why the gate measures the distance to it.
3. **Cant.** The simulation of an optimised alignment used the calculated `cantPossible`. The design calculation
   computes `cantPossible` on the new geometry (`geometry_engine.py:81`, `:196`, and `runCalculationLoop` at `:30-40`),
   and the speeds the simulation reads come from it (`vehicle_engine.py:34`, `:58`, default `stationSpeed150` and
   `speedLimits150`). The imported cant is read only by the as-built branch (`geometry_engine.py:617-619`), which
   `optimization_runner.py:181-184` and `gui.py:4691-4698` select, and which `gui.py:5171-5186` disables on an optimised
   alignment. The export agrees: `landxml_exporter.py:389-398` writes the calculated cant when it exists. The fixture
   shows the difference: its imported cant is zero everywhere, while `cantPossible` is non-zero at 64 of 130 stations
   and reaches ±100 mm. Builder therefore shows the calculated cant, the Contract's alternative. `cantPossible` is the
   cant the speeds were computed from in both modes: in as-built mode it holds the interpolated imported cant
   (`geometry_engine.py:619`).
4. **Spiral and curve parameters.** The optimiser does not change `spiralRot` or `curveRot`: the returned element dict
   carries each element's `rot` unchanged (`geometry_engine.py:2661`, `:2675`), and it returns no `spiralType`,
   `spiralConst` or `curveType` (`geometry_engine.py:2628-2695`). `promoteOptimizedElements` (`geometry_engine.py:1050-1056`)
   copies only the keys the optimiser returns, so the imported `spiralType` stays. `spiralConst` is read from the
   attribute name `consant` (`readfile.py:412`), which the file does not use, so the array is NaN in the fixture, and
   `curveType` (`readfile.py:427`) is not used by the reader. Neither is needed to rebuild an element.
