"""Regression: an aggregate answer gave a false all-clear while a live breach existed (found in a live run)."""
import unittest
from datetime import datetime, timezone

from agent.evidence import AGGREGATED, is_aggregate_sql, telemetry_records
from agent.rules import evaluate_rules
from agent.verifier import verify_draft

try:
    from test_dispatch_workflow import ScriptedPlanner, make_graph, of_type, run
except ImportError:
    from tests.test_dispatch_workflow import ScriptedPlanner, make_graph, of_type, run

NOW = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
V = "FDE_VIEWS.VW_ACTIVE_FLEET"
ALL_CLEAR = "No SOP action is required on current data."
NOT_CHECKED = "SOP thresholds were not checked against current readings for this question."


class TestIsAggregateSql(unittest.TestCase):
    def test_aggregates(self):
        for sql in (
            f"SELECT Risk_Classification, AVG(Current_Temperature_C) AS a, MAX(Timestamp) AS Timestamp FROM {V} GROUP BY Risk_Classification",
            f"SELECT count (*) AS n FROM {V}",
            f"SELECT * FROM (SELECT MAX(Current_Temperature_C) AS Current_Temperature_C, MAX(Timestamp) AS Timestamp FROM {V}) t",
        ):
            self.assertTrue(is_aggregate_sql(sql), sql)

    def test_individual_readings(self):
        for sql in (
            f"SELECT TOP 10 Timestamp, Current_Temperature_C FROM {V} ORDER BY Timestamp DESC",
            f"SELECT Timestamp, Current_Temperature_C FROM {V} WHERE Timestamp = (SELECT MAX(Timestamp) FROM {V})",
            "",
        ):
            self.assertFalse(is_aggregate_sql(sql), sql)


class TestAggregatedRecords(unittest.TestCase):
    def setUp(self):
        self.ledger = telemetry_records(
            ["Risk_Classification", "Current_Temperature_C", "Timestamp"], [("High Risk", 12.9, datetime(2026, 11, 18, 21, 30))],
            now=NOW, window_hours=24, aggregated=True,
        )

    def check(self, draft, ledger=None):
        return verify_draft(draft, ledger=self.ledger if ledger is None else ledger, triggers=[], historical=[], question="", window_hours=24)

    def test_never_fresh_and_never_triggers(self):
        self.assertEqual(self.ledger[0]["freshness"], AGGREGATED)
        self.assertEqual(evaluate_rules(self.ledger), ([], []))

    def test_all_clear_is_rejected_for_aggregates_and_without_telemetry(self):
        self.assertFalse(self.check(f"[T1] averages 12.9 °C. {ALL_CLEAR}")["passed"])
        self.assertFalse(self.check(f"The SOP sets a limit. {ALL_CLEAR}", ledger=[])["passed"])
        self.assertTrue(self.check(f"[T1] averages 12.9 °C. {NOT_CHECKED}")["passed"])

    def test_all_clear_is_allowed_for_individual_readings(self):
        ledger = telemetry_records(["Timestamp", "Current_Temperature_C"], [(datetime(2026, 11, 18, 21), 2.1)], now=NOW, window_hours=24)
        self.assertTrue(self.check(f"[T1] is 2.1 °C. {ALL_CLEAR}", ledger=ledger)["passed"])

    def test_graph_sends_a_false_all_clear_back(self):
        planner = ScriptedPlanner([("query_telemetry_db", {"sql_query": f"SELECT AVG(Current_Temperature_C) AS a, MAX(Timestamp) AS Timestamp FROM {V}"})])
        graph, _, _ = make_graph([f"[T1] {ALL_CLEAR}", f"[T1] {NOT_CHECKED}"], planner=planner)
        events = run(graph, "What is the average temperature per risk class?")
        self.assertEqual([v["passed"] for v in of_type(events, "verification")], [False, True])
        self.assertEqual(of_type(events, "evidence")[0]["fresh"], 0)
        self.assertNotIn(ALL_CLEAR, of_type(events, "final_answer")[0]["content"])


if __name__ == "__main__":
    unittest.main()
