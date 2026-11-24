"""Corridor weather tool (Open-Meteo): measured observations only, with retries and graceful degradation."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from langchain_core.tools import tool

from rag.core.resilience import with_retry

if TYPE_CHECKING:
    from agent.config import AgentSettings

logger = logging.getLogger(__name__)


class CorridorRequestError(ValueError):
    """Invalid coordinates or day window (reported to the caller, never retried)."""


def fetch_corridor_observations(
    latitude: float,
    longitude: float,
    *,
    client: httpx.Client,
    api_url: str = "https://api.open-meteo.com/v1/forecast",
    past_days: int = 0,
    forecast_days: int = 0,
) -> dict:
    """Measured weather from Open-Meteo as structured data. Reports observations only: no derived
    risk labels or scores (deciding what the weather means is the SOP rules' and agent's job)."""
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        raise CorridorRequestError(
            f"coordinates ({latitude}, {longitude}) are out of bounds "
            "(latitude must be in [-90, 90] and longitude in [-180, 180])."
        )
    if not (0 <= past_days <= 7 and 0 <= forecast_days <= 7):
        raise CorridorRequestError("past_days and forecast_days must be between 0 and 7.")

    params: dict[str, str | int | float] = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": "true",
        "timezone": "UTC",
    }
    if past_days > 0 or forecast_days > 0:
        params["daily"] = "temperature_2m_max,temperature_2m_min,windspeed_10m_max"
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
    current = raw.get("current_weather") or {}
    observations: dict = {
        "source": "open-meteo",
        "latitude": latitude,
        "longitude": longitude,
        "current": {
            "time": current.get("time"),
            "temperature_c": _num(current.get("temperature")),
            "wind_kmh": _num(current.get("windspeed")),
            "wind_direction_deg": _num(current.get("winddirection")),
        },
        "daily": [],
    }
    daily = raw.get("daily")
    if isinstance(daily, dict) and daily.get("time"):
        t_max = daily.get("temperature_2m_max") or []
        t_min = daily.get("temperature_2m_min") or []
        w_max = daily.get("windspeed_10m_max") or []
        for idx, day in enumerate(daily.get("time") or []):
            observations["daily"].append(
                {
                    "date": day,
                    "temp_max_c": _num(t_max[idx]) if idx < len(t_max) else None,
                    "temp_min_c": _num(t_min[idx]) if idx < len(t_min) else None,
                    "wind_max_kmh": _num(w_max[idx]) if idx < len(w_max) else None,
                }
            )
    return observations


def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value, unit: str) -> str:
    return "n/a" if value is None else f"{value}{unit}"


def format_corridor_observations(obs: dict, *, past_days: int = 0, forecast_days: int = 0) -> str:
    current = obs.get("current") or {}
    lines = [
        "--- CORRIDOR WEATHER (Open-Meteo, measured) ---",
        f"Target GPS: {obs.get('latitude')}, {obs.get('longitude')}",
        f"Observed at: {current.get('time') or 'n/a'} UTC",
        f"External Temp: {_fmt(current.get('temperature_c'), '°C')} | Wind Speed: {_fmt(current.get('wind_kmh'), ' km/h')}",
    ]
    if obs.get("daily"):
        lines.append(f"Daily window (past_days={past_days}, forecast_days={forecast_days}):")
        for day in obs["daily"]:
            lines.append(
                f"  - {day['date']}: Temp {_fmt(day['temp_min_c'], '°C')}..{_fmt(day['temp_max_c'], '°C')}"
                f" | Max Wind {_fmt(day['wind_max_kmh'], ' km/h')}"
            )
    lines.append("Note: weather only. Port congestion comes from telemetry (Port_Congestion_Level).")
    lines.append("-----------------------------------------------")
    return "\n".join(lines)


def fetch_corridor_telemetry(
    latitude: float,
    longitude: float,
    *,
    client: httpx.Client,
    api_url: str = "https://api.open-meteo.com/v1/forecast",
    past_days: int = 0,
    forecast_days: int = 0,
) -> str:
    """Text form of `fetch_corridor_observations` for the LLM tool interface."""
    try:
        obs = fetch_corridor_observations(
            latitude, longitude, client=client, api_url=api_url, past_days=past_days, forecast_days=forecast_days
        )
    except CorridorRequestError as exc:
        return f"Corridor API Validation Error: {exc}"
    return format_corridor_observations(obs, past_days=past_days, forecast_days=forecast_days)


def build_corridor_client(settings: "AgentSettings", transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(settings.corridor_timeout_s, connect=2.0),
        transport=transport or httpx.HTTPTransport(retries=2),
    )


def make_corridor_conditions_tool(
    settings: "AgentSettings | None" = None,
    *,
    transport: httpx.BaseTransport | None = None,
):
    """Create the `fetch_corridor_conditions` LangChain tool."""
    from agent.config import get_agent_settings

    cfg = settings or get_agent_settings()
    http_client = build_corridor_client(cfg, transport)

    @tool("fetch_corridor_conditions")
    def fetch_corridor_conditions(
        latitude: float,
        longitude: float,
        past_days: int = 0,
        forecast_days: int = 0,
    ) -> str:
        """Fetches measured weather (temperature, wind) from Open-Meteo for given GPS coordinates: current
        conditions, plus past_days (0-7) of history or forecast_days (0-7) of forecast. Weather only:
        port congestion is the Port_Congestion_Level column in query_telemetry_db."""
        try:
            return fetch_corridor_telemetry(
                latitude,
                longitude,
                client=http_client,
                api_url=cfg.corridor_api_url,
                past_days=past_days,
                forecast_days=forecast_days,
            )
        except Exception as exc:
            logger.warning("Corridor API communication failed: %s", exc)
            return f"Corridor API Communication Failure: {exc}"

    return fetch_corridor_conditions
