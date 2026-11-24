"""Offline tests: questions about past periods, the coded rollups and seasonality test, and data-source labels."""
import random
import re
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent.dispatch.nodes.ledger import MIXED_SOURCES_NOTE
from agent.evidence import REAL, SYNTHETIC, UNKNOWN_SOURCE, source_filter, telemetry_records
from agent.intent import build_scan_sql, classify_intent
from agent.period import parse_period
from agent.rollup import MAX_ROLLUP_ROWS, month_sql, year_sql
from agent.rules import RULES, evaluate_rules
from agent.seasonality import INSUFFICIENT, NOT_SEASONAL, SEASONAL, assess_seasonality, seasonality_record
from agent.tools.telemetry_sql import validate_readonly_select
from agent.verifier import verify_draft
from legacy_ingest.schema import TableRef, create_table_sql
from tests.test_dispatch_workflow import (
    COLUMNS, FRESH_BREACH, GOOD_DRAFT, HISTORICAL_BREACH, FakeEngine, ScriptedPlanner, make_graph, of_type, run,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
SRC_COLUMNS = [*COLUMNS, "Data_Source"]


class SourceEngine(FakeEngine):
    """FakeEngine whose result columns depend on the query: rollups get their own rows."""

    def __init__(self, rows=(), columns=SRC_COLUMNS, year=(), month=()):
        super().__init__(list(rows))
        self.columns, self.year, self.month = columns, year, month
        self.limits = []

    def connect(self):
        engine = self

        class _Result:
            def __init__(self, columns, rows):
                self.columns, self.rows = columns, rows

            def keys(self):
                return self.columns

            def fetchmany(self, n):
                engine.limits.append(n)
                return self.rows[:n]

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, clause, params=None):
                sql = str(clause)
                engine.sql.append(sql)
                for marker, rows in (("AS Month", engine.month), ("AS Year", engine.year)):
                    if marker in sql:
                        return _Result(list(rows[0]), [list(r.values()) for r in rows])
                return _Result(engine.columns, engine.rows)

        return _Conn()


def rollup_row(bucket, value, *, source=REAL, n=2700, share=0.27, years=4):
    row = {"Data_Source": source, bucket: value, "Reading_Count": n}
    for rule in RULES:
        base = 1 if rule.sql_scope else n
        if rule.sql_scope:
            row[f"{rule.id}_Base"] = base
        row[f"{rule.id}_Count"] = round(base * share)
        row[f"{rule.id}_Pct"] = round(100 * round(base * share) / base, 1)
    if bucket == "Month":
        row["Years_Covered"] = years
    row["Timestamp"] = datetime(2024, 8, 29)
    return row


FLAT_MONTHS = [rollup_row("Month", m) for m in range(1, 13)]
YEARS = [rollup_row("Year", y, n=8760) for y in (2021, 2022, 2023)]


# ---------------------------------------------------------------------------
class TestPeriod(unittest.TestCase):
    def bounds(self, text):
        p = parse_period(text, NOW)
        return p and (p.start and p.start.date().isoformat(), p.end and p.end.date().isoformat())

    def test_named_periods(self):
        for text, expected in {
            "breaches in 2023": ("2023-01-01", "2024-01-01"),
            "July 2023": ("2023-07-01", "2023-08-01"),
            "Dec 2023": ("2023-12-01", "2024-01-01"),
            "from March 2022 to June 2023": ("2022-03-01", "2023-07-01"),
            "2021 to 2024": ("2021-01-01", "2025-01-01"),
            "2021-2024": ("2021-01-01", "2025-01-01"),
            "between 2021 and 2024": ("2021-01-01", "2025-01-01"),
            "since 2022": ("2022-01-01", None),
            "summer of 2023": ("2023-06-01", "2023-09-01"),
            "winter 2023": ("2022-12-01", "2023-03-01"),
            "Q4 2022": ("2022-10-01", "2023-01-01"),
            "on 2023-07-14": ("2023-07-14", "2023-07-15"),
            "last year": ("2025-01-01", "2026-01-01"),
            "the past year": ("2025-11-24", None),
            "recent year": ("2025-11-24", None),
            "last 12 months": ("2025-11-24", None),
            "last 3 years": ("2023-11-24", None),
            "past 6 months": ("2026-05-24", None),
            "past 10 days": ("2026-11-14", None),
            "over several years": (None, None),
            "the historical data warehouse": (None, None),
        }.items():
            self.assertEqual(self.bounds(text), expected, text)

    def test_questions_about_now_have_no_period(self):
        for text in ("Which shipments are above 4 °C near Long Beach?", "readings in the last 24 hours", "shipments now",
                     "show 2050 shipments", "unit 20231", "at 33.75, -118.20", "2023-13-45"):
            self.assertIsNone(parse_period(text, NOW), text)

    def test_month_arithmetic_at_month_end(self):
        p = parse_period("past 6 months", datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.assertEqual(p.start.date().isoformat(), "2026-02-28")


class TestIntent(unittest.TestCase):
    def kind(self, q):
        return classify_intent(q, NOW).kind

    def test_history_questions(self):
        for q in ("How often would the SOP have triggered in 2021-2024, and is it seasonal?",
                  "SOP breaches in the past year", "Are temperature breaches seasonal?",
                  "Which months had the most SOP breaches?", "How many SOP violations per year since 2021?",
                  "What SOP escalations were there in the recent year?"):
            self.assertEqual(self.kind(q), "sop_history", q)

    def test_other_routes_are_unchanged(self):
        self.assertEqual(self.kind("Which shipments are above 4 °C near Long Beach and what does the SOP require?"), "incident_scan")
        self.assertEqual(self.kind("What does the SOP say about port congestion?"), "sop_lookup")
        self.assertEqual(self.kind("What is the average temperature per risk class?"), "open")
        self.assertEqual(self.kind("What was the average temperature in 2023?"), "open")
        self.assertEqual(self.kind("Tell me a joke from 2023"), "clarify")

    def test_past_readings_are_a_dated_scan_of_breaches(self):
        intent = classify_intent("Which shipments in July 2023 broke the SOP?", NOW)
        sql = build_scan_sql(intent, half_width_deg=0.35, max_rows=10)
        self.assertEqual(intent.kind, "incident_scan")
        self.assertIn("Timestamp >= '2023-07-01' AND Timestamp < '2023-08-01'", sql)
        self.assertIn("(Current_Temperature_C > 4.0) OR", sql)
        validate_readonly_select(sql)

    def test_current_scan_is_not_filtered(self):
        sql = build_scan_sql(classify_intent("Any breaches near Long Beach now?", NOW), half_width_deg=0.35, max_rows=10)
        self.assertNotIn("Timestamp >=", sql)
        self.assertNotIn(" OR ", sql)


class TestRollups(unittest.TestCase):
    def test_sql_predicates_match_the_python_rules(self):
        rng = random.Random(3)
        rows = [
            (rng.choice([33.60, 33.75, 34.0, 34.2]), rng.choice([-118.45, -118.2, -118.0, -117.2]),
             rng.choice([-0.1, 0.0, 2.0, 4.0, 4.01]), rng.choice(["High Risk", "Moderate Risk", "Low Risk"]),
             rng.choice([0.65, 0.66, 0.1]), rng.choice([7.0, 7.1, 2.0]))
            for _ in range(600)
        ]
        names = ["Latitude", "Longitude", "Current_Temperature_C", "Risk_Classification", "Delay_Probability", "Port_Congestion_Level"]
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute(f"CREATE TABLE v ({', '.join(names)})")
        db.executemany("INSERT INTO v VALUES (?,?,?,?,?,?)", rows)
        for rule in RULES:
            by_sql = [bool(r[0]) for r in db.execute(f"SELECT CASE WHEN {rule.sql_predicate} THEN 1 ELSE 0 END FROM v ORDER BY rowid")]
            by_code = [rule.check(dict(zip(names, r))) is not None for r in rows]
            self.assertEqual(by_sql, by_code, rule.id)
            self.assertTrue(any(by_code) and not all(by_code), rule.id)

    def test_rollups_pass_the_guard_and_never_mix_sources(self):
        intent = classify_intent("Are SOP breaches near Long Beach in 2021-2024 seasonal?", NOW)
        for build in (year_sql, month_sql):
            sql = build(intent, half_width_deg=0.35)
            validate_readonly_select(sql)
            self.assertIn("GROUP BY Data_Source", sql)
            self.assertIn(">= 30 THEN", sql)  # no rate over a handful of readings
            self.assertIn("Timestamp < '2025-01-01'", sql)
            self.assertIn("Latitude BETWEEN", sql)


class TestSeasonality(unittest.TestCase):
    def test_flat_rates_are_not_seasonal_and_sparse_rules_are_insufficient(self):
        out = assess_seasonality(FLAT_MONTHS)
        self.assertEqual(out["TEMP_BREACH"]["verdict"], NOT_SEASONAL)
        self.assertEqual(out["PORT_CONGESTION"]["verdict"], INSUFFICIENT)

    def test_a_real_season_is_found(self):
        months = [rollup_row("Month", m, share=0.40 if m in (6, 7, 8) else 0.22) for m in range(1, 13)]
        out = assess_seasonality(months)["TEMP_BREACH"]
        self.assertEqual(out["verdict"], SEASONAL)
        self.assertGreater(out["chi_square"], out["critical_value"])

    def test_synthetic_rows_never_decide(self):
        synthetic = [rollup_row("Month", m, source=SYNTHETIC, share=0.9 if m == 7 else 0.1) for m in range(1, 13)]
        self.assertEqual(assess_seasonality(synthetic)["TEMP_BREACH"]["verdict"], INSUFFICIENT)
        self.assertEqual(assess_seasonality(FLAT_MONTHS + synthetic)["TEMP_BREACH"]["verdict"], NOT_SEASONAL)

    def test_one_year_or_missing_months_are_insufficient(self):
        one_year = [rollup_row("Month", m, years=1) for m in range(1, 13)]
        for rows in (one_year, FLAT_MONTHS[:11], []):
            self.assertEqual(assess_seasonality(rows)["TEMP_BREACH"]["verdict"], INSUFFICIENT)


class TestVerifier(unittest.TestCase):
    def check(self, draft, months=FLAT_MONTHS):
        ledger = telemetry_records(list(YEARS[0]), [list(r.values()) for r in YEARS], now=NOW, window_hours=24, aggregated=True)
        ledger.append(seasonality_record(months))
        return verify_draft(draft, ledger=ledger, triggers=[], historical=[], question="2021-2024", window_hours=24)

    def test_seasonal_claims_are_rejected_when_the_test_found_none(self):
        for claim in ("Breaches peak in summer [H1].", "There is a clear seasonal pattern [H1].", "The breach rate is seasonal [H1].",
                      "Breaches are more common in July [H1].", "The data shows a seasonal peak [H1]."):
            problems = self.check(claim)["problems"]
            self.assertTrue(any("seasonal pattern" in p for p in problems), claim)

    def test_reporting_the_verdict_passes(self):
        for text in ("There is no seasonal pattern [H1].", "TEMP_BREACH: no seasonal pattern [H1].",
                     "The test did not find that breaches are seasonal [H1].", "Insufficient data to say whether it is seasonal [H1].",
                     "Temperature breaches would have triggered in 27.0% of 8760 readings [T1]."):
            self.assertEqual(self.check(text)["problems"], [], text)

    def test_a_claim_is_allowed_when_the_test_found_one(self):
        months = [rollup_row("Month", m, share=0.40 if m in (6, 7, 8) else 0.22) for m in range(1, 13)]
        self.assertEqual(self.check("There is a clear seasonal pattern [H1].", months)["problems"], [])

    def test_an_action_on_a_rollup_or_the_test_is_blocked(self):
        self.assertFalse(self.check("ACTION[escalate_tier2] on [T1]: x")["passed"])


class TestHistoryWorkflow(unittest.TestCase):
    def test_rollups_and_sop_without_planner_weather_or_approval(self):
        draft = "Retrospective: TEMP_BREACH would have triggered in 27.0% of readings [T1]. There is no seasonal pattern [H1]."
        engine = SourceEngine(year=YEARS, month=FLAT_MONTHS)
        graph, _, _ = make_graph([draft], engine=engine)
        events = run(graph, "How often would the SOP have triggered near Long Beach in 2021-2024, and is it seasonal?")
        self.assertEqual(sorted(e["name"] for e in of_type(events, "tool_input")),
                         ["query_telemetry_db", "query_telemetry_db", "search_compliance_sop"])
        self.assertEqual(engine.limits, [MAX_ROLLUP_ROWS, MAX_ROLLUP_ROWS])
        self.assertEqual(of_type(events, "approval_required"), [])
        final = of_type(events, "final_answer")[0]
        self.assertTrue(final["verified"], final["content"])
        self.assertNotIn("Synthetic data", final["content"])

    def test_synthetic_years_are_left_out_when_real_history_exists(self):
        years = [*YEARS, rollup_row("Year", 2024, source=SYNTHETIC, share=0.9)]
        graph, _, _ = make_graph(["TEMP_BREACH: 27.0% of readings [T1]."], engine=SourceEngine(year=years))
        events = run(graph, "How often would the SOP have triggered in 2021-2024?")
        self.assertEqual(of_type(events, "evidence")[0]["synthetic"], 0)
        self.assertIn("**Real readings only:**", of_type(events, "final_answer")[0]["content"])

    def test_a_period_with_only_synthetic_readings_is_answered_and_labelled(self):
        years = [rollup_row("Year", 2025, source=SYNTHETIC)]
        graph, _, _ = make_graph(["TEMP_BREACH: 27.0% of readings [T1]."], engine=SourceEngine(year=years))
        final = of_type(run(graph, "How many SOP breaches were there last year?"), "final_answer")[0]
        self.assertIn("**Synthetic data:** 1 of 1", final["content"])

    def test_a_full_page_of_past_readings_is_called_partial(self):
        graph, _, _ = make_graph(["Readings [T1] are historical."], engine=SourceEngine([(*HISTORICAL_BREACH, REAL)] * 10))
        final = of_type(run(graph, "Which shipments in February 2023 broke the SOP?"), "final_answer")[0]
        self.assertIn("**Partial list:**", final["content"])

    def test_month_rollup_only_when_seasonality_is_asked(self):
        engine = SourceEngine(year=YEARS, month=FLAT_MONTHS)
        graph, _, _ = make_graph(["TEMP_BREACH: 27.0% of readings [T1]."], engine=engine)
        run(graph, "How often would the SOP have triggered in 2021-2024?")
        self.assertEqual(len(engine.sql), 1)

    def test_false_seasonal_claim_is_sent_back_then_marked_unverified(self):
        draft = "Breaches peak in summer [H1]."
        graph, _, _ = make_graph([draft, draft], engine=SourceEngine(year=YEARS, month=FLAT_MONTHS))
        events = run(graph, "Are SOP breaches seasonal?")
        self.assertEqual([v["passed"] for v in of_type(events, "verification")], [False, False])
        self.assertIn("**Unverified:**", of_type(events, "final_answer")[0]["content"])

    def test_past_breach_shows_what_the_sop_would_have_required_but_permits_nothing(self):
        draft = "[T1] reached 19.0 °C on 2023-02-14 22:00. ACTION[divert_inland_empire] on [T1]: 9.5 > 7.0."
        seen = []

        graph, res, _ = make_graph([draft, draft], engine=SourceEngine([(*HISTORICAL_BREACH, REAL)]))
        original = res.llm().invoke
        object.__setattr__(res.llm(), "invoke", lambda msgs, *a, **k: (seen.append(msgs[-1].content), original(msgs, *a, **k))[1])
        events = run(graph, "Which shipments in February 2023 broke the SOP?")
        self.assertIn("SOP would have required: Divert to the Inland Empire", seen[0])
        self.assertEqual(of_type(events, "approval_required"), [])
        self.assertIn("**Blocked action**", of_type(events, "final_answer")[0]["content"])

    def test_a_question_about_the_past_never_gives_an_all_clear_for_now(self):
        # The query looked at February 2023 only; an incident could be under way right now.
        bad = "[T1] is historical (2023-02-14 22:00 UTC). No SOP action is required on current data."
        good = "[T1] is historical (2023-02-14 22:00 UTC). SOP thresholds were not checked against current readings for this question."
        graph, _, _ = make_graph([bad, good], engine=SourceEngine([(*HISTORICAL_BREACH, REAL)]))
        events = run(graph, "Which shipments in February 2023 broke the SOP?")
        self.assertEqual([v["passed"] for v in of_type(events, "verification")], [False, True])

    def test_the_freshness_rule_is_unchanged(self):
        recs = telemetry_records(SRC_COLUMNS, [(*HISTORICAL_BREACH, REAL)], now=NOW, window_hours=24)
        self.assertEqual(evaluate_rules(recs)[0], [])

    def test_outage_fallback_lists_the_rollup_and_the_verdict(self):
        graph, res, _ = make_graph([], engine=SourceEngine(year=YEARS, month=FLAT_MONTHS))
        events = run(graph, "Are SOP breaches in 2021-2024 seasonal?")
        content = of_type(events, "final_answer")[0]["content"]
        self.assertIn("## Seasonality test", content)
        self.assertIn(NOT_SEASONAL, content)


class TestDataSourceLabels(unittest.TestCase):
    def test_source_of_rows(self):
        def one(sql="", row=(FRESH_BREACH[0], 1.0), columns=("Timestamp", "Current_Temperature_C")):
            return telemetry_records(list(columns), [row], now=NOW, window_hours=24, sql=sql)[0]["data_source"]

        with_col = ("Timestamp", "Data_Source")
        self.assertEqual(one(row=(FRESH_BREACH[0], "synthetic"), columns=with_col), SYNTHETIC)
        self.assertEqual(one("SELECT x FROM v WHERE Data_Source = 'historical'"), REAL)
        self.assertEqual(one(), UNKNOWN_SOURCE)
        # A renamed literal is not evidence of where a row came from.
        self.assertEqual(one("SELECT 'historical' AS Data_Source FROM v", (FRESH_BREACH[0], "historical"), with_col), UNKNOWN_SOURCE)
        self.assertIsNone(source_filter("WHERE Data_Source = 'historical' OR Data_Source = 'historical'"))
        self.assertIsNone(source_filter("WHERE Data_Source = 'historical' AND Data_Source = 'synthetic'"))

    def test_synthetic_fresh_breach_triggers_and_is_labelled_everywhere(self):
        graph, _, _ = make_graph([GOOD_DRAFT], engine=SourceEngine([(*FRESH_BREACH, SYNTHETIC)]))
        events = run(graph, "Which shipments are above 4 °C near Long Beach?")
        self.assertEqual(of_type(events, "evidence")[0]["synthetic"], 1)
        request = of_type(events, "approval_required")[0]
        self.assertTrue(request["synthetic"] and request["actions"][0]["synthetic"])
        final = of_type(run(graph, resume={"decision": "approve", "by": "dana"}), "final_answer")[0]
        self.assertIn("**Synthetic data:** 1 of 1 telemetry records", final["content"])

    def test_real_rows_carry_no_notice(self):
        graph, _, _ = make_graph([GOOD_DRAFT], engine=SourceEngine([(*FRESH_BREACH, REAL)]))
        events = run(graph, "Which shipments are above 4 °C near Long Beach?")
        request = of_type(events, "approval_required")[0]
        self.assertFalse(request["synthetic"])
        final = of_type(run(graph, resume={"decision": "approve", "by": "dana"}), "final_answer")[0]
        self.assertNotIn("Synthetic data", final["content"])
        self.assertNotIn("Data source not stated", final["content"])

    def test_model_query_without_data_source_is_flagged(self):
        planner = ScriptedPlanner([("query_telemetry_db", {"sql_query":
                  "SELECT AVG(Current_Temperature_C) AS Avg_T, MAX(Timestamp) AS Timestamp FROM FDE_VIEWS.VW_ACTIVE_FLEET"})])
        engine = SourceEngine([(1.5, datetime(2026, 11, 24))], columns=["Avg_T", "Timestamp"])
        graph, _, _ = make_graph(["The average is 1.5 [T1]."], planner=planner, engine=engine)
        events = run(graph, "What is the average temperature?")
        self.assertIn(MIXED_SOURCES_NOTE, of_type(events, "evidence")[0]["notes"])
        self.assertIn("**Data source not stated:**", of_type(events, "final_answer")[0]["content"])


class TestSql(unittest.TestCase):
    def test_synthetic_table_matches_the_loader_schema(self):
        def squash(sql):
            return re.sub(r"\s+", " ", sql).strip()

        expected = squash(create_table_sql(TableRef("dbo", "TBL_SC_FLEET_SYNTH_RAW")))
        for name in ("add_synthetic_source.sql", "setup_security_view.sql"):
            self.assertIn(expected, squash((ROOT / "BE" / "sql" / name).read_text()), name)

    def test_both_scripts_define_the_same_view_and_deny_the_raw_table(self):
        views = []
        for name in ("add_synthetic_source.sql", "setup_security_view.sql"):
            text = (ROOT / "BE" / "sql" / name).read_text()
            views.append(re.search(r"VIEW FDE_VIEWS\.VW_ACTIVE_FLEET AS.*?GO", text, re.S).group(0))
            self.assertIn("DENY SELECT ON dbo.TBL_SC_FLEET_SYNTH_RAW TO USR_FDE_RO;", text)
        self.assertEqual(*views)


if __name__ == "__main__":
    unittest.main()
