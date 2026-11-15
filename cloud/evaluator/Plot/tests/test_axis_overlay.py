from __future__ import annotations

import unittest

from pointcloud_viewer.axis_overlay import nice_tick_step, tick_values


class AxisOverlayTests(unittest.TestCase):
    def test_tick_step_never_smaller_than_centimetre(self) -> None:
        for span in (0.0001, 0.01, 0.031, 0.5, 4.0, 123.0):
            self.assertGreaterEqual(nice_tick_step(span), 0.01)

    def test_tick_step_uses_one_two_five_series(self) -> None:
        for span in (0.08, 0.4, 1.6, 8.0, 37.0):
            step = nice_tick_step(span)
            normalized = step / (10 ** int(__import__("math").floor(__import__("math").log10(step))))
            self.assertIn(round(normalized, 6), (1.0, 2.0, 5.0))

    def test_tick_values_are_monotonic_and_bounded(self) -> None:
        values = tick_values(-0.13, 0.42, 0.05)
        self.assertTrue((values[1:] > values[:-1]).all())
        self.assertTrue((values >= -0.13 - 1e-8).all())
        self.assertTrue((values <= 0.42 + 1e-8).all())


if __name__ == "__main__":
    unittest.main()
