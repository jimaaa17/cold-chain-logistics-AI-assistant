"""Synthetic fleet telemetry for demos, tests and training. Never real data.

Writes a CSV with the raw dataset's columns, so the unmodified ``legacy_ingest`` loader can load it
into a table of its own (``TBL_SC_FLEET_SYNTH_RAW``).

* ``config``    - run options, target rates and the table name.
* ``reference`` - empirical quantiles of the raw dataset (a small tracked JSON file).
* ``profiles``  - ``historical`` (same distributions as the raw dataset) and ``operational``.
* ``scenarios`` - incidents placed in the live window, for demos.
* ``report``    - checks on the generated rows, using the SOP rules the agent uses.
* ``load``      - loads or clears the synthetic table.
* ``cli``       - ``python -m data_synth``.
"""
