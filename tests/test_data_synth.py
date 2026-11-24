"""Offline tests for the synthetic telemetry generator. They need neither the raw CSV nor a database."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from agent.rules import RULES
from data_synth import cli
from data_synth.config import BACKFILL_START, SCENARIOS, SYNTH_TABLE, SynthConfig
from data_synth.load import UnsafeTarget, check_target, clear, synth_settings
from data_synth.profiles import generate, risk_class
from data_synth.reference import RISK, TIMESTAMP, Reference, ks_distance
from data_synth.report import breaches, build_report
from data_synth.scenarios import EXPECTED
from legacy_ingest.config import LegacySettings
from legacy_ingest.loader import dry_run

END = datetime(2026, 11, 24, 18, 0)
REF = Reference.load()


def make(**kw):
    cfg = SynthConfig(**{"end": END, **kw})
    return cfg, generate(cfg, REF)


class TestGeneration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.df = make()

    def test_operational_profile_meets_its_targets(self):
        rep = build_report(self.df, self.cfg, REF)
        self.assertEqual(rep.problems, [])
        self.assertGreater(rep.backfill_rows, 19_000)
        self.assertEqual(rep.live_rows, 24 * 96)

    def test_targets_hold_for_other_seeds(self):
        for seed in (1, 2, 3):
            cfg, df = make(seed=seed)
            self.assertEqual(build_report(df, cfg, REF).problems, [], f"seed {seed}")

    def test_timestamps_are_unique_inside_the_period_and_end_exactly_at_end(self):
        ts = self.df[TIMESTAMP]
        self.assertTrue(ts.is_unique)
        self.assertEqual(ts.min().to_pydatetime(), BACKFILL_START)
        self.assertEqual(ts.max().to_pydatetime(), END)
        live = ts[ts >= self.cfg.live_start]
        self.assertTrue((live > END - timedelta(hours=24)).all())  # all fresh at the end time

    def test_same_seed_reproduces_and_another_seed_differs(self):
        self.assertTrue(self.df.equals(make()[1]))
        self.assertFalse(self.df.equals(make(seed=8)[1]))

    def test_risk_class_follows_the_raw_dataset_cuts(self):
        self.assertEqual(list(risk_class(np.array([0.2999, 0.3, 0.6999, 0.7]))),
                         ["Low Risk", "Moderate Risk", "Moderate Risk", "High Risk"])
        self.assertEqual(list(self.df[RISK]), list(risk_class(self.df["disruption_likelihood_score"].to_numpy())))

    def test_operational_temperatures_persist_between_readings(self):
        # A truck's excursion lasts hours, so live breaches come in runs rather than single spikes.
        live = self.df[self.df[TIMESTAMP] >= self.cfg.live_start]
        hot = (live["iot_temperature"] > 4.0).to_numpy()
        step = 900 // self.cfg.trucks
        truck = (((END - live[TIMESTAMP]).dt.total_seconds() % 900) // step).to_numpy()
        neighbours = [hot[truck == k] for k in np.unique(truck)]
        pairs = sum(int((h[1:] & h[:-1]).sum()) for h in neighbours)
        self.assertGreater(pairs, 0.5 * hot.sum())

    def test_historical_profile_matches_the_reference_and_keeps_independence(self):
        cfg, df = make(profile="historical")
        self.assertEqual(build_report(df, cfg, REF).problems, [])
        self.assertLess(abs(df["iot_temperature"].autocorr(1)), 0.03)

    def test_live_only(self):
        cfg, df = make(backfill=False)
        self.assertEqual(len(df), 24 * 96)
        self.assertEqual(build_report(df, cfg, REF).problems, [])

    def test_loader_accepts_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.csv"
            self.df.to_csv(path, index=False, float_format="%.15g", date_format="%Y-%m-%d %H:%M:%S")
            settings = LegacySettings(_env_file=None, raw_data_path=path, reject_dir=Path(tmp) / "r")
            report = dry_run(settings)
        self.assertEqual((report.status, report.rows_rejected, report.rows_loaded), ("DRY_RUN", 0, len(self.df)))


class TestScenarios(unittest.TestCase):
    def test_each_scenario_triggers_exactly_its_rule_on_live_rows(self):
        for name in SCENARIOS:
            if name == "none":
                continue
            for seed in (1, 7):
                cfg, df = make(scenario=name, seed=seed, backfill=False)
                hits = {rule for rule, mask in breaches(df).items() if mask.any()}
                self.assertEqual(hits, EXPECTED[name], f"{name}, seed {seed}")

    def test_the_newest_reading_near_long_beach_is_a_scenario_reading(self):
        # The console's scan returns the newest readings of an area first.
        for name in ("cooling_failure", "port_congestion", "high_risk_delay"):
            _, df = make(scenario=name, backfill=False)
            newest = df.sort_values(TIMESTAMP).tail(1)
            rule = next(r for r in RULES if r.id in EXPECTED[name])
            self.assertTrue(breaches(newest)[rule.id].all(), name)

    def test_report_flags_a_scenario_that_did_not_take(self):
        cfg, df = make(scenario="clean", backfill=False)
        df.loc[0, "iot_temperature"] = 9.0
        self.assertTrue(any("scenario clean" in p for p in build_report(df, cfg, REF).problems))


class TestReportAndConfig(unittest.TestCase):
    def test_report_flags_broken_rows(self):
        cfg, df = make(backfill=False)
        df.loc[1, TIMESTAMP] = df.loc[0, TIMESTAMP]
        df.loc[2, "port_congestion_level"] = 11.0
        df.loc[3, RISK] = "Low Risk" if df.loc[3, RISK] != "Low Risk" else "High Risk"
        problems = " ".join(build_report(df, cfg, REF).problems)
        for expected in ("duplicate timestamps", "port_congestion_level outside", "risk_classification"):
            self.assertIn(expected, problems)

    def test_invalid_options(self):
        for kw in ({"profile": "x"}, {"scenario": "x"}, {"trucks": 1}, {"profile": "historical", "scenario": "clean"},
                   {"end": BACKFILL_START}):
            with self.assertRaises(ValueError):
                SynthConfig(**{"end": END, **kw})

    def test_ks_distance(self):
        a = np.linspace(0, 1, 500)
        self.assertEqual(ks_distance(a, a), 0.0)
        self.assertEqual(ks_distance(a, a + 2), 1.0)


class TestLoadGuards(unittest.TestCase):
    def settings(self, **kw):
        return LegacySettings(_env_file=None, **{"sql_server_host": "localhost", **kw})

    def test_only_the_synthetic_table_is_a_target(self):
        self.assertEqual(synth_settings(base=self.settings(target_table="TBL_SC_FLEET_HIST_RAW")).target_table, SYNTH_TABLE)

    def test_remote_hosts_and_production_are_refused(self):
        with mock.patch.dict(os.environ, {"APP_ENV": ""}):
            check_target(self.settings(sql_server_host="127.0.0.1"), allow_remote=False)
            remote = self.settings(sql_server_host="db.example.com")
            with self.assertRaises(UnsafeTarget):
                check_target(remote, allow_remote=False)
            check_target(remote, allow_remote=True)
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}), self.assertRaises(UnsafeTarget):
            check_target(self.settings(sql_server_host="localhost"), allow_remote=True)

    def test_clear_deletes_only_synthetic_rows(self):
        conn = mock.MagicMock()
        cursor = conn.cursor.return_value
        cursor.execute.return_value.fetchone.return_value = [1]
        cursor.execute.return_value.rowcount = 5
        with mock.patch.dict(os.environ, {"APP_ENV": ""}):
            self.assertEqual(clear(connect=lambda s: conn, base=self.settings()), 5)
        deletes = [c.args[0] for c in cursor.execute.call_args_list if "DELETE" in c.args[0]]
        self.assertEqual(deletes, [f"DELETE FROM [dbo].[{SYNTH_TABLE}];"])
        conn.commit.assert_called_once()


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(args))
        return code, out.getvalue()

    def test_writes_csv_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.csv"
            code, out = self.run_cli("--live-only", "--scenario", "cooling_failure", "--end", "2026-11-24T18:00:00Z", "--out", str(path))
            manifest = json.loads(path.with_suffix(".manifest.json").read_text())
        self.assertEqual(code, 0)
        self.assertIn("all checks passed", out)
        self.assertEqual((manifest["synthetic"], manifest["scenario"], manifest["end_utc"]),
                         (True, "cooling_failure", "2026-11-24T18:00:00"))

    def test_bad_configuration_exits_2(self):
        self.assertEqual(self.run_cli("--profile", "historical", "--scenario", "clean", "--end", "2026-11-24")[0], 2)

    def test_failed_checks_never_load(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("data_synth.load.load_csv") as load, \
                mock.patch("data_synth.report.MAX_KS", 0.0):
            code, _ = self.run_cli("--live-only", "--profile", "historical", "--end", "2026-11-24",
                                   "--out", str(Path(tmp) / "x.csv"), "--load")
        self.assertEqual(code, 1)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
