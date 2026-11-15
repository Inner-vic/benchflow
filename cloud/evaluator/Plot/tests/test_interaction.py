from __future__ import annotations

import unittest

from pointcloud_viewer.interaction import PickCandidate, is_click, nearest_screen_candidate


class InteractionTests(unittest.TestCase):
    def test_click_threshold_distinguishes_drag(self) -> None:
        self.assertTrue(is_click((10.0, 10.0), (13.0, 12.0)))
        self.assertFalse(is_click((10.0, 10.0), (15.0, 10.0)))

    def test_nearest_candidate_wins(self) -> None:
        candidate = nearest_screen_candidate([
            PickCandidate("ground_truth", 1, 10.0, 0.2),
            PickCandidate("prediction", 2, 4.0, 0.5),
        ])
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.source_id, "prediction")
        self.assertEqual(candidate.original_index, 2)

    def test_prediction_wins_exact_distance_tie(self) -> None:
        candidate = nearest_screen_candidate([
            PickCandidate("ground_truth", 1, 5.0, 0.2),
            PickCandidate("prediction", 2, 5.0, 0.8),
        ])
        self.assertEqual(candidate.source_id, "prediction")

    def test_empty_click_returns_none(self) -> None:
        self.assertIsNone(nearest_screen_candidate([PickCandidate("prediction", 2, 13.0, 0.5)]))


if __name__ == "__main__":
    unittest.main()
