"""Chat model and tool factories shared by the dispatch workflow and the legacy ReAct graph.

Provider packages (`langchain_openai`, `langchain_google_genai`, `langchain_ollama`) are imported
inside `build_reasoner_llm`, so importing this module stays cheap.
"""
from __future__ import annotations

import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from agent.config import AgentSettings, get_agent_settings
from agent.tools import (
    make_corridor_conditions_tool,
    make_inprocess_sop_tool,
    make_telemetry_sql_tool,
)

logger = logging.getLogger(__name__)


def build_reasoner_llm(settings: AgentSettings | None = None) -> BaseChatModel:
    """Instantiate the configured chat model (GEMINI, OPENAI, or OLLAMA)."""
    cfg = settings or get_agent_settings()
    provider = cfg.agent_llm

    if provider == "OPENAI":
        from langchain_openai import ChatOpenAI

        api_key = cfg.openai_api_key.get_secret_value() if cfg.openai_api_key else None
        logger.info("Reasoner LLM: provider=OPENAI model=%s", cfg.openai_model)
        return ChatOpenAI(model=cfg.openai_model, temperature=cfg.llm_temperature, api_key=api_key)

    if provider == "GEMINI":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not cfg.gemini_api_key or not cfg.gemini_api_key.get_secret_value():
            raise ValueError("GEMINI_API_KEY is required when AGENT_LLM=GEMINI.")
        logger.info("Reasoner LLM: provider=GEMINI model=%s", cfg.gemini_model)
        extra = {} if cfg.gemini_thinking_budget is None else {"thinking_budget": cfg.gemini_thinking_budget}
        return ChatGoogleGenerativeAI(
            model=cfg.gemini_model,
            temperature=cfg.llm_temperature,
            google_api_key=cfg.gemini_api_key.get_secret_value(),
            max_output_tokens=cfg.llm_max_output_tokens,
            **extra,
        )

    logger.info("Reasoner LLM: provider=OLLAMA model=%s", cfg.ollama_model)
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        from langchain_community.chat_models.ollama import ChatOllama  # type: ignore[no-redef]
    return ChatOllama(model=cfg.ollama_model, temperature=cfg.llm_temperature, num_predict=1024)


def build_default_tools(settings: AgentSettings | None = None) -> list[BaseTool]:
    cfg = settings or get_agent_settings()
    return [
        make_telemetry_sql_tool(cfg),
        make_corridor_conditions_tool(cfg),
        make_inprocess_sop_tool(agent_settings=cfg),
    ]
