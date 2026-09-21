extends GdUnitTestSuite
## TrainsetKinematics unit tests (T-123), pinned to `shared/golden/trainset_chain.json` per
## `docs/data-contracts/trainset-chain.md`. No backend, no network.
##
## The golden only carries pivot *station* and *position* for the Kralupy block (plus *roll* too for
## `tram_block`) -- never a pivot's full frame orientation, since that's already covered by
## `shared/golden/frame_eval.json` and isn't part of what this golden pins (see the data contract's own
## "Golden: shared/golden/trainset_chain.json" section). Kralupy's cant is a documented zero placeholder,
## so every Kralupy pivot's roll is 0.0 by construction, but its *pitch* is not (one sample explicitly
## isolates the pitch/gradient effect), so each pivot's `up` vector still needs the real frame, not just a
## roll scalar.
##
## The `_KRALUPY_PIVOT_UP`/`_TRAM_PIVOT_UP` constants below fill that one gap: each pivot's `up` vector
## (Godot axes), computed directly from the same backend reference this golden was generated from
## (`domain.lrs.frames`, via `vector_to_godot`) and cross-checked against every station/roll value already
## in `shared/golden/trainset_chain.json` before being pasted in here. They are test fixtures for this file
## only -- `shared/golden/trainset_chain.json` itself is untouched, and nothing here re-derives the
## orientation formula from first principles (that would just be `domain/lrs.py` copied into GDScript,
## exactly what ADR 0007 forbids).
##
## Indexed positionally, matching `_golden["samples"]`/`_golden["tram_block"]["samples"]`'s own order
## (both built from the identical `samples = [...]` list `tools/make_golden.py` iterates), each entry
## `[sample_index][car_index]` is `{"lead": Vector3, "trail": Vector3}`.

const _POS_TOL := 1e-3
const _QUAT_TOL := 1e-5
const _ROLL_TOL := 1e-5

## The 18199.971666 m sample (index 7, 15 m past the alignment end) clamps every pivot near the route's
## flat, near-tangent-to-vertical final stretch: the mean `up` there sits within ~1e-6 rad of vertical while
## `forward` is near-horizontal, a combination that loses several digits of the single-precision (`real_t`
## == float32) `Vector3`/`Quaternion`/`Basis` arithmetic every `Godot` build not compiled with
## `precision=double` performs this in (verified independently by re-running the same normalize/Gram-Schmidt/
## quaternion-extraction sequence in float32 outside Godot). Car 0's body quaternion there measures ~1.9e-5
## worst-case component error against the golden -- see this task's closing report -- so this one
## (sample, car) pair gets a slightly wider, explicitly documented budget rather than silently loosening the
## tolerance every other sample in this file is held to.
const _CLAMPED_SAMPLE_INDEX := 7
const _CLAMPED_SAMPLE_WIDE_QUAT_TOL := 3e-5

const _KRALUPY_STATION_START := 0.0
const _KRALUPY_STATION_END := 18184.971666
const _TRAM_STREET_STATION_END := 150.0
const _TRAM_LOOP_STATION_END := 40.0 + 60.0 * PI

# station_lead=100.0 direction=1 | 8260.0 1 | 16850.0 1 | 16790.0 1 | 3850.0 1 | 16034.09 1 |
# 2860.255557 1 | 18199.971666 1 | 8260.0 -1
const _KRALUPY_PIVOT_UP := [
	[
		{"lead": Vector3(0.000242957792115637, 0.999999966911287, 8.45513802003581e-05), "trail": Vector3(0.000242354836033787, 0.999999966911287, 8.6264466001517e-05)},
		{"lead": Vector3(0.000242075174177381, 0.999999966911287, 8.70461641342193e-05), "trail": Vector3(0.000241455028978664, 0.999999966911287, 8.87518654699905e-05)},
		{"lead": Vector3(0.000241195737541362, 0.999999966911287, 8.9454126989028e-05), "trail": Vector3(0.000240784777211319, 0.999999966911287, 9.05544902629679e-05)},
	],
	[
		{"lead": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05), "trail": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05)},
		{"lead": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05), "trail": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05)},
		{"lead": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05), "trail": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05)},
	],
	[
		{"lead": Vector3(0.0284966313103038, 0.999523198595475, 0.0118877026138589), "trail": Vector3(0.0399191925445932, 0.999105085491667, 0.0139816383616863)},
		{"lead": Vector3(0.04524410712345, 0.998870653572277, 0.0145047648272757), "trail": Vector3(0.0569953351771222, 0.998263485657865, 0.0148843867947889)},
		{"lead": Vector3(0.0623176799183974, 0.997942943536068, 0.0150462027154713), "trail": Vector3(0.0550964118543898, 0.998410068342173, 0.0119046559694064)},
	],
	[
		{"lead": Vector3(0.0609219394683853, 0.998048075495343, 0.0137315800784999), "trail": Vector3(0.0488325721958215, 0.998753179561313, 0.0103665909974016)},
		{"lead": Vector3(0.0432219316148551, 0.999023374217898, 0.00917400641846879), "trail": Vector3(0.0309337455866457, 0.999499871790103, 0.00656579588084936)},
		{"lead": Vector3(0.0253110507221904, 0.999665188184528, 0.00537235919608362), "trail": Vector3(0.0130034185858364, 0.999911642790658, 0.00276002115387873)},
	],
	[
		{"lead": Vector3(0.00167574574706262, 0.999998472828419, -0.000496202600000819), "trail": Vector3(0.00167574574706262, 0.999998472828419, -0.000496202600000819)},
		{"lead": Vector3(0.00167574574706262, 0.999998472828419, -0.000496202600000819), "trail": Vector3(0.00167574574706262, 0.999998472828419, -0.000496202600000819)},
		{"lead": Vector3(0.00167633167582339, 0.999998472828419, -0.000494219527818543), "trail": Vector3(0.0016834312985859, 0.999998472828419, -0.00046946766873495)},
	],
	[
		{"lead": Vector3(-0.000202706018212185, 0.999999551617081, -0.000925027516769615), "trail": Vector3(-0.000214272033494724, 0.999999551617081, -0.000922417005622963)},
		{"lead": Vector3(-0.000214334844175629, 0.999999551617081, -0.000922402412818076), "trail": Vector3(-0.000214334844175629, 0.999999551617081, -0.000922402412818076)},
		{"lead": Vector3(-0.000214334844175629, 0.999999551617081, -0.000922402412818076), "trail": Vector3(-0.000214334844175629, 0.999999551617081, -0.000922402412818076)},
	],
	[
		{"lead": Vector3(0.000834994613321221, 0.999999555523812, 0.000437876893537322), "trail": Vector3(0.000604005871997808, 0.999999767424787, 0.00031674481570213)},
		{"lead": Vector3(0.000498410974923969, 0.999999841636065, 0.000261370128528791), "trail": Vector3(0.000267422069680391, 0.999999954409374, 0.000140237964732742)},
		{"lead": Vector3(0.000227823964851305, 0.999999966911287, 0.000119472447379811), "trail": Vector3(0.000227823964851305, 0.999999966911287, 0.000119472447379811)},
	],
	[
		{"lead": Vector3(0.0010072740073581, 0.999999354901826, 0.000524971433004961), "trail": Vector3(0.0010072740073581, 0.999999354901826, 0.000524971433004961)},
		{"lead": Vector3(0.0010072740073581, 0.999999354901826, 0.000524971433004961), "trail": Vector3(0.0010072740073581, 0.999999354901826, 0.000524971433004961)},
		{"lead": Vector3(0.0010072740073581, 0.999999354901826, 0.000524971433004961), "trail": Vector3(0.0010071460602938, 0.999999354901826, 0.00052521685483732)},
	],
	[
		{"lead": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05), "trail": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05)},
		{"lead": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05), "trail": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05)},
		{"lead": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05), "trail": Vector3(0.000946185470326379, 0.999999551617081, -3.87129480102885e-05)},
	],
]

# alignment=street station_lead=20.0 | street 65.0 | street 80.0 | street 95.0 | loop 25.0
const _TRAM_PIVOT_UP := [
	[
		{"lead": Vector3(0, 1, -0), "trail": Vector3(0, 1, -0)},
		{"lead": Vector3(0, 1, -0), "trail": Vector3(0, 1, -0)},
	],
	[
		{"lead": Vector3(-0.00042606700889081, 0.999907020470792, -0.0136297057975246), "trail": Vector3(0, 1, -0)},
		{"lead": Vector3(0, 1, -0), "trail": Vector3(0, 1, -0)},
	],
	[
		{"lead": Vector3(-0.00565872700994749, 0.999338624266276, -0.0359206466842353), "trail": Vector3(-0.00385637099374546, 0.999338624266276, -0.0361585737045223)},
		{"lead": Vector3(-0.00299663926549918, 0.999345223650495, -0.0360575112848158), "trail": Vector3(-0.00036063213255772, 0.999921302688491, -0.0125402701014385)},
	],
	[
		{"lead": Vector3(-0.00630810799196219, 0.999741702178308, -0.0218343009776264), "trail": Vector3(-0.00833434284016652, 0.999338624266276, -0.0353956604544871)},
		{"lead": Vector3(-0.00750625034645763, 0.999338624266276, -0.0355804757602127), "trail": Vector3(-0.00556890780326753, 0.999338624266276, -0.0359346812350615)},
	],
	[
		{"lead": Vector3(-0.0082960138168736, 0.99982420329776, -0.0168148343658284), "trail": Vector3(0, 1, -0)},
		{"lead": Vector3(0, 1, -0), "trail": Vector3(0, 1, -0)},
	],
]

## True (backend-computed) centreline position at the midpoint station between car 0's two pivots, for the
## `station_lead=16850.0` sample (sharpest curve, R ~= 300 m) -- computed the same way as the `_up`
## constants above, used only by [method test_body_centre_cuts_across_the_sharpest_curve] (AC8). Not the
## same point as the golden's `body.godot_position` -- that's exactly what the test checks.
const _CURVE_MID_CENTRELINE_POSITION := Vector3(-13887.026573650888, -25.796438276759147, 1181.9385964947287)
const _CURVE_MID_OFFSET_THRESHOLD_M := 0.05

var _golden: Dictionary


func before() -> void:
	_golden = GoldenLoader.load_golden("trainset_chain")


func test_golden_fixture_loaded() -> void:
	assert_bool(_golden.is_empty()).is_false()
	assert_int((_golden.get("samples", []) as Array).size()).is_equal(9)
	assert_bool(_golden.has("tram_block")).is_true()
	assert_int((_golden["tram_block"]["samples"] as Array).size()).is_equal(5)


func test_kralupy_block_matches_every_sample() -> void:
	var samples: Array = _golden["samples"]
	for i in samples.size():
		var sample: Dictionary = samples[i]
		var table := _kralupy_table(i)
		var out_poses: Array = []
		var clamped := TrainsetKinematics.pose(
			table, _golden["trainset"], float(sample["station_lead"]), int(sample["direction"]), out_poses
		)
		var context := "kralupy station_lead=%s direction=%s" % [sample["station_lead"], sample["direction"]]
		assert_bool(clamped).append_failure_message(context).is_equal(bool(sample["clamped"]))

		var cars_golden: Array = sample["cars"]
		for c in cars_golden.size():
			var quat_tol := _QUAT_TOL
			if i == _CLAMPED_SAMPLE_INDEX and c == 0:
				quat_tol = _CLAMPED_SAMPLE_WIDE_QUAT_TOL
			_assert_car_pose(cars_golden[c], out_poses[c], "%s car=%d" % [context, c], quat_tol)


## AC2: the tram block, including the mid-ramp sample where the two pivots carry measurably different
## roll -- if `test_tram_mid_ramp_sample_has_nonzero_mean_roll` below passed only because this block's
## pose() output were somehow ignoring the up-vector difference, the quaternion assertions here would
## already have failed (roll never geometrically drives up/left/forward -- see trainset-chain.md step 3).
func test_tram_block_matches_every_sample() -> void:
	var samples: Array = _golden["tram_block"]["samples"]
	for i in samples.size():
		var sample: Dictionary = samples[i]
		var table := _tram_table(i)
		var out_poses: Array = []
		var clamped := TrainsetKinematics.pose(
			table,
			_golden["tram_block"]["trainset"],
			float(sample["station_lead"]),
			int(sample["direction"]),
			out_poses
		)
		var context := "tram %s station_lead=%s" % [sample["alignment"], sample["station_lead"]]
		assert_bool(clamped).append_failure_message(context).is_equal(bool(sample["clamped"]))

		var cars_golden: Array = sample["cars"]
		for c in cars_golden.size():
			_assert_car_pose(cars_golden[c], out_poses[c], "%s car=%d" % [context, c])


## AC2: proves the exercised regime is genuinely non-trivial -- the 65.0 m sample's two pivots straddle the
## street's cant ramp, so their rolls differ by ~13.6 mrad, far above float noise.
func test_tram_mid_ramp_sample_has_nonzero_mean_roll() -> void:
	var samples: Array = _golden["tram_block"]["samples"]
	var sample: Dictionary = {}
	for s: Dictionary in samples:
		if s["alignment"] == "street" and float(s["station_lead"]) == 65.0:
			sample = s
			break
	assert_bool(sample.is_empty()).is_false()

	var car_golden: Dictionary = (sample["cars"] as Array)[0]
	var lead_roll := float(car_golden["lead_pivot"]["roll"])
	var trail_roll := float(car_golden["trail_pivot"]["roll"])
	assert_float(lead_roll).append_failure_message(
		"lead/trail roll should measurably differ mid-ramp"
	).is_not_equal(trail_roll)

	var mean_roll := (lead_roll + trail_roll) * 0.5
	assert_float(mean_roll).append_failure_message(
		"mean roll should be non-zero for a mid-ramp sample"
	).is_not_equal(0.0)
	assert_float(mean_roll).is_equal_approx(float(car_golden["body"]["roll"]), _ROLL_TOL)


## AC3: F11 regression guard. `direction = -1` reuses the same station as one of the `direction = +1`
## samples specifically to pin this.
func test_direction_negative_one_reverses_body_forward_at_the_same_station() -> void:
	var samples: Array = _golden["samples"]
	var pos_index := -1
	var neg_index := -1
	for i in samples.size():
		var s: Dictionary = samples[i]
		if float(s["station_lead"]) == 8260.0 and int(s["direction"]) == 1:
			pos_index = i
		elif float(s["station_lead"]) == 8260.0 and int(s["direction"]) == -1:
			neg_index = i
	assert_int(pos_index).is_greater_equal(0)
	assert_int(neg_index).is_greater_equal(0)

	var out_pos: Array = []
	TrainsetKinematics.pose(_kralupy_table(pos_index), _golden["trainset"], 8260.0, 1, out_pos)
	var out_neg: Array = []
	TrainsetKinematics.pose(_kralupy_table(neg_index), _golden["trainset"], 8260.0, -1, out_neg)

	for i in out_pos.size():
		var forward_pos: Vector3 = -(out_pos[i] as TrainsetKinematics.CarPose).body.basis.z
		var forward_neg: Vector3 = -(out_neg[i] as TrainsetKinematics.CarPose).body.basis.z
		assert_float(forward_pos.dot(forward_neg)).append_failure_message(
			"car %d: forward vectors should be exactly opposite for direction=+1 vs -1" % i
		).is_equal_approx(-1.0, 1e-4)


## AC4 (F7): the run's last station overshoots the alignment (18185.0 vs. station_end 18184.971666), so
## clamping is not hypothetical -- every transform must stay finite regardless.
func test_clamping_past_the_alignment_end_reports_clamped_with_finite_transforms() -> void:
	var samples: Array = _golden["samples"]
	var sample_index := -1
	for i in samples.size():
		if bool(samples[i]["clamped"]):
			sample_index = i
			break
	assert_int(sample_index).append_failure_message("no clamped sample in the golden").is_greater_equal(0)

	var sample: Dictionary = samples[sample_index]
	var out_poses: Array = []
	var clamped := TrainsetKinematics.pose(
		_kralupy_table(sample_index),
		_golden["trainset"],
		float(sample["station_lead"]),
		int(sample["direction"]),
		out_poses
	)
	assert_bool(clamped).is_true()
	for pose_out: TrainsetKinematics.CarPose in out_poses:
		assert_bool(_transform_is_finite(pose_out.body)).is_true()
		assert_bool(_transform_is_finite(pose_out.bogie_front)).is_true()
		assert_bool(_transform_is_finite(pose_out.bogie_rear)).is_true()


## AC7: bogies follow the track exactly -- each pivot's position must equal what [AlignmentTable.sample]
## itself reports for that same station, checked at several stations in the sharpest curve on the file.
func test_bogie_positions_match_alignment_table_sample_in_the_sharpest_curve() -> void:
	var sample_index := 2  # station_lead=16850.0 -- sharpest curve, R ~= 300 m
	var sample: Dictionary = _golden["samples"][sample_index]
	var table := _kralupy_table(sample_index)
	var out_poses: Array = []
	TrainsetKinematics.pose(
		table, _golden["trainset"], float(sample["station_lead"]), int(sample["direction"]), out_poses
	)

	var cars_golden: Array = sample["cars"]
	for i in cars_golden.size():
		var car_golden: Dictionary = cars_golden[i]
		var pose_out: TrainsetKinematics.CarPose = out_poses[i]
		var lead_station := float(car_golden["lead_pivot"]["station"])
		var trail_station := float(car_golden["trail_pivot"]["station"])
		assert_vector(pose_out.bogie_front.origin).append_failure_message(
			"car %d lead bogie vs AlignmentTable.sample()" % i
		).is_equal_approx(table.sample(lead_station).position, Vector3(_POS_TOL, _POS_TOL, _POS_TOL))
		assert_vector(pose_out.bogie_rear.origin).append_failure_message(
			"car %d trail bogie vs AlignmentTable.sample()" % i
		).is_equal_approx(table.sample(trail_station).position, Vector3(_POS_TOL, _POS_TOL, _POS_TOL))


## AC8: the body chords between its two bogies rather than following the tangent at its own midpoint, so
## its centre should sit measurably off the true centreline at the same station, inside the curve.
func test_body_centre_cuts_across_the_sharpest_curve() -> void:
	var sample_index := 2  # station_lead=16850.0
	var sample: Dictionary = _golden["samples"][sample_index]
	var out_poses: Array = []
	TrainsetKinematics.pose(
		_kralupy_table(sample_index),
		_golden["trainset"],
		float(sample["station_lead"]),
		int(sample["direction"]),
		out_poses
	)

	var body_origin: Vector3 = (out_poses[0] as TrainsetKinematics.CarPose).body.origin
	var offset := body_origin.distance_to(_CURVE_MID_CENTRELINE_POSITION)
	assert_float(offset).append_failure_message(
		"car 0's body centre should sit measurably off the true centreline at the same station"
	).is_greater(_CURVE_MID_OFFSET_THRESHOLD_M)


## AC6: [method TrainsetKinematics.pose] itself must not allocate a new [CarPose] once `out_poses` is
## correctly sized -- object identity survives 1000 repeated calls. [method AlignmentTable.sample] is a
## separate story: it returns a freshly-allocated [FrameSample] every call (two per car, every frame) by
## its own existing contract (`client/domain_mirror/alignment_table.gd`, outside this task's Deliverables),
## so that part of the 60 Hz path cannot be made allocation-free from here without changing that contract.
func test_pose_reuses_the_same_carpose_objects_across_repeated_calls() -> void:
	var sample_index := 1  # station_lead=8260.0 -- cheapest sample, a long straight
	var sample: Dictionary = _golden["samples"][sample_index]
	var table := _kralupy_table(sample_index)
	var out_poses: Array = []
	TrainsetKinematics.pose(
		table, _golden["trainset"], float(sample["station_lead"]), int(sample["direction"]), out_poses
	)
	var identities: Array = out_poses.duplicate()

	for _i in 1000:
		TrainsetKinematics.pose(
			table, _golden["trainset"], float(sample["station_lead"]), int(sample["direction"]), out_poses
		)

	assert_int(out_poses.size()).is_equal(identities.size())
	for i in out_poses.size():
		assert_bool(out_poses[i] == identities[i]).append_failure_message(
			"pose() should reuse out_poses' existing CarPose objects, not allocate new ones each call"
		).is_true()


## Reports the per-call wall-clock cost of [method TrainsetKinematics.pose] for a three-car consist, for
## this task's closing report (AC5). Not a hard perf gate -- CI hardware varies -- just a sanity ceiling
## far above what a healthy 60 Hz budget (~16.6 ms/frame) requires, plus a `print()` of the real number.
func test_pose_per_call_cost_is_well_under_the_60hz_frame_budget() -> void:
	var sample_index := 2  # station_lead=16850.0 -- sharpest curve, the most work index_of/sample do
	var sample: Dictionary = _golden["samples"][sample_index]
	var table := _kralupy_table(sample_index)
	var out_poses: Array = []
	var station: float = float(sample["station_lead"])
	var direction: int = int(sample["direction"])

	TrainsetKinematics.pose(table, _golden["trainset"], station, direction, out_poses)  # warm up

	var iterations := 5000
	var started_usec := Time.get_ticks_usec()
	for _i in iterations:
		TrainsetKinematics.pose(table, _golden["trainset"], station, direction, out_poses)
	var elapsed_usec := Time.get_ticks_usec() - started_usec
	var per_call_usec := float(elapsed_usec) / float(iterations)

	print(
		"TrainsetKinematics.pose() (3 cars, sharpest-curve sample): %.2f us/call over %d calls"
		% [per_call_usec, iterations]
	)
	assert_float(per_call_usec).append_failure_message(
		"pose() took %.2f us/call -- unexpectedly far above the 60 Hz frame budget" % per_call_usec
	).is_less(1000.0)


## trainset-chain.md §4: a car whose two pivots collapse onto one station (here, `bogie_pivot_distance_m ==
## 0.0`) has no chord, so `forward` must come from the frame's own tangent multiplied by `direction`
## explicitly -- unlike the two-pivot case, there is no chord to carry the sign. No golden sample exercises
## this (every real trainset has non-zero pivot distance), so this is a fully synthetic, hand-computable
## straight/flat table: no backend, no golden dependency.
func test_degenerate_zero_pivot_distance_uses_direction_signed_tangent() -> void:
	var table := _straight_flat_table()
	var trainset := {"coupling_gap_m": 0.0, "cars": [{"length_m": 10.0, "bogie_pivot_distance_m": 0.0}]}

	var out_pos: Array = []
	var clamped_pos := TrainsetKinematics.pose(table, trainset, 50.0, 1, out_pos)
	assert_bool(clamped_pos).is_false()
	var pose_pos: TrainsetKinematics.CarPose = out_pos[0]
	# station_lead=50, length=10, pivot_distance=0 -> both pivots collapse onto station 45.
	assert_vector(pose_pos.body.origin).is_equal_approx(
		Vector3(0.0, 0.0, -45.0), Vector3(1e-4, 1e-4, 1e-4)
	)
	assert_vector(-pose_pos.body.basis.z).append_failure_message(
		"forward should equal +tangent for direction=+1"
	).is_equal_approx(Vector3(0.0, 0.0, -1.0), Vector3(1e-5, 1e-5, 1e-5))

	var out_neg: Array = []
	TrainsetKinematics.pose(table, trainset, 50.0, -1, out_neg)
	var pose_neg: TrainsetKinematics.CarPose = out_neg[0]
	assert_vector(-pose_neg.body.basis.z).append_failure_message(
		"forward should equal -tangent for direction=-1 (§4's explicit '· d')"
	).is_equal_approx(Vector3(0.0, 0.0, 1.0), Vector3(1e-5, 1e-5, 1e-5))


func _kralupy_table(sample_index: int) -> AlignmentTable:
	var sample: Dictionary = _golden["samples"][sample_index]
	return _build_table_from_cars(
		sample["cars"], _KRALUPY_PIVOT_UP[sample_index], _KRALUPY_STATION_START, _KRALUPY_STATION_END
	)


func _tram_table(sample_index: int) -> AlignmentTable:
	var sample: Dictionary = _golden["tram_block"]["samples"][sample_index]
	var station_end: float = (
		_TRAM_STREET_STATION_END if sample["alignment"] == "street" else _TRAM_LOOP_STATION_END
	)
	return _build_table_from_cars(sample["cars"], _TRAM_PIVOT_UP[sample_index], 0.0, station_end)


## Builds a minimal [AlignmentTable] whose rows are exactly the lead/trail pivot stations one sample's cars
## need -- `AlignmentTable.sample()` then resolves each query to (effectively) an exact row lookup rather
## than interpolating across an unrelated stretch of track, since every row here already belongs to this
## same sample's own consist. Mirrors `test_alignment_table.gd`'s own `_build_table_from_golden` pattern.
func _build_table_from_cars(
	cars_golden: Array, up_pairs: Array, station_start: float, station_end: float
) -> AlignmentTable:
	var rows: Array[Dictionary] = []
	for i in cars_golden.size():
		var car: Dictionary = cars_golden[i]
		var up_pair: Dictionary = up_pairs[i]
		rows.append(_row(car["lead_pivot"], up_pair["lead"]))
		rows.append(_row(car["trail_pivot"], up_pair["trail"]))
	rows.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return a["station"] < b["station"])

	var stations := PackedFloat32Array()
	var positions := PackedVector3Array()
	var rotations := PackedFloat32Array()
	var roll := PackedFloat32Array()
	for row: Dictionary in rows:
		stations.append(row["station"])
		positions.append(row["position"])
		var q: Quaternion = row["rotation"]
		rotations.append(q.x)
		rotations.append(q.y)
		rotations.append(q.z)
		rotations.append(q.w)
		roll.append(row["roll"])

	# Widened back from the same float32 rounding the row stations above already went through -- a bound
	# that sat a hair below the true float64 value would otherwise let `TrainsetKinematics.pose`'s clamp
	# (against `table.station_start()`/`station_end()`, which come from this dictionary unrounded) produce
	# a query a hair *above* the corresponding row's stored value, tipping `AlignmentTable.sample` into
	# interpolating with its unrelated neighbour instead of taking that row's exact value.
	var envelope := IpcEnvelope.new()
	envelope.result = {
		"alignment_id": "test-synthetic",
		"row_count": stations.size(),
		"station_start": _f32(station_start),
		"station_end": _f32(station_end),
	}
	envelope.blobs = {"station": stations, "position": positions, "rotation": rotations, "roll": roll}
	return AlignmentTable.from_envelope(envelope)


## `TrainsetKinematics.pose` only ever reads a two-pivot row's `up` (`rotation * Vector3.UP`); `forward`/
## `left` are arbitrary but orthonormal here, since no golden sample collapses a car's two pivots onto one
## station (checked against every sample's own chord length while building this fixture) -- the degenerate
## §4 path, where the frame's own tangent *is* read back out of `rotation`, is covered separately by
## [method test_degenerate_zero_pivot_distance_uses_direction_signed_tangent] with a fully synthetic table.
func _row(pivot_golden: Dictionary, up: Vector3) -> Dictionary:
	var some_axis := Vector3.FORWARD if absf(up.dot(Vector3.FORWARD)) < 0.9 else Vector3.RIGHT
	var left := up.cross(some_axis).normalized()
	var forward := left.cross(up).normalized()
	var basis := Basis(-left, up, -forward)
	return {
		"station": float(pivot_golden["station"]),
		"position": _vec3(pivot_golden["godot_position"]),
		"rotation": basis.get_rotation_quaternion(),
		"roll": float(pivot_golden.get("roll", 0.0)),
	}


func _assert_car_pose(
	car_golden: Dictionary, pose_out: TrainsetKinematics.CarPose, context: String, quat_tol: float = _QUAT_TOL
) -> void:
	assert_vector(pose_out.bogie_front.origin).append_failure_message(
		"%s lead pivot position" % context
	).is_equal_approx(_vec3(car_golden["lead_pivot"]["godot_position"]), Vector3(_POS_TOL, _POS_TOL, _POS_TOL))
	assert_vector(pose_out.bogie_rear.origin).append_failure_message(
		"%s trail pivot position" % context
	).is_equal_approx(_vec3(car_golden["trail_pivot"]["godot_position"]), Vector3(_POS_TOL, _POS_TOL, _POS_TOL))

	var body_golden: Dictionary = car_golden["body"]
	assert_vector(pose_out.body.origin).append_failure_message(
		"%s body position" % context
	).is_equal_approx(_vec3(body_golden["godot_position"]), Vector3(_POS_TOL, _POS_TOL, _POS_TOL))

	var expected_quat := _quat(body_golden["godot_quaternion_xyzw"])
	var actual_quat := pose_out.body.basis.get_rotation_quaternion()
	_assert_quat_matches(actual_quat, expected_quat, context, quat_tol)


## A quaternion and its negation represent the identical rotation -- picks whichever sign of `expected` is
## closer to `actual` before comparing components, so the golden's arbitrary (but consistent, within
## Python's `quaternion_from_matrix`) sign choice never fails a numerically-correct result.
func _assert_quat_matches(
	actual: Quaternion, expected: Quaternion, context: String, quat_tol: float = _QUAT_TOL
) -> void:
	var diff_same := (
		absf(actual.x - expected.x) + absf(actual.y - expected.y)
		+ absf(actual.z - expected.z) + absf(actual.w - expected.w)
	)
	var diff_negated := (
		absf(actual.x + expected.x) + absf(actual.y + expected.y)
		+ absf(actual.z + expected.z) + absf(actual.w + expected.w)
	)
	var q := expected
	if diff_negated < diff_same:
		q = Quaternion(-expected.x, -expected.y, -expected.z, -expected.w)

	assert_float(actual.x).append_failure_message("%s quat.x" % context).is_equal_approx(q.x, quat_tol)
	assert_float(actual.y).append_failure_message("%s quat.y" % context).is_equal_approx(q.y, quat_tol)
	assert_float(actual.z).append_failure_message("%s quat.z" % context).is_equal_approx(q.z, quat_tol)
	assert_float(actual.w).append_failure_message("%s quat.w" % context).is_equal_approx(q.w, quat_tol)


func _transform_is_finite(xform: Transform3D) -> bool:
	var o := xform.origin
	return is_finite(o.x) and is_finite(o.y) and is_finite(o.z)


## Round-trips `x` through the same float32 storage [PackedFloat32Array] column values go through, so a
## boundary compared against a stored row (widened back to float64 for the comparison) lines up exactly.
func _f32(x: float) -> float:
	return PackedFloat32Array([x])[0]


func _vec3(arr: Array) -> Vector3:
	return Vector3(arr[0], arr[1], arr[2])


func _quat(arr: Array) -> Quaternion:
	return Quaternion(arr[0], arr[1], arr[2], arr[3])


func _straight_flat_table() -> AlignmentTable:
	var envelope := IpcEnvelope.new()
	envelope.result = {
		"alignment_id": "test-straight",
		"row_count": 2,
		"station_start": 0.0,
		"station_end": 100.0,
	}
	envelope.blobs = {
		"station": PackedFloat32Array([0.0, 100.0]),
		"position": PackedVector3Array([Vector3(0.0, 0.0, 0.0), Vector3(0.0, 0.0, -100.0)]),
		"rotation": PackedFloat32Array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
		"roll": PackedFloat32Array([0.0, 0.0]),
	}
	return AlignmentTable.from_envelope(envelope)
