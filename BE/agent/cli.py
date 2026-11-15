"""CLI entrypoint for the refactored backend Agent Orchestrator.

    python -m agent --dry-run
    python -m agent --prompt "Explain the difference between Tier 1 and Tier 2 escalation."
    python -m agent   # interactive REPL
"""
from __future__ import annotations

import argparse
import logging

from pydantic import ValidationError

from agent.config import AgentSettings, get_agent_settings
from agent.graph import AgentOrchestrator, load_system_prompt
from agent.tools.telemetry_sql import validate_readonly_select
from rag.config import configure_logging

logger = logging.getLogger("agent.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cold-Chain Logistics AI-Assistant Backend Orchestrator")
    parser.add_argument("--prompt", type=str, help="Run a single dispatcher prompt and exit")
    parser.add_argument("--session-id", type=str, default="cli-session", help="Session ID for state & auditing")
    parser.add_argument("--llm", type=str, help="Override AGENT_LLM (GEMINI, OPENAI, OLLAMA)")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration and guards without running")
    args = parser.parse_args(argv)

    try:
        settings = get_agent_settings()
        if args.llm is not None:
            settings = AgentSettings.model_validate({**settings.model_dump(), "agent_llm": args.llm})
    except (ValidationError, ValueError) as exc:
        configure_logging()
        logger.error("Configuration error: %s", exc)
        return 2

    configure_logging(settings.agent_log_level)

    if args.dry_run:
        sys_prompt = load_system_prompt(settings.system_prompt_path)
        sample_sql = f"SELECT TOP 5 * FROM {settings.sql_allowed_view}"
        validate_readonly_select(sample_sql)
        logger.info(
            "Dry-run OK | LLM=%s | SQL=%s:%d (%s) | View=%s | SystemPrompt=%d chars",
            settings.agent_llm,
            settings.sql_server_host,
            settings.sql_server_port,
            settings.sql_agent_user,
            settings.sql_allowed_view,
            len(str(sys_prompt.content)),
        )
        return 0

    try:
        orchestrator = AgentOrchestrator(settings)
    except Exception as exc:
        logger.error("Failed to initialize AgentOrchestrator: %s", exc)
        return 2

    if args.prompt is not None:
        try:
            answer = orchestrator.ask(args.prompt, session_id=args.session_id)
            print(answer)
            return 0
        except Exception as exc:
            logger.error("Orchestrator execution failed: %s", exc)
            return 1

    print("=" * 60)
    print(f"FDE Supply Chain Backend Orchestrator Online [LLM={settings.agent_llm}]")
    print("=" * 60)
    while True:
        try:
            user_input = input("\nDispatcher > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting orchestrator.")
            return 0
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            return 0
        try:
            for node_name, content in orchestrator.stream(user_input, session_id=args.session_id):
                if node_name == "tools":
                    print("   [System] Retrieved external data via ToolNode...")
                elif node_name == "reasoner" and content:
                    print(f"\nFDE Agent:\n{content}")
        except Exception as exc:
            logger.error("Orchestrator runtime error: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
