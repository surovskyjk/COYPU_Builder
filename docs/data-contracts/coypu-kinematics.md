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
