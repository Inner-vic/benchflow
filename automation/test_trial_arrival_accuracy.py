import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import test_probe_arrival_inputs as probe_fixtures
from trial_arrival_accuracy import summarize, trial


class TrialMetricsTests(unittest.TestCase):
    def test_gate_misses_and_outliers_keep_their_denominators(self):
        rows = [
            {"gate_eligible": True, "error_mm": -100, "status": "early"},
            {"gate_eligible": True, "error_mm": 2000, "status": "late"},
            {"gate_eligible": False, "error_mm": 10, "status": "late"},
            {"gate_eligible": True, "error_mm": None, "status": "missed"},
        ]
        result = summarize(rows)
        self.assertEqual((result["expected"], result["gate_eligible"], result["detected"], result["out_of_range"], result["accuracy_samples"]), (4, 3, 2, 1, 1))
        self.assertEqual(result["p95_abs_mm"], 100)
        self.assertEqual(result["signed_mean_mm"], -100)
        self.assertIsNone(result["sample_std_mm"])

    def test_small_sample_quantiles_and_sample_variance(self):
        result = summarize([{"gate_eligible": True, "error_mm": x, "status": "late"} for x in (10, 30)])
        self.assertEqual(result["p95_abs_mm"], 29)
        self.assertEqual(result["sample_variance_mm2"], 200)
        self.assertIsNone(summarize([])["p95_abs_mm"])


class TrialContactIndependenceTests(unittest.TestCase):
    def test_trial_does_not_feed_predictions_into_jerk_detector(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = probe_fixtures.ArrivalInputProbeTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            fixture.write_timeline()
            for wheel, path in fixture.truth.items():
                value = json.loads(path.read_text())
                # One synthetic contact within the fixture's prediction coverage.
                value["frames"][0]["ts_ns"] = 1787194170554674000
                value["frames"][1]["ts_ns"] = 1787194170564674000
                path.write_text(json.dumps(value))
            core_path = root / "core.py"
            core_path.write_text("# identity-only stub\n")
            calls = []

            def contacts(*args, **kwargs):
                calls.append((len(args), kwargs))
                return [{"onset_time": .005, "peak_time": .006, "peak_mag": 1000, "speed": 7.5, "n_peaks": 3}]

            core = types.SimpleNamespace(
                find_jerk_impacts=contacts,
                find_model_detections=lambda *args: [{"detect_time": 0., "zero_time": .01}],
                assign_detections=lambda impacts, detections: detections,
                calc_error=lambda *args: (75., "late", .01, .005),
                jerk_threshold_arr=lambda tj, *args: np.full(len(tj), 392.4),
                FRONT_FRONT_PAIR_WIN=1., _DIST_TRULY_ZERO_MM=.0001,
                DIST_ZERO_THRESHOLD=100., _THRESH_SPD_KPH=np.array([4, 9]),
                _THRESH_GS=np.array([40, 40]), MIN_JERK_PEAKS=3, EVENT_MERGE_GAP_S=.2,
                MERGE_GAP_DIST_MM=800., ONSET_MIN_RATIO=2., ONSET_MIN_SPAN_MM=10.,
                ONSET_GAP_AFTER_MM=400., STARTUP_GUARD_S=.5,
            )
            # Latest policy needs a newer trajectory for the changed same-time estimate.
            text = fixture.timeline.read_text()
            lines = text.splitlines()
            import csv
            rows = list(csv.DictReader(lines))
            rows[0]["traj_ts_ms"] = "1787194170354.674"
            rows[1]["traj_ts_ms"] = "1787194170454.674"
            with fixture.timeline.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fixture.columns)
                writer.writeheader()
                writer.writerows(rows)
            with patch("trial_arrival_accuracy.load_core", return_value=core):
                result, _ = trial(fixture.timeline, fixture.truth, core_path)
            self.assertEqual(calls, [(4, {}), (4, {})])
            self.assertEqual(result["metrics"]["combined"]["accuracy_samples"], 2)
            self.assertEqual(result["detail"][0]["error_mm"], 75)


if __name__ == "__main__":
    unittest.main()
