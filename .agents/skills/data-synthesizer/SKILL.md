---
name: data-synthesizer
description: >-
  Generate, check, load or clear synthetic cold-chain fleet telemetry for this project. Use when asked
  for synthetic, fake, sample, demo or live-looking sensor data, to make SOP rules trigger on fresh
  readings, to stage an incident (cooling failure, port congestion, high-risk delay), or to fill the gap
  after the 2021-2024 dataset.
---

# Data synthesizer

Wraps `python -m data_synth` (code in `BE/data_synth/`). Run from the repo root with the venv active and
`PYTHONPATH=BE:.`.

Why it exists: SOP actions are permitted only on readings newer than 24 h (`EVIDENCE_FRESHNESS_HOURS`),
and the real dataset ends 2024-08-29. Never widen that window to make old readings trigger. Generate new
readings instead. For questions about past years, no data is needed: the assistant reports what the SOP
would have required.

## 1. Choose

| Need | Use |
|---|---|
| Believable fleet, live demo, 24 h window | `--profile operational` (default) |
| Same distributions as the raw dataset (load tests, schema checks) | `--profile historical` |
| A known answer near Long Beach | `--scenario cooling_failure`, `port_congestion` or `high_risk_delay` |
| Nothing should trigger | `--scenario clean` |

`historical` reproduces the raw data's noise: about 97% of readings breach some rule and there is no
seasonality. The seasons, routes and rates in `operational` are assumptions in `BE/data_synth/profiles.py`,
not measurements. Say so if someone asks what the data proves.

## 2. Generate and check

```sh
python -m data_synth --scenario port_congestion
```

Read the printed report. If it lists a PROBLEM, fix the cause; don't loosen `OPERATIONAL_TARGETS` or
`MAX_KS` to get a pass. Output goes to `data/synthetic/` (git-ignored) with a manifest holding the seed
and end time, which reproduce the file. Validate with the loader, without a database:

```sh
python -m legacy_ingest --dry-run --csv data/synthetic/operational_port_congestion.csv
```

## 3. Load (changes a database: confirm with the user first)

Check `SQL_SERVER_HOST` first: the shell or `.env` may point at a remote server. Name the host when you ask.

```sh
python -m data_synth --scenario port_congestion --load
```

This replaces the rows of `TBL_SC_FLEET_SYNTH_RAW` only; the historical table is never touched. It refuses
a remote SQL host and `APP_ENV=production`. Don't use `--allow-remote` unless the user names a development
server. The view must include the synthetic table: run `BE/sql/add_synthetic_source.sql` once as admin.

The live window is denser (96 readings per truck a day) than the hourly backfill, so rates over a recent
period weight it more. Live readings go stale after 24 h. Run step 3 again for a fresh window.

## 4. Clear

```sh
python -m data_synth --reset
```

## Rules

- Never present synthetic data as real. Every answer that uses it must carry the "Synthetic data" line,
  which code adds; don't remove it.
- Seasonal conclusions come from `Data_Source = 'historical'` only. Seasons in synthetic data are ones
  this generator put there.
- Changing a column, threshold or the port area: update `BE/agent/rules.py` first. The generator's report
  reads its rules from there.
