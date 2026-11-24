"""CLI entrypoint for the agentic dispatch workflow (same graph the API serves).

    python -m agent --dry-run
    python -m agent --prompt "Which shipments are above 4 °C near Long Beach?"
    python -m agent   # interactive REPL
    python -m agent --legacy-react --prompt "..."   # previous single ReAct loop, for comparison
"""
from __future__ import annotations

import argparse
import logging
import sys

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
    parser.add_argument("--legacy-react", action="store_true", help="Use the previous single ReAct loop")
    parser.add_argument(
        "--decision", choices=["approve", "reject"],
        help="Answer a high-stakes approval request non-interactively (otherwise asked on a terminal)",
    )
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
        try:
            settings.require_sql_password()
        except ValueError as exc:
            logger.error("Configuration error: %s", exc)
            return 2
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

    if args.legacy_react:
        return _run_legacy(settings, args)

    try:
        from agent.workflow import build_dispatch_workflow

        graph = build_dispatch_workflow(settings)
    except Exception as exc:
        logger.error("Failed to build the dispatch workflow: %s", exc)
        return 2

    if args.prompt is not None:
        try:
            _run_turn(graph, args.prompt, args.session_id, args.decision)
            return 0
        except Exception as exc:
            logger.error("Dispatch workflow failed: %s", exc)
            return 1

    print("=" * 60)
    print(f"FDE Dispatch Workflow Online [LLM={settings.agent_llm}]")
    print("=" * 60)
    while True:
        try:
            user_input = input("\nDispatcher > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return 0
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            return 0
        try:
            _run_turn(graph, user_input, args.session_id, args.decision)
        except Exception as exc:
            logger.error("Dispatch workflow runtime error: %s", exc)


def _print_events(events) -> dict | None:
    """Prints progress and the final answer; returns a pending approval request, if any."""
    pending = None
    for event in events:
        kind = event.get("type")
        if kind == "tool_input":
            print(f"   [data] {event['name']}")
        elif kind == "evidence":
            print(f"   [ledger] {event['records']} records, triggers: {', '.join(event['triggers']) or 'none'}")
            for gap in event.get("gaps", []):
                print(f"   [gap] {gap}")
        elif kind == "verification":
            verdict = "passed" if event["passed"] else ("failed, retrying" if event.get("retrying") else "failed")
            print(f"   [check] attempt {event['attempt']}: {verdict}")
            for problem in event.get("problems", []):
                print(f"           - {problem}")
        elif kind == "approval_required":
            pending = event
        elif kind == "final_answer":
            print(f"\n{event['content']}")
    return pending


def _run_turn(graph, prompt: str, session_id: str, decision: str | None) -> None:
    from agent.workflow import iter_dispatch_events

    pending = _print_events(iter_dispatch_events(graph, session_id=session_id, prompt=prompt))
    if pending is None:
        return
    print("\nApproval required:")
    for action in pending.get("actions", []):
        print(f"  - {action['label']} [{action['evidence']}] because {action.get('trigger', '')}")
    if decision is None and sys.stdin.isatty():
        answer = input("Approve? [y/N] ").strip().lower()
        decision = "approve" if answer in {"y", "yes"} else "reject"
    if decision is None:
        print("Paused for approval. Re-run with --decision approve|reject and the same --session-id.")
        return
    _print_events(
        iter_dispatch_events(graph, session_id=session_id, resume={"decision": decision, "by": "cli-dispatcher"})
    )


def _run_legacy(settings: AgentSettings, args) -> int:
    try:
        orchestrator = AgentOrchestrator(settings)
    except Exception as exc:
        logger.error("Failed to initialize AgentOrchestrator: %s", exc)
        return 2
    if args.prompt is None:
        logger.error("--legacy-react needs --prompt")
        return 2
    try:
        print(orchestrator.ask(args.prompt, session_id=args.session_id))
        return 0
    except Exception as exc:
        logger.error("Orchestrator execution failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
