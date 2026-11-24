"""F7 regression: SOP thresholds are deterministic code, driven only by telemetry fields.

Weather (however windy) can never trigger a diversion; only Port_Congestion_Level > 7.0 on fresh
telemetry inside the Port of Long Beach / LA area can.
"""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.evidence import telemetry_records, weather_records
from agent.rules import evaluate_rules

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
WINDOW_H = 24.0
LB = (33.75, -118.20)  # inside PORTS_LB_LA_BOX
COLUMNS = ["Timestamp", "Latitude", "Longitude", "Current_Temperature_C",
           "Risk_Classification", "Delay_Probability", "Port_Congestion_Level"]


def _telemetry(congestion, *, age_h=1.0, lat=LB[0], lon=LB[1], temp=2.0, risk="Low", delay=0.1):
    ts = (NOW - timedelta(hours=age_h)).isoformat()
    return telemetry_records(COLUMNS, [[ts, lat, lon, temp, risk, delay, congestion]],
                             now=NOW, window_hours=WINDOW_H)


def _weather(wind_kmh):
    obs = {"latitude": LB[0], "longitude": LB[1],
           "current": {"time": (NOW - timedelta(minutes=15)).isoformat(),
                       "temperature_c": 18.0, "wind_kmh": wind_kmh}}
    return weather_records(obs, now=NOW, window_hours=WINDOW_H)


def _rule_ids(items):
    return {t["rule"] for t in items}


class TestSopRulesF7(unittest.TestCase):
    def test_light_breeze_with_normal_congestion_triggers_nothing(self):
        # The original F7 incident: 10.2 km/h wind was turned into an 8.5/10 "congestion index".
        triggers, historical = evaluate_rules(_weather(10.2) + _telemetry(3.0))
        self.assertEqual(triggers, [])
        self.assertEqual(historical, [])

    def test_weather_alone_never_triggers_even_in_a_storm(self):
        triggers, historical = evaluate_rules(_weather(120.0))
        self.assertEqual((triggers, historical), ([], []))

    def test_congestion_threshold_is_strictly_greater_than_7(self):
        for level in (6.9, 7.0):
            triggers, _ = evaluate_rules(_telemetry(level))
            self.assertNotIn("PORT_CONGESTION", _rule_ids(triggers), level)
        triggers, _ = evaluate_rules(_telemetry(7.1))
        self.assertIn("PORT_CONGESTION", _rule_ids(triggers))
        trig = next(t for t in triggers if t["rule"] == "PORT_CONGESTION")
        self.assertEqual(trig["actions"], ["divert_inland_empire"])
        self.assertTrue(trig["high_stakes"])  # diversion pauses for dispatcher approval
        self.assertEqual(trig["evidence"], "T1")

    def test_congestion_outside_lb_la_area_does_not_trigger(self):
        triggers, historical = evaluate_rules(_telemetry(9.0, lat=40.7, lon=-74.0))
        self.assertNotIn("PORT_CONGESTION", _rule_ids(triggers + historical))

    def test_missing_congestion_value_does_not_trigger(self):
        triggers, historical = evaluate_rules(_telemetry(None))
        self.assertNotIn("PORT_CONGESTION", _rule_ids(triggers + historical))

    def test_stale_congestion_is_history_not_an_action(self):
        triggers, historical = evaluate_rules(_telemetry(9.0, age_h=72))
        self.assertNotIn("PORT_CONGESTION", _rule_ids(triggers))
        self.assertIn("PORT_CONGESTION", _rule_ids(historical))

    def test_system_prompt_no_longer_asks_for_a_congestion_index(self):
        prompt = (Path(__file__).resolve().parents[1] / "BE/agent/prompts/system_prompt.txt").read_text()
        self.assertNotIn("[Index]/10", prompt)  # the old template slot that forced an invented score
        self.assertIn("Port_Congestion_Level", prompt)


if __name__ == "__main__":
    unittest.main()
