import unittest

from app.server import World


class AnalyticsTests(unittest.TestCase):
    def test_analytics_explain_the_run(self):
        world = World()
        for _ in range(300):
            world.step()
        analytics = world.analytics()
        self.assertGreater(analytics["window_hours"], 24)
        self.assertEqual(len(analytics["insights"]), 4)
        self.assertLessEqual(len(analytics["trend"]), 97)
        self.assertEqual(sum(x["name"] == "Export" for x in analytics["load_mix"]), 1)

    def test_event_log_is_in_snapshot(self):
        world = World()
        world.log_event("Test event", "Useful context")
        snapshot = world.snapshot()
        self.assertEqual(snapshot["analytics"]["events"][0]["title"], "Test event")
        self.assertIn("thermal_autonomy_h", snapshot["analytics"])


if __name__ == "__main__":
    unittest.main()
