"""Legacy fleet-history loader: CSV -> typed MSSQL table ``dbo.TBL_SC_FLEET_HIST_RAW``.

Simulates the 2000s-era enterprise system the agent integrates with. The package is split so
each step is a small, testable unit:

* ``config``    - typed settings from env / ``.env``.
* ``schema``    - single source of truth for columns, DDL, INSERT and MERGE statements.
* ``extract``   - chunked CSV reader + source fingerprint.
* ``transform`` - clean -> legacy column mapping and typing.
* ``validate``  - row-level data-quality rules and reject quarantine.
* ``db``        - pyodbc connection (with retries), bulk insert, application lock.
* ``audit``     - run audit table and idempotency lookups.
* ``loader``    - orchestration: staging load, reconciliation, atomic swap / MERGE.
* ``cli``       - ``python -m legacy_ingest``.
"""
