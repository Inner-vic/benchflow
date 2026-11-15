from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evaluation_runner import run_evaluation
from full_transaction import execute_full_evaluation, validate_full_job
from queue_worker import process_next
from runner_state import approve_stage, claim_bench, read_json, submit_job


class CommandRunner:
    def run(self, step, _spec, _execution, _context):
        return {"step": step, "ok": True, "exit_code": 0}


class TransferRunner:
    def __init__(self, report_source: Path):
        self.root = report_source

    def inventory(self, _endpoint, _path):
        return [{"path": "prediction.txt", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}]

    def exists(self, _endpoint, _path):
        return False

    def prepare_parent(self, _endpoint, _path):
        pass

    def copy_to_local(self, _endpoint, _source, destination):
        destination.mkdir(parents=True)
        (destination / "prediction.txt").write_text("x", encoding="utf-8")

    def copy_from_local(self, source, _endpoint, destination):
        self.destination = destination

    def finalize(self, _endpoint, _partial, _final):
        pass


class EvaluationRunner:
    def __init__(self, report: Path):
        self.report = report
        self.steps: list[str] = []

    def run(self, name, _command, context):
        self.steps.append(name)
        if name == "collect_report":
            shutil.copy2(self.report, context["local_report_path"])
        return {"step": name, "ok": True, "exit_code": 0}


def full_case(root: Path, *, protocol: str | None = None) -> tuple[dict, dict, Path]:
    job = {
        "run_id": "full-test",
        "mode": "offline",
        "execution": {"command_set_id": "full"},
        "replay_input": {"scene": "scene-a"},
        "predictions": {
            "transfer_set_id": "transfer",
            "source_path": "/bench/pred",
            "destination_path": "/lpai/pred",
        },
        "evaluation": {
            "evaluation_set_id": "eval",
            "prediction_path": "/lpai/pred",
            "config_path": "/lpai/runs/eval.yaml",
        },
    }
    if protocol is not None:
        job["runner_protocol"] = protocol
    steps = (
        "precheck", "capture_baseline", "stop_active", "start_candidate",
        "health_candidate", "replay", "restore", "verify_restore",
    )
    profile = {
        "execution": {
            "command_sets": {
                "full": {
                    "strategy": "isolated_candidate",
                    "commands": {
                        step: {"transport": "local", "argv": ["true"]}
                        for step in steps
                    },
                }
            }
        },
        "prediction_transfer": {
            "sets": {
                "transfer": {
                    "source": {
                        "transport": "ssh",
                        "host_alias": "bench",
                        "allowed_root": "/bench",
                    },
                    "workstation_cache_root": str(root / "cache"),
                    "destination": {
                        "transport": "ssh",
                        "host_alias": "lpai",
                        "allowed_root": "/lpai",
                    },
                }
            }
        },
        "evaluation": {
            "sets": {
                "eval": {
                    "allowed_prediction_root": "/lpai",
                    "allowed_config_root": "/lpai/runs",
                    "commands": {
                        "evaluate": {"transport": "local", "argv": ["true"]},
                        "collect_report": {"transport": "local", "argv": ["true"]},
                    },
                }
            }
        },
    }
    profile_path = root / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    return job, profile, profile_path


def submit_full(root: Path, job: dict) -> Path:
    plan = {
        "runner_protocol": "1.0",
        "preflight": {"status": "READY", "blockers": []},
        "steps": [],
    }
    submit_job(
        job=job,
        source=root / "job.json",
        plan=plan,
        runs_dir=root / "runs",
        submitted_by="user",
        client="test",
    )
    approve_stage(root / "runs", job["run_id"], "ACTIVATE", "user")
    approve_stage(root / "runs", job["run_id"], "REPLAY", "user")
    return root / "runs" / job["run_id"]


class FullTransactionTests(unittest.TestCase):
    def test_omitted_and_explicit_legacy_protocol_are_accepted(self) -> None:
        for protocol in (None, "1.0"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                job, _profile, profile_path = full_case(root, protocol=protocol)
                resolved = validate_full_job(job, profile_path, root / "run")
                self.assertEqual("/lpai/pred", resolved["transfer"]["destination"])

    def test_protocol_11_is_rejected_before_bench_claim_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, _profile, profile_path = full_case(root, protocol="1.1")
            submit_full(root, job)

            result = process_next(
                root / "runs", root / "locks", "full-evaluation", profile_path,
            )

            self.assertEqual("BLOCKED", result["status"])
            self.assertEqual("INVALID_EXECUTION_PROFILE", result["reason"])
            self.assertIn("requires runner_protocol 1.0", result["message"])
            self.assertFalse((root / "locks" / "bench.lock").exists())
            events = (root / "runs" / "full-test" / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("FULL_EVALUATION_STARTED", events)
            self.assertNotIn("TRANSACTION_STARTED", events)

    def test_success_reaches_full_completed_after_restore_transfer_and_fresh_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, profile_path = full_case(root)
            run_dir = submit_full(root, job)
            report = Path(__file__).resolve().parent / "fixtures" / "eval_report.sample.txt"

            cached_runner = EvaluationRunner(report)
            cached = run_evaluation(
                job, profile, run_dir, execute=True, runner=cached_runner,
            )
            self.assertEqual("a001", cached["attempt"])
            self.assertEqual("EVALUATION_COMPLETED", cached["state"])

            claim_bench(root / "runs", root / "locks", "full-test")
            fresh_runner = EvaluationRunner(report)
            result = execute_full_evaluation(
                runs_dir=root / "runs",
                locks_dir=root / "locks",
                run_id="full-test",
                profile_path=profile_path,
                command_runner=CommandRunner(),
                transfer_runner=TransferRunner(root),
                evaluation_runner=fresh_runner,
            )

            self.assertEqual("FULL_COMPLETED", result["state"])
            self.assertEqual("EVALUATION_COMPLETED", result["evaluation"]["state"])
            self.assertEqual("a002", result["evaluation"]["attempt"])
            self.assertEqual(["evaluate", "collect_report"], fresh_runner.steps)
            self.assertEqual("FULL_COMPLETED", read_json(run_dir / "state.json")["state"])
            self.assertFalse((root / "locks" / "bench.lock").exists())

            events = [
                json.loads(line)["event"]
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            ordered = [
                "FULL_EVALUATION_STARTED",
                "TRANSACTION_STARTED",
                "TRANSACTION_COMPLETED",
                "PREDICTION_TRANSFER_STARTED",
                "EVALUATION_STARTED",
                "FULL_EVALUATION_COMPLETED",
            ]
            positions = [events.index(name) for name in ordered]
            self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
