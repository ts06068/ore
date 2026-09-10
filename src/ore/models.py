"""Versioned, serializable contracts shared by ORE's execution components."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical_digest(value: Any) -> str:
    """Hash the actual contract, not its human-assigned version label."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)


class Budget(Contract):
    max_turns: int = Field(default=100, ge=1)
    max_seconds: float = Field(default=3600, gt=0)
    max_tokens: int | None = Field(default=None, ge=1)
    max_bytes: int = Field(default=1_000_000_000, ge=1)
    max_agent_workers: int = Field(default=4, ge=1, le=64)
    max_tasks: int = Field(default=10_000, ge=1)


class PublicationWindow(Contract):
    basis: Literal["issue_date", "publication_date", "online_date"] = "issue_date"
    start: date | None = Field(default=None, alias="from")
    until_exclusive: date | None = None

    @model_validator(mode="after")
    def ordered(self) -> "PublicationWindow":
        if self.start and self.until_exclusive and self.start >= self.until_exclusive:
            raise ValueError("publication window must have from < until_exclusive")
        return self


class Mission(Contract):
    goal: str = Field(min_length=1)
    urls: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)
    artifact_roles: list[str] = Field(default_factory=lambda: ["main_pdf", "supplement"])
    completeness: Literal["bounded", "inventory", "systematic"] = "bounded"
    model_policy: Literal["fixed", "auto", "quality_constrained_auto"] = "auto"
    model: str | None = None
    effort: str | None = None
    access_profile: str = "public"
    publication_window: PublicationWindow | None = None
    budget: Budget = Field(default_factory=Budget)
    scope: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_ui_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data.setdefault("goal", data.get("instructions") or data.get("name"))
        routing = data.get("routing") or {}
        if isinstance(routing, dict):
            for source, target in (("mode", "model_policy"), ("model", "model"), ("effort", "effort")):
                if source in routing:
                    data.setdefault(target, routing[source])
        if "access_profile_ref" in data:
            data.setdefault("access_profile", data["access_profile_ref"])
        if "limits" in data and "budget" not in data:
            data["budget"] = data["limits"]
        return data

    @field_validator("goal")
    @classmethod
    def nonblank_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("goal must not be blank")
        return value.strip()


class Rune(Contract):
    schema_version: str = "ore.rune/v1"
    protocol_id: str = Field(default="custom", min_length=1)
    protocol_version: str = Field(default="0.1.0", min_length=1)
    instructions: str = ""
    context: str | dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    milestones: list[str | dict[str, Any]] = Field(default_factory=list)
    recipes: list[dict[str, Any]] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    access_profile_ref: str | None = None

    @property
    def digest(self) -> str:
        return canonical_digest(self)


class Resource(Contract):
    canonical_id: str | None = None
    title: str | None = None
    doi: str | None = None
    url: str | None = None
    resource_type: str = "resource"
    eligibility: Literal["included", "excluded", "needs_review"] = "needs_review"
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Artifact(Contract):
    resource_id: str | None = None
    role: str = "attachment"
    version: str | None = None
    source_url: str | None = None
    sha256: str | None = None
    path: str | None = None
    media_type: str | None = None
    bytes: int | None = Field(default=None, ge=0)
    integrity: str = "unverified"
    identity: str = "unverified"
    status: str = "pending"


class Observation(Contract):
    kind: str
    source_url: str | None = None
    resource_id: str | None = None
    artifact_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(Contract):
    sha256: str
    path: str
    media_type: str
    bytes: int
    integrity: Literal["verified", "invalid", "unverified"]
    identity: Literal["verified", "mismatch", "unknown", "not_required"]
    status: Literal["verified", "needs_review", "invalid"]
    issues: list[str] = Field(default_factory=list)

