"""Structured outputs of the model decision steps. Code checks every field before using it."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RouteDecision(BaseModel):
    kind: Literal["incident_scan", "sop_lookup", "open", "clarify"]
    reasoning: str = Field(description="One sentence")
    place: str | None = Field(default=None, description="A place from the known-places list, if one is meant")
    latitude: float | None = Field(default=None, description="Only if the user typed coordinates")
    longitude: float | None = Field(default=None, description="Only if the user typed coordinates")
    min_temp_c: float | None = Field(default=None, description="Only if the user stated it")
    max_temp_c: float | None = Field(default=None, description="Only if the user stated it")


class Finding(BaseModel):
    """One reading past a threshold the SOP states. Code does the comparison and the actions."""

    evidence: str = Field(description="Telemetry id, e.g. T3")
    field: str = Field(description="Telemetry field name as in the ledger")
    operator: Literal[">", ">=", "<", "<="] = Field(description="The breach condition: reading OPERATOR threshold")
    threshold: float
    sop_evidence: str = Field(description="Id of the SOP excerpt stating the threshold, e.g. S2")
    sop_quote: str = Field(description="The SOP's exact words stating the threshold")


class Assessment(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    reasoning: str = Field(description="One or two sentences")


class Verdict(BaseModel):
    passed: bool
    problems: list[str] = Field(default_factory=list, description="Empty when passed")
