"""Контракт даних: що знаходять правила і яку дію пропонують."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Action(BaseModel):
    kind: Literal["pause_ad", "add_negatives", "feed_labels"]
    target: str                                  # ad_id, campaign_id або 'feed'
    payload: dict = Field(default_factory=dict)
    requires_confirmation: bool = True           # витрата грошей і зміни в кабінеті: лише через людину


class Alert(BaseModel):
    source: Literal["meta", "google"]
    kind: str
    severity: Literal["high", "medium", "low"]
    entity_id: str
    entity_name: str
    title: str
    details: str
    metrics: dict[str, float] = Field(default_factory=dict)
    action: Action | None = None


class ProductDecision(BaseModel):
    item_id: str
    title: str
    label_from: int
    label_to: int
    rule: str
    reason: str
    metrics: dict[str, float] = Field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.label_from != self.label_to


class TermVerdict(BaseModel):
    term: str
    negative: bool
    source: Literal["pattern", "performance", "llm", "none"]
    reason: str
    clicks: int = 0
    cost: float = 0.0
    conversions: float = 0.0
