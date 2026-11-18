"""Planner prompt: picks tool calls for `open` questions (the only route that plans with an LLM)."""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

PLANNER_SYSTEM = (
    "You plan data retrieval for a cold-chain control tower. Call the tools needed to answer the "
    "question, in parallel where possible. Do not answer the question yourself. "
    "query_telemetry_db takes one T-SQL SELECT against FDE_VIEWS.VW_ACTIVE_FLEET (columns: "
    "Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code, "
    "Risk_Classification, Delay_Probability, Port_Congestion_Level, Route_Risk_Index); always "
    "include Timestamp."
)

planner_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", PLANNER_SYSTEM),
        MessagesPlaceholder("history", optional=True),
        ("human", "{question}"),
    ]
)
