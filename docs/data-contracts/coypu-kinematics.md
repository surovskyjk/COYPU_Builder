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
