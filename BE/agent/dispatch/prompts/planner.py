"""Planner prompt: picks tool calls for `open` questions (the only route that plans with an LLM).

The human turn carries what `intake` already worked out in code (place, area and temperature
filters as SQL conditions), so the model doesn't have to guess coordinates. Settings arrive as
variables at render time. Literal braces in the template text must be doubled (`{{` / `}}`).
"""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from agent.intent import Intent, area_sql, period_sql

HISTORY_MESSAGES = 4

PLANNER_SYSTEM = """<system_context>
You plan data retrieval for a cold-chain control tower. Call the tools needed to answer the question, in parallel where possible. Do not answer the question yourself; another step does that.
</system_context>

<tools>
- query_telemetry_db: one read-only T-SQL SELECT against FDE_VIEWS.VW_ACTIVE_FLEET.
  Columns: Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code, Risk_Classification, Delay_Probability, Port_Congestion_Level, Route_Risk_Index, Data_Source.
  Data_Source is 'historical' (real readings, 2021-01 to 2024-08) or 'synthetic' (generated, later dates).
- fetch_corridor_conditions: measured and forecast weather for a latitude and longitude.
- search_compliance_sop: search the SOP knowledge base.
</tools>

<planning_rules>
Apply these checks in order. Each one that matches adds a tool call:
PRIORITY 1: the question asks about shipments, temperatures, risk, delay or congestion -> query_telemetry_db.
  - Counts, averages, minimums, maximums or comparisons: compute them in SQL (COUNT, AVG, MIN, MAX with GROUP BY) and include MAX(Timestamp) AS Timestamp. The answer step may not compute numbers itself.
  - Individual readings: SELECT TOP {max_rows}, always include Timestamp and Data_Source, ORDER BY Timestamp DESC.
  - Never mix data sources: in every aggregate query, either GROUP BY Data_Source (and select it) or filter Data_Source = 'historical'. Never rename the column.
  - Apply the area, period and temperature filters given in the human turn as WHERE conditions, exactly as written.
PRIORITY 2: coordinates are given in the human turn -> fetch_corridor_conditions for those coordinates.
PRIORITY 3: the question asks what to do, or about a threshold, rule or procedure -> search_compliance_sop with a short query in the SOP's words.
If nothing matches, call search_compliance_sop with the question.
</planning_rules>

<examples>
<example>
Question: "What is the average temperature per risk class?"
Calls: query_telemetry_db(SELECT Data_Source, Risk_Classification, AVG(Current_Temperature_C) AS Avg_Temperature_C, COUNT(*) AS Shipment_Count, MAX(Timestamp) AS Timestamp FROM FDE_VIEWS.VW_ACTIVE_FLEET GROUP BY Data_Source, Risk_Classification)
</example>
<example>
Question: "How many High Risk shipments are near Long Beach?" with coordinates 33.77, -118.19
Calls: query_telemetry_db(SELECT Data_Source, COUNT(*) AS Shipment_Count, MAX(Timestamp) AS Timestamp FROM FDE_VIEWS.VW_ACTIVE_FLEET WHERE Risk_Classification = 'High Risk' AND <area filter> GROUP BY Data_Source),
       fetch_corridor_conditions(latitude=33.77, longitude=-118.19)
</example>
</examples>

<critical_requirements>
- SELECT only, against FDE_VIEWS.VW_ACTIVE_FLEET only. No comments, no semicolons.
- Never answer in text; only call tools.
</critical_requirements>"""

PLANNER_HUMAN = (
    "Question to plan for: {question}\n\n"
    "Known place: {place}\n"
    "Coordinates: {coordinates}\n"
    "Area filter: {area_filter}\n"
    "Period filter: {period_filter}\n"
    "Temperature filter: {temperature_filter}\n"
    "Routing reason: {intent_reason}"
)

planner_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", PLANNER_SYSTEM),
        MessagesPlaceholder("history", optional=True, n_messages=HISTORY_MESSAGES),
        ("human", PLANNER_HUMAN),
    ]
)


def intent_variables(intent: Intent, *, half_width_deg: float) -> dict[str, str]:
    """The human-turn fields for an intent, as plain text and SQL conditions ("none" when absent).

    The area bounds are the same as the incident-scan query's (`intent.area_sql`)."""
    coordinates = f"{intent.latitude:g}, {intent.longitude:g}" if intent.has_area else "none"
    area = " AND ".join(area_sql(intent, half_width_deg=half_width_deg)) or "none"
    temps = []
    if intent.min_temp_c is not None:
        temps.append(f"Current_Temperature_C > {intent.min_temp_c:g}")
    if intent.max_temp_c is not None:
        temps.append(f"Current_Temperature_C < {intent.max_temp_c:g}")
    return {
        "place": intent.place or "none",
        "coordinates": coordinates,
        "area_filter": area,
        "period_filter": " AND ".join(period_sql(intent)) or "none",
        "temperature_filter": " AND ".join(temps) or "none",
        "intent_reason": intent.reason,
    }
