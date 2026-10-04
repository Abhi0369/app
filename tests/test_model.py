import unittest

from app.sim.hub import Hub


class HubModelTests(unittest.TestCase):
    def test_energy_is_conserved(self):
        hub = Hub()
        for _ in range(180):
            hub.step({"cold": True, "irrigate": "Z2", "ice": True, "dryer": True})
        self.assertAlmostEqual(hub.truth()["energy_balance_error_kwh"], 0.0, places=3)

    def test_priority_never_overallocates_solar(self):
        hub = Hub()
        for _ in range(24):
            t = hub.step({"cold": True, "irrigate": "Z2", "ice": True, "dryer": True})
            solar_loads = sum(t["loads_kw"].values())
            self.assertLessEqual(solar_loads, t["pv_kw"] + 0.02)

    def test_outage_curtails_export_and_keeps_balance(self):
        hub = Hub()
        hub.grid.outage = True
        for _ in range(24):
            t = hub.step({"cold": True})
            self.assertEqual(t["loads_kw"]["export"], 0.0)
        self.assertAlmostEqual(hub.truth()["energy_balance_error_kwh"], 0.0, places=3)

    def test_probe_fault_is_reported(self):
        hub = Hub()
        hub.zones[1].probes[1].fault = "dropout"
        t = hub.step({})
        self.assertEqual(t["zones"][1]["probes"][1]["fault"], "dropout")


if __name__ == "__main__":
    unittest.main()
