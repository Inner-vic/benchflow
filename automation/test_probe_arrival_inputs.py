import csv
import json
import tempfile
import unittest
from pathlib import Path

from probe_arrival_inputs import latest_success_at_time, main, ms_to_ns, probe_scene, read_jerk, read_timeline


class ArrivalInputProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.timeline = self.root / "timeline.csv"
        self.truth = {}
        for wheel in ("fl", "fr"):
            path = self.root / f"{wheel}.json"
            path.write_text(json.dumps({
                "signal_name": f"s_InDatCalcFAS_WhlJerk{wheel.upper()}_mps3",
                "frame_count": 2,
                "frames": [{"ts_ns": 1787194170552948803, "physical": -10.0},
                           {"ts_ns": 1787194170572948803, "physical": 30.0}],
            }))
            self.truth[wheel] = path
        self.columns = ["odom_ts_ms", "traj_ts_ms", "payload_ctrl_ts_ms", "publish_seq", "dds_write_ok", "speed_kph"]
        self.columns += [f"{w}_{f}_0" for w in ("fl", "fr") for f in ("dist_pre_mm", "dist_out_mm", "feat", "start_dist")]

    def write_timeline(self, malformed=False):
        with self.timeline.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=self.columns)
            writer.writeheader()
            for i, distance in enumerate((200, 150, 0)):
                row = dict.fromkeys(self.columns, 0)
                row.update(odom_ts_ms="1787194170554.674", traj_ts_ms="1787194170554.674",
                           payload_ctrl_ts_ms="1787194170554.674" if i < 2 else "1787194170564.674",
                           publish_seq=i, dds_write_ok=1, speed_kph=7.5,
                           fl_dist_out_mm_0=distance, fr_dist_out_mm_0=distance,
                           fl_start_dist_0=1, fr_start_dist_0=1)
                writer.writerow(row)
            if malformed:
                stream.write("1787194170574.674,0")

    def test_epoch_precision_does_not_round_through_float(self):
        self.assertEqual(ms_to_ns("1787194170554.674"), 1787194170554674000)
        with self.assertRaises(ValueError):
            ms_to_ns("NaN")

    def test_incomplete_tail_and_conflicting_times_are_preserved_as_evidence(self):
        self.write_timeline(malformed=True)
        info, samples = read_timeline(self.timeline, 0)
        self.assertEqual(len(samples), 3)
        self.assertEqual(info["invalid_rows"], [{"line": 5, "reason": "incomplete_or_extra_columns"}])
        self.assertEqual(info["conflicting_timestamp_group_count"], 1)
        self.assertEqual(info["duplicate_timestamp_rows"], 1)

    def test_probe_exports_all_samples_without_claiming_contacts_or_metrics(self):
        self.write_timeline()
        before = self.timeline.read_bytes()
        export = self.root / "export"
        plan = probe_scene(self.timeline, self.truth, 0, export)
        self.assertFalse(plan["accuracy_available"])
        self.assertGreater(plan["overlap_ns"]["fl"], 0)
        self.assertEqual(plan["jerk"]["fl"]["time_range_ns"][0], 1787194170552948803)
        with (export / "distance_front.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["timestamp_ns"], rows[2]["timestamp_ns"])
        self.assertEqual(self.timeline.read_bytes(), before)
        with self.assertRaises(FileExistsError):
            probe_scene(self.timeline, self.truth, 0, export)

    def test_jerk_identity_and_count_must_match(self):
        with self.assertRaisesRegex(ValueError, "signal_name"):
            read_jerk(self.truth["fl"], "fr")
        value = json.loads(self.truth["fl"].read_text())
        value["frame_count"] = 3
        self.truth["fl"].write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "frame_count"):
            read_jerk(self.truth["fl"], "fl")

    def test_nonzero_object_slot_still_uses_shared_slot_zero_gate(self):
        self.write_timeline()
        with self.timeline.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        extra = [f"{w}_{f}_1" for w in ("fl", "fr") for f in ("dist_pre_mm", "dist_out_mm", "feat", "start_dist")]
        with self.timeline.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=self.columns + extra)
            writer.writeheader()
            for row in rows:
                writer.writerow({**row, **dict.fromkeys(extra, 0)})
        info, _ = read_timeline(self.timeline, 1)
        self.assertEqual(info["dual_startdist_eligible_rows"], 3)

    def test_latest_policy_selects_one_entire_newer_message(self):
        self.write_timeline()
        _, samples = read_timeline(self.timeline, 0)
        samples[0]["trajectory_timestamp_ns"] -= 200_000_000
        samples[1]["trajectory_timestamp_ns"] -= 100_000_000
        samples[1]["fl_start_dist_0"] = 0
        samples[1]["fr_dist_out_mm_0"] = 999
        chosen = latest_success_at_time(samples, 0)
        self.assertEqual(len(chosen), 2)
        self.assertEqual(chosen[0]["publish_seq"], 1)
        self.assertEqual(chosen[0]["fl_dist_out_mm_0"], 150)
        self.assertEqual(chosen[0]["fr_dist_out_mm_0"], 999)
        self.assertEqual(chosen[0]["fl_start_dist_0"], 0)

    def test_latest_policy_does_not_replace_success_with_failed_dds(self):
        self.write_timeline()
        _, samples = read_timeline(self.timeline, 0)
        samples[1]["dds_write_ok"] = 0
        self.assertEqual(latest_success_at_time(samples, 0)[0]["publish_seq"], 0)

    def test_latest_policy_refuses_conflicting_unchanged_snapshot_or_regression(self):
        self.write_timeline()
        _, samples = read_timeline(self.timeline, 0)
        with self.assertRaisesRegex(ValueError, "different values"):
            latest_success_at_time(samples, 0)
        samples[1]["publish_seq"] = 0
        with self.assertRaisesRegex(ValueError, "publish_seq"):
            latest_success_at_time(samples, 0)

    def test_missing_task_inputs_return_error_and_no_accuracy(self):
        index = self.root / "index.json"
        index.write_text(json.dumps({"scenes": [{"scene": "scene-a", "task_copy": str(self.root / "missing")}]}))
        out = self.root / "result.json"
        self.assertEqual(main(["--task-index", str(index), "--truth-root", str(self.root), "--output", str(out)]), 2)
        report = json.loads(out.read_text())
        self.assertEqual(report["states"], {"INPUT_ERROR": 1})
        self.assertFalse(report["accuracy_available"])


if __name__ == "__main__":
    unittest.main()
