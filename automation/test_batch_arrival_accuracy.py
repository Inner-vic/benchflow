import unittest

from batch_arrival_accuracy import metrics, pair_contacts


def impact(t):
    return {"peak_time": t, "onset_time": t-.1}


class BatchArrivalTests(unittest.TestCase):
    def test_pairing_uses_jerk_only_and_keeps_unmatched_wheel(self):
        groups = pair_contacts([impact(1), impact(4)], [impact(1.05)], 1)
        self.assertEqual([(g["fl"],g["fr"]) for g in groups], [(0,0),(1,None)])
        self.assertEqual([g["paired"] for g in groups], [True,False])

    def test_ordered_pairing_does_not_greedily_steal_a_later_match(self):
        groups = pair_contacts([impact(0),impact(.8)], [impact(.7),impact(1.5)], .75)
        self.assertEqual([(g["fl"],g["fr"]) for g in groups], [(0,0),(1,1)])
        self.assertTrue(any(g["ambiguous"] for g in groups))

    def test_pairing_chooses_smallest_gap_after_maximizing_pairs(self):
        groups = pair_contacts([impact(1),impact(1.8)], [impact(1.85)], 1)
        self.assertEqual([(g["fl"],g["fr"]) for g in groups], [(0,None),(1,0)])
        self.assertEqual(pair_contacts([],[],1), [])

    def test_missing_contact_is_neither_prediction_miss_nor_gate_failure(self):
        def row(available,gate,error,status,flags=None):
            return {"contact_available":available,"gate_eligible":gate,"error_mm":error,
                    "status":status,"flags":flags or []}
        rows = [row(False,None,None,"contact_unavailable"),row(True,False,None,"missed"),
                row(True,True,None,"missed"),row(True,True,600,"late"),row(True,True,-100,"early",["flag"])]
        result = metrics(rows)
        self.assertEqual((result["expected"],result["contact_available"],result["contact_missing"]),(5,4,1))
        self.assertEqual((result["gate_failed"],result["gate_unknown"],result["gate_eligible"]),(1,1,3))
        self.assertEqual(result["unmatched_predictions_all"],2)
        self.assertEqual(result["unmatched_predictions_gated"],1)
        self.assertEqual(result["accuracy_samples"],1)
        self.assertEqual(result["p95_abs_mm"],100)
        self.assertEqual(result["out_of_range"],1)


if __name__ == "__main__":
    unittest.main()
