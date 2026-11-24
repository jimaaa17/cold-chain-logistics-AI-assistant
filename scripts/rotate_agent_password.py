"""Rotate the SQL password of the read-only agent login (`SQL_AGENT_USER`) and save it to `.env`.

    python scripts/rotate_agent_password.py          # show the plan, change nothing
    python scripts/rotate_agent_password.py --yes    # rotate

Connects with `SQL_ADMIN_USER` / `SQL_ADMIN_PASSWORD`. The new password is never printed. Order:
save the new `.env` next to the old one, ALTER LOGIN, test the new login, replace `.env`. If a step
fails, the saved copy is kept so the new password can't be lost.

Exit codes: 0 ok or plan only, 1 rotation failed, 2 configuration error.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import string
import sys
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "BE") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "BE"))

PASSWORD_LENGTH = 32
PENDING_SUFFIX = ".rotating"  # ".env.rotating" is covered by .gitignore
_PASSWORD_LINE = re.compile(r"(?im)^[ \t]*(?:export[ \t]+)?SQL_AGENT_PASSWORD[ \t]*=.*$")


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """Letters and digits only: safe in T-SQL literals, ODBC strings and `.env` without escaping."""
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(c in group for c in password) for group in (string.ascii_lowercase, string.ascii_uppercase, string.digits)):
            return password


def with_new_password(env_text: str, password: str) -> str:
    line = f"SQL_AGENT_PASSWORD={password}"
    if _PASSWORD_LINE.search(env_text):
        return _PASSWORD_LINE.sub(line, env_text)
    separator = "" if not env_text or env_text.endswith("\n") else "\n"
    return f"{env_text}{separator}{line}\n"


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _error_name(exc: BaseException) -> str:
    """Type and SQLSTATE only: driver messages can quote the statement, which holds the password."""
    state = exc.args[0] if exc.args and re.fullmatch(r"[0-9A-Z]{5}", str(exc.args[0])) else None
    return f"{type(exc).__name__} ({state})" if state else type(exc).__name__


def main(argv: list[str] | None = None, *, connect: Callable | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate the SQL password of the read-only agent login")
    parser.add_argument("--yes", action="store_true", help="Rotate now (without it, only show the plan)")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    args = parser.parse_args(argv)

    from dotenv import dotenv_values
    from pydantic import SecretStr, ValidationError

    from legacy_ingest import db
    from legacy_ingest.config import LegacySettings
    from legacy_ingest.schema import quote_ident

    env_file = args.env_file.resolve()
    if not env_file.is_file():
        print(f"Configuration error: {env_file} not found.", file=sys.stderr)
        return 2
    try:
        # Encryption is forced: ALTER LOGIN sends the new password in the statement text.
        admin = LegacySettings(_env_file=env_file, sql_encrypt=True)
        agent_user = os.getenv("SQL_AGENT_USER") or dotenv_values(env_file).get("SQL_AGENT_USER") or "USR_FDE_RO"
        login = quote_ident(agent_user)
    except (ValidationError, ValueError) as exc:
        print(f"Configuration error: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not admin.sql_admin_user or not admin.sql_admin_password:
        print("Configuration error: SQL_ADMIN_USER and SQL_ADMIN_PASSWORD must be set.", file=sys.stderr)
        return 2
    if agent_user.lower() == admin.sql_admin_user.lower():
        print("Configuration error: SQL_AGENT_USER is the admin login.", file=sys.stderr)
        return 2

    target = f"{agent_user} on {admin.sql_server_host}:{admin.sql_server_port}"
    if not args.yes:
        print(f"Plan: set a new password for {target}, test it, then update {env_file}.\nNothing changed. Add --yes to rotate.")
        return 0

    connect = connect or db.connect
    pending = env_file.with_name(env_file.name + PENDING_SUFFIX)
    password = generate_password()
    try:
        _write_private(pending, with_new_password(env_file.read_text(encoding="utf-8"), password))
    except FileExistsError:
        print(f"{pending} exists: an earlier rotation did not finish. Check which password works, then remove it.", file=sys.stderr)
        return 1

    step = "connecting as admin"
    try:
        conn = connect(admin)
        try:
            conn.autocommit = True
            step = "changing the password"
            conn.cursor().execute(f"ALTER LOGIN {login} WITH PASSWORD = '{password}';")
        finally:
            conn.close()
        step = "testing the new password"
        agent = admin.model_copy(update={"sql_admin_user": agent_user, "sql_admin_password": SecretStr(password)})
        check = connect(agent)
        try:
            check.cursor().execute("SELECT 1;").fetchone()
        finally:
            check.close()
    except Exception as exc:
        print(f"Rotation failed while {step}: {_error_name(exc)}. {env_file} was not changed.", file=sys.stderr)
        if step == "connecting as admin":
            pending.unlink()
        else:
            print(f"The database may already have the new password, which is saved in {pending}.", file=sys.stderr)
        return 1

    os.replace(pending, env_file)
    print(f"Rotated the password for {target} and updated {env_file}.")
    print("Copy SQL_AGENT_PASSWORD to every other machine that uses this login, then restart fde-api there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
