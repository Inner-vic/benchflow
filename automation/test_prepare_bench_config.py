from __future__ import annotations

import unittest

from prepare_bench_config import ConfigError, patch_yaml_text


SOURCE = """replay:
  enable: OFF  # replay
speed_gate:
  enable: false
perception_gate:
  enable: false
apa_gate:
  enable: false
gear_gate:
  enable: false
eval_mode:
  enable: ON
  mode: 1
eval_dump:
  enable: OFF
  output_dir: /old/output
  scene_root: /old/scenes
"""


class PrepareBenchConfigTests(unittest.TestCase):
    def test_patches_only_requested_values(self) -> None:
        patched, changes, observed = patch_yaml_text(
            SOURCE,
            scene_root="/app/livc-test/run/scenes",
            output_dir="/app/livc-test/run/output",
        )
        self.assertIn("replay:\n  enable: ON  # replay", patched)
        self.assertIn("eval_dump:\n  enable: ON", patched)
        self.assertIn("scene_root: /app/livc-test/run/scenes", patched)
        self.assertIn("eval_mode:\n  enable: ON", patched)
        self.assertEqual("1", observed["eval_mode.mode"])
        self.assertEqual(8, len(changes))

    def test_true_gates_are_patched_for_offline_evaluation(self) -> None:
        source = SOURCE.replace("  enable: false", "  enable: true")
        patched, changes, observed = patch_yaml_text(
            source,
            scene_root="/app/livc-test/run/scenes",
            output_dir="/app/livc-test/run/output",
        )
        self.assertEqual("true", observed["speed_gate.enable"])
        self.assertIn("speed_gate:\n  enable: false", patched)
        self.assertIn("perception_gate:\n  enable: false", patched)
        self.assertEqual(8, len(changes))

    def test_missing_gate_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            patch_yaml_text(
                SOURCE.replace("speed_gate:\n  enable: false\n", ""),
                scene_root="/app/livc-test/run/scenes",
                output_dir="/app/livc-test/run/output",
            )


if __name__ == "__main__":
    unittest.main()
