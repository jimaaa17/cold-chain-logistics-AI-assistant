"""LangGraph ReAct state machine factory and orchestrator wrapper (TDD Section 2).

Zero import-time side effects: LLMs, database engines, and vector stores are constructed
lazily or injected explicitly.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Annotated, Any, Iterable, Sequence, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from agent.audit import AuditLogger
from agent.config import AgentSettings, get_agent_settings
from agent.llm import build_default_tools, build_reasoner_llm  # re-exported for existing imports
from agent.tracing import new_span_id, new_trace_id

logger = logging.getLogger(__name__)

FALLBACK_SYSTEM_PROMPT = "You are a Senior Data Scientist for a cold-chain logistics company."


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def load_system_prompt(path: Path | None = None) -> SystemMessage:
    target = path or (Path(__file__).resolve().parent / "prompts" / "system_prompt.txt")
    try:
        return SystemMessage(content=target.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("Could not read system prompt at %s; using fallback", target)
        return SystemMessage(content=FALLBACK_SYSTEM_PROMPT)


def format_message_content(content: Any) -> str:
    """Normalize string or multi-block list content (e.g. from Gemini) into clean text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


def build_agent_graph(
    settings: AgentSettings | None = None,
    *,
    llm: Any | None = None,
    tools: Sequence[BaseTool] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    system_prompt_path: Path | None = None,
):
    """Compile the LangGraph ReAct state machine with dependency-injected LLM and tools."""
    cfg = settings or get_agent_settings()
    active_tools = list(tools) if tools is not None else build_default_tools(cfg)
    active_llm = llm or build_reasoner_llm(cfg)
    llm_with_tools = active_llm.bind_tools(active_tools) if hasattr(active_llm, "bind_tools") else active_llm
    prompt_file = system_prompt_path or cfg.system_prompt_path

    def reasoning_node(state: AgentState) -> dict[str, list[BaseMessage]]:
        messages = list(state.get("messages", []))
        sys_msg = load_system_prompt(prompt_file)
        if not messages:
            messages = [sys_msg]
        elif isinstance(messages[0], SystemMessage):
            messages[0] = sys_msg
        else:
            messages = [sys_msg, *messages]

        if not any(not isinstance(m, SystemMessage) for m in messages):
            return {"messages": []}

        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    builder = StateGraph(AgentState)
    builder.add_node("reasoner", reasoning_node)
    builder.add_node("tools", ToolNode(active_tools))
    builder.add_edge(START, "reasoner")
    builder.add_conditional_edges("reasoner", tools_condition)
    builder.add_edge("tools", "reasoner")

    return builder.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())


class AgentOrchestrator:
    """High-level backend orchestrator managing session threads and immutable audit logging."""

    def __init__(
        self,
        settings: AgentSettings | None = None,
        *,
        llm: Any | None = None,
        tools: Sequence[BaseTool] | None = None,
        audit_logger: AuditLogger | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.settings = settings or get_agent_settings()
        self.tools = list(tools) if tools is not None else build_default_tools(self.settings)
        self.audit = audit_logger or AuditLogger.from_settings(self.settings)
        self.graph = build_agent_graph(
            self.settings,
            llm=llm,
            tools=self.tools,
            checkpointer=checkpointer,
        )

    def ask(self, prompt: str, *, session_id: str = "default-session") -> str:
        """Execute a complete ReAct turn and return the final synthesized answer."""
        final_answer = ""
        for node_name, text_out in self.stream(prompt, session_id=session_id):
            if node_name == "reasoner" and text_out:
                final_answer = text_out
        return final_answer

    def stream(self, prompt: str, *, session_id: str = "default-session") -> Iterable[tuple[str, str]]:
        """Stream `(node_name, formatted_content)` updates while recording SQL + Langfuse audit spans."""
        started = time.perf_counter()
        trace_id = new_trace_id(session_id)
        self.audit.record(
            session_id,
            "user_input",
            "Dispatcher",
            prompt,
            trace_id=trace_id,
            span_id=new_span_id(),
            elapsed_ms=0,
        )
        config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
        lf_callback = self.audit.bridge.get_langchain_callback(session_id, trace_id=trace_id)
        if lf_callback is not None:
            config["callbacks"] = [lf_callback]

        events = self.graph.stream(
            {"messages": [HumanMessage(content=prompt)]},
            config=config,
            stream_mode="updates",
        )
        for event in events:
            for node_name, node_state in event.items():
                for msg in node_state.get("messages", []):
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    if isinstance(msg, ToolMessage):
                        tool_name = getattr(msg, "name", None) or "tool"
                        content = format_message_content(msg.content)
                        self.audit.record(
                            session_id,
                            "tools",
                            tool_name,
                            content,
                            trace_id=trace_id,
                            span_id=new_span_id(),
                            elapsed_ms=elapsed_ms,
                        )
                        yield ("tools", content)
                    elif isinstance(msg, AIMessage):
                        tool_calls = getattr(msg, "tool_calls", None) or []
                        for tc in tool_calls:
                            tc_name = tc.get("name", "tool_call") if isinstance(tc, dict) else "tool_call"
                            self.audit.record(
                                session_id,
                                "reasoner_dispatch",
                                tc_name,
                                tc,
                                trace_id=trace_id,
                                span_id=new_span_id(),
                                elapsed_ms=elapsed_ms,
                            )
                        text_out = format_message_content(msg.content)
                        if text_out and not tool_calls:
                            self.audit.record(
                                session_id,
                                "reasoner",
                                "FinalSynthesis",
                                text_out,
                                trace_id=trace_id,
                                span_id=new_span_id(),
                                elapsed_ms=elapsed_ms,
                            )
                        if text_out:
                            yield ("reasoner", text_out)
