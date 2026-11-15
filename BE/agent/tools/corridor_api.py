"""Live corridor weather and congestion conditions tool with retries and graceful degradation (TDD 2.2)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from langchain_core.tools import tool

from rag.core.resilience import with_retry

if TYPE_CHECKING:
    from agent.config import AgentSettings

logger = logging.getLogger(__name__)


def fetch_corridor_telemetry(
    latitude: float,
    longitude: float,
    *,
    client: httpx.Client,
    api_url: str = "https://api.open-meteo.com/v1/forecast",
    wind_threshold_kmh: float = 10.0,
    past_days: int = 0,
    forecast_days: int = 0,
) -> str:
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return (
            f"Corridor API Validation Error: coordinates ({latitude}, {longitude}) are out of bounds "
            "(latitude must be in [-90, 90] and longitude in [-180, 180])."
        )
    if not (0 <= past_days <= 7 and 0 <= forecast_days <= 7):
        return (
            "Corridor API Validation Error: past_days and forecast_days must be between 0 and 7."
        )

    params: dict[str, str | int | float] = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": "true",
    }
    if past_days > 0 or forecast_days > 0:
        params["daily"] = "temperature_2m_max,temperature_2m_min,windspeed_10m_max"
        params["timezone"] = "UTC"
        if past_days > 0:
            params["past_days"] = past_days
        if forecast_days > 0:
            params["forecast_days"] = forecast_days

    @with_retry(attempts=3, max_wait=2.0)
    def _get() -> dict:
        response = client.get(api_url, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Malformed JSON response from weather service")
        return data

    raw = _get()
    payload = raw.get("current_weather") or {}
    temp = payload.get("temperature", "N/A")
    wind = float(payload.get("windspeed", 0.0) or 0.0)

    is_disrupted = wind > wind_threshold_kmh
    congestion_index = 8.5 if is_disrupted else 2.5
    status_note = "High Transit Disruption" if is_disrupted else "Corridor Normal"

    lines = [
        "--- LIVE CORRIDOR TELEMETRY ---",
        f"Target GPS: {latitude}, {longitude}",
        f"External Temp: {temp}°C | Wind Speed: {wind} km/h",
        f"Corridor Risk: {status_note} (Congestion Index: {congestion_index}/10)",
    ]

    daily = raw.get("daily")
    if isinstance(daily, dict) and daily.get("time"):
        times = daily.get("time") or []
        t_max = daily.get("temperature_2m_max") or []
        t_min = daily.get("temperature_2m_min") or []
        w_max = daily.get("windspeed_10m_max") or []
        lines.append(f"Temporal Window (past_days={past_days}, forecast_days={forecast_days}):")
        for idx, day_str in enumerate(times):
            day_tmax = t_max[idx] if idx < len(t_max) else "N/A"
            day_tmin = t_min[idx] if idx < len(t_min) else "N/A"
            day_wind = float(w_max[idx] or 0.0) if idx < len(w_max) else 0.0
            day_risk = "High Transit Disruption" if day_wind > wind_threshold_kmh else "Normal"
            lines.append(
                f"  - {day_str}: Temp {day_tmin}°C..{day_tmax}°C | Max Wind {day_wind} km/h ({day_risk})"
            )

    lines.append("-------------------------------")
    return "\n".join(lines)


def make_corridor_conditions_tool(
    settings: "AgentSettings | None" = None,
    *,
    transport: httpx.BaseTransport | None = None,
):
    """Create the `fetch_corridor_conditions` LangChain tool."""
    from agent.config import get_agent_settings

    cfg = settings or get_agent_settings()
    http_client = httpx.Client(
        timeout=httpx.Timeout(cfg.corridor_timeout_s, connect=2.0),
        transport=transport or httpx.HTTPTransport(retries=2),
    )

    @tool("fetch_corridor_conditions")
    def fetch_corridor_conditions(
        latitude: float,
        longitude: float,
        past_days: int = 0,
        forecast_days: int = 0,
    ) -> str:
        """Fetches past, present (live), and future (forecast) weather and corridor conditions from Open-Meteo for given GPS coordinates.
        Set past_days (0-7) for historical weather correlation or forecast_days (0-7) for upcoming route forecasts."""
        try:
            return fetch_corridor_telemetry(
                latitude,
                longitude,
                client=http_client,
                api_url=cfg.corridor_api_url,
                wind_threshold_kmh=cfg.corridor_wind_threshold_kmh,
                past_days=past_days,
                forecast_days=forecast_days,
            )
        except Exception as exc:
            logger.warning("Corridor API communication failed: %s", exc)
            return f"Corridor API Communication Failure: {exc}"

    return fetch_corridor_conditions
