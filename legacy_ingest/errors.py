"""Exception types mapped to CLI exit codes (see ``legacy_ingest.cli``)."""


class LegacyIngestError(RuntimeError):
    """Base class for every loader error."""


class ConfigError(LegacyIngestError):
    """Bad or missing configuration (credentials, driver, source file). Exit code 2."""


class IngestLocked(LegacyIngestError):
    """Another run currently holds the load lock for the target table. Exit code 3."""


class ValidationFailed(LegacyIngestError):
    """Too many rejected rows (or none valid); nothing is published. Exit code 1."""


class ReconciliationError(LegacyIngestError):
    """Staging table contents don't match what was sent. Exit code 1."""
