import unittest

from network_doctor import diagnose


PROFILE = {
    "management": {
        "expected_interface": "eno2",
        "workstation_address": "192.0.2.12",
        "allowed_controller_subnets": ["192.0.2.10/21"],
        "tunnel_prefixes": ["tun", "utun"],
    },
    "bench": {"address": "192.0.2.15"},
}


def snapshot() -> dict:
    return {
        "controller": {"ip": "192.0.2.11", "route": {"interface": "eno2", "source": "192.0.2.12"}},
        "workstation": {"sshd_active": True, "ssh_listening": True},
        "bench": {
            "address": "192.0.2.15",
            "parent": {"name": "eno1", "exists": True, "operstate": "up", "carrier": 1},
            "vlan": {"name": "eno1.3", "exists": True, "operstate": "up", "carrier": 1},
            "route": {"interface": "eno1.3", "source": "192.0.2.14", "linkdown": False},
            "neighbor": "192.0.2.15 dev eno1.3 lladdr aa:bb:cc:dd:ee:ff REACHABLE",
            "tcp22_reachable": True,
            "lock": None,
        },
    }


class NetworkDoctorTests(unittest.TestCase):
    def test_ready_snapshot(self):
        result = diagnose(snapshot(), PROFILE)
        self.assertEqual("READY", result["status"])
        self.assertTrue(result["safe_to_start_bench_work"])
        self.assertFalse(result["mutations_performed"])

    def test_tunnel_return_route_is_separate_blocker(self):
        value = snapshot()
        value["controller"]["route"]["interface"] = "utun"
        codes = {item["code"] for item in diagnose(value, PROFILE)["issues"]}
        self.assertIn("CONTROLLER_RETURN_ROUTE_TUNNEL", codes)
        self.assertNotIn("BENCH_PHYSICAL_LINK_DOWN", codes)

    def test_physical_link_down_has_actionable_message(self):
        value = snapshot()
        value["bench"]["parent"].update({"operstate": "down", "carrier": 0})
        value["bench"]["route"]["linkdown"] = True
        value["bench"]["tcp22_reachable"] = False
        result = diagnose(value, PROFILE)
        issues = {item["code"]: item for item in result["issues"]}
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("BENCH_PHYSICAL_LINK_DOWN", issues)
        self.assertIn("未执行刷包、回灌", issues["BENCH_PHYSICAL_LINK_DOWN"]["message"])
        self.assertNotIn("BENCH_SSH_TIMEOUT", issues)

    def test_lock_reports_owner(self):
        value = snapshot()
        value["bench"]["lock"] = {"run_id": "run-7", "submitted_by": "lisi"}
        issues = {item["code"]: item for item in diagnose(value, PROFILE)["issues"]}
        self.assertIn("run-7", issues["BENCH_OCCUPIED"]["message"])
        self.assertIn("lisi", issues["BENCH_OCCUPIED"]["message"])

    def test_unapproved_controller_subnet_is_blocked(self):
        value = snapshot()
        value["controller"]["ip"] = "192.0.2.7"
        codes = {item["code"] for item in diagnose(value, PROFILE)["issues"]}
        self.assertIn("CONTROLLER_SUBNET_NOT_APPROVED", codes)

    def test_todo_management_subnet_blocks_deployment(self):
        profile = {**PROFILE, "management": {**PROFILE["management"], "allowed_controller_subnets": ["TODO: CIDR"]}}
        codes = {item["code"] for item in diagnose(snapshot(), profile)["issues"]}
        self.assertIn("DEVICE_PROFILE_INCOMPLETE", codes)


if __name__ == "__main__":
    unittest.main()
