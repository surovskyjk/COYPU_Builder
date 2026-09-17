# T-111 — `KinematicsRun` normalisation and `RunTable` baking

**Milestone:** P1.M1 · **Retires debt:** D3 (part) · **Depends on:** nothing · **Blocks:** T-112, T-114, T-123

## Context

`run.get` currently raises `E_NOT_FOUND` with the message *"kinematics runs are not implemented yet"* and
`domain/kinematics/` is an empty package. COYPU produces train runs in three shapes — a strict-SI batch CSV,
a localised GUI report CSV, and numpy arrays inside a `.coypu` archive — and
`docs/data-contracts/coypu-kinematics.md` describes all three precisely. Builder has to normalise them into
one domain object and bake it into a table the client can interpolate at 60 Hz without ever calling back
(ADR 0007).

The subtle part is time. COYPU integrates `t[i] = t[i−1] + Δs/v_avg + dwell[i]`, capping speed at 0.5 m/s
near stops, so a station stop appears as a **time jump at nearly constant station**. A naive resample of that
produces division by zero, infinite speed, or a train that teleports.

## Preconditions

- `io/coypu/archive.py` reads `.coypu` and exposes `CoypuProject.kinematics(index)` returning a dict of
  `station_m`, `time_s`, `speed_ms`, `accel_ms2`, `f_trac_kn`, `f_brake_kn`, `f_res_kn`, `dwell_s`.
- `backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu` carries real kinematics arrays and
  `..._stops.csv` carries six stops.
- `domain/kinematics/` and `domain/analysis/` are empty packages.
- `conftest.py` exposes `kralupy_project`, `kralupy_stops_csv`, `kralupy` (the converted alignment).

Read before starting: `docs/data-contracts/coypu-kinematics.md` (the whole file — it is the specification for
this task), `docs/adr/0007-evaluation-locality.md`, `io/coypu/archive.py`, `domain/sampling.py` (for the
shape a baked table takes).

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/kinematics/run.py` | new — `KinematicsRun`, `Stop`, `RunDirection` |
| `backend/src/coypu_builder/domain/kinematics/table.py` | new — `RunTable`, `bake_run_table` |
| `backend/src/coypu_builder/domain/kinematics/__init__.py` | re-export |
| `backend/src/coypu_builder/io/coypu/kinematics_csv.py` | new — both CSV dialects and the stops CSV |
| `backend/src/coypu_builder/io/coypu/archive.py` | add a helper returning `KinematicsRun` objects |
| `backend/tests/test_kinematics_normalise.py` | new |
| `backend/tests/test_run_table.py` | new |
| `backend/tests/fixtures/synthetic/kinematics.py` | new — hand-built runs for edge cases |
| `tools/make_golden.py` | emit `shared/golden/run_table.json` |
| `shared/golden/run_table.json` | new — generated |
| `docs/data-contracts/coypu-kinematics.md` | append a "Builder normalisation" section describing the result |

## Contract

### Domain objects

```python
class RunDirection(IntEnum):
    FORWARD = 1     # station increases with time
    REVERSE = -1    # station decreases with time

@dataclass(frozen=True)
class Stop:
    station_m: float
    dwell_s: float = 30.0
    name: str = ""

@dataclass(frozen=True)
class KinematicsRun:
    """One simulated train run, normalised to SI and to an ascending time axis."""
    station_m: np.ndarray     # (n,) absolute metres, LandXML staStart basis
    time_s: np.ndarray        # (n,) non-decreasing, starts at 0.0
    speed_ms: np.ndarray      # (n,) non-negative magnitude
    accel_ms2: np.ndarray     # (n,)
    f_traction_kn: np.ndarray | None = None
    f_braking_kn: np.ndarray | None = None
    f_resistance_kn: np.ndarray | None = None
    direction: RunDirection = RunDirection.FORWARD
    stops: tuple[Stop, ...] = ()
    name: str = ""
    vehicle_index: int = 0
    warnings: tuple[str, ...] = ()
```

Normalisation rules, all of which need a test:

- **Units.** km → m, km/h → m/s. Station in the GUI dialect is km with three decimals; speed is km/h with
  one. The batch dialect is already SI.
- **Time axis.** Always ascending and starting at zero, regardless of direction. A source with an empty or
  missing time column produces `time_s = None`-equivalent behaviour: reject it with a clear error rather
  than fabricating a time base. A non-monotonic time column is a **warning plus a stable sort**, not a crash.
- **Direction.** `REVERSE` when station decreases with time. Station stays in source order; do not reverse
  the arrays. Consumers use `direction` to orient the vehicle.
- **Speed sign.** `speed_ms` is a magnitude and never negative; direction lives in `direction`.
- **Dwell.** Where the source carries `kinematicsDwellTimesS_i`, keep it. Where it carries only the time jump
  (both CSV dialects), detect dwells as a time increase greater than `dwell_threshold_s` (default 1.0) across
  a station increase below `station_epsilon_m` (default 0.5) and synthesise the `Stop` list from them,
  matching names from a stops CSV when one is supplied.
- **Missing force columns** are `None`, not zero arrays — a zero array is a claim about physics that the
  source did not make.

### Readers

```python
def read_kinematics_csv(path, *, stops: Sequence[Stop] = ()) -> KinematicsRun: ...
def read_stops_csv(path) -> tuple[Stop, ...]: ...
```

`read_kinematics_csv` detects the dialect from the header row: the strict-SI batch header
(`stationM,timeS,speedMs,...`) or the localised GUI report header in EN, CZ or DE per the table in the data
contract. Detection is by header content, never by filename. An unrecognised header raises a clear error
naming the headers it found and the dialects it knows.

`CoypuProject` gains:

```python
def kinematics_run(self, index: int, *, stops: Sequence[Stop] = ()) -> KinematicsRun: ...
def kinematics_runs(self, *, stops: Sequence[Stop] = ()) -> tuple[KinematicsRun, ...]: ...
```

### Baked table

```python
@dataclass(frozen=True)
class RunTable:
    """Time-uniform resample of a KinematicsRun; the client indexes it in O(1) at 60 Hz (ADR 0007)."""
    dt: float
    time_start: float                 # always 0.0 for now, kept explicit for future partial tables
    station: np.ndarray               # (m,)
    speed: np.ndarray                 # (m,)
    accel: np.ndarray                 # (m,)
    f_traction: np.ndarray | None
    f_braking: np.ndarray | None
    f_resistance: np.ndarray | None
    direction: RunDirection
    stops: tuple[Stop, ...]

    def __len__(self) -> int: ...
    @property
    def duration(self) -> float: ...

def bake_run_table(run: KinematicsRun, dt: float = 0.05) -> RunTable: ...
```

Time-uniform is the deliberate choice: the client computes `i = t / dt` with no search, and a dwell falls out
correctly as a flat run of stations rather than as a degenerate interval. An 18 km run at `dt = 0.05` is
roughly 25 000 rows — trivial to ship and to hold.

Resampling rules:

- Interpolate station, speed, accel and the forces linearly against `time_s`.
- Duplicate time values (zero-length intervals in the source) must be collapsed before interpolation, keeping
  the last sample, so that `np.interp` receives a strictly increasing x-array.
- During a dwell the station is flat and the speed is zero or near zero; the resample must reproduce that
  without a spike. Assert it.
- The final row lands exactly on the last source time, extending `dt` slightly on the last step if needed
  rather than truncating the run.

### Golden

`tools/make_golden.py` emits `shared/golden/run_table.json` from the Kralupy `.coypu` run 0: `dt`, row count,
duration, and a sample of ~20 rows at spread indices (including one inside a dwell and one at maximum speed)
as `{t, station, speed, accel}`. T-115 pins the client's run-table interpolation to it.

## Invariants

- **`domain/` is pure.** `domain/kinematics/` gets numpy only. All file reading lives in `io/coypu/`.
- **Stations are absolute metres everywhere in the domain.** COYPU's km appear only inside `io/coypu`.
- **ADR 0007:** the baked table is what the client interpolates; nothing here may assume a per-frame call.
- The `.coypu` reader's existing behaviour must not change for existing callers.

## Acceptance criteria

1. The Kralupy `.coypu` run 0 normalises with no warnings, station strictly within the alignment's
   `[station_start, station_end]`, time ascending from zero.
2. A round-trip test: the GUI-dialect CSV and the batch-dialect CSV of the same run normalise to the same
   `KinematicsRun` within the precision the dialects allow (km with 3 decimals ⇒ 1 mm; km/h with 1 decimal
   ⇒ ~0.03 m/s). Synthesise both CSVs in the test from the `.coypu` arrays rather than committing new files.
3. Localised GUI headers in all three languages (EN, CZ, DE) are detected. Test with synthetic headers.
4. A synthetic reversed run (descending station) yields `RunDirection.REVERSE`, non-negative speeds, and a
   correctly baked table.
5. Dwell detection recovers all six Kralupy stops, with names matched from the stops CSV to within 5 m of the
   CSV station.
6. `bake_run_table` produces no NaN, no infinity and no negative speed for any of: the Kralupy run, a
   synthetic run with a 120 s dwell, a synthetic run with duplicate time values, and a single-row run.
7. Interpolating the baked table back at the source times reproduces the source stations to within 1 cm
   outside dwells.
8. `shared/golden/run_table.json` regenerates byte-identically on a second `make_golden.py` run.
9. `uv run ruff check .` clean, `uv run pytest` green, existing 46 tests untouched.

## Out of scope

- Any protocol method — `run.get` stays as it is until T-114.
- The trainset chain, vehicle geometry, consist composition — T-112 and T-113.
- Re-simulating or recomputing kinematics. Builder consumes COYPU's results; it does not reproduce
  `vehicle_engine.py`.
- Speed-limit profiles, cant-deficiency results, `calculationCache.landXmlDerived` — later, and only if a
  milestone asks for them.

## Verification

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
uv run python ../tools/make_golden.py
git diff --stat shared/golden
```

## Report back

State: the dwell-detection thresholds you settled on and the evidence for them; how many stops were recovered
from the Kralupy run against the six in the CSV; any discrepancy between `docs/data-contracts/coypu-kinematics.md`
and what the archive actually contains; and the chosen `dt` with its row count and byte size for the Kralupy run.
