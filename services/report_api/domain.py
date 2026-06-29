from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class ReportType(StrEnum):
    TRANSFER_DAILY = "transfer_daily"
    WORLD_CUP_DAILY = "world_cup_daily"
    MATCH_PREDICTION = "match_prediction"


class ReportLength(StrEnum):
    CONCISE = "concise"
    STANDARD = "standard"
    DEEP = "deep"


class MatchStage(StrEnum):
    GROUP = "group"
    KNOCKOUT = "knockout"


class Evidence(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    published_at: datetime
    source_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)

    @field_validator("published_at")
    @classmethod
    def published_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        return value


class ReportRequest(BaseModel):
    report_type: ReportType
    subject: str = Field(min_length=1, max_length=300)
    report_date: date
    data_cutoff: datetime
    length: ReportLength = ReportLength.STANDARD
    focus: list[str] = Field(default_factory=list, max_length=8)
    match_stage: MatchStage | None = None
    evidence: list[Evidence] = Field(min_length=1, max_length=100)

    @field_validator("data_cutoff")
    @classmethod
    def data_cutoff_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("data_cutoff must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_context(self) -> ReportRequest:
        ids = [item.id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence IDs must be unique")
        if any(item.published_at > self.data_cutoff for item in self.evidence):
            raise ValueError("evidence cannot be newer than data_cutoff")
        if self.report_type == ReportType.MATCH_PREDICTION and not self.match_stage:
            raise ValueError("match_stage is required for match prediction reports")
        if self.report_type != ReportType.MATCH_PREDICTION and self.match_stage:
            raise ValueError("match_stage is only valid for match prediction reports")
        return self


class ConsumerReportRequest(BaseModel):
    report_type: ReportType
    subject: str = Field(min_length=3, max_length=300)
    report_date: date
    length: ReportLength = ReportLength.STANDARD
    focus: list[str] = Field(default_factory=list, max_length=8)
    match_stage: MatchStage | None = None

    @model_validator(mode="after")
    def validate_context(self) -> ConsumerReportRequest:
        if self.report_type == ReportType.MATCH_PREDICTION and not self.match_stage:
            raise ValueError("match_stage is required for match prediction reports")
        if self.report_type != ReportType.MATCH_PREDICTION and self.match_stage:
            raise ValueError("match_stage is only valid for match prediction reports")
        return self


class ReportSection(BaseModel):
    heading: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    evidence_ids: list[str] = Field(min_length=1)


class EvidenceFactor(BaseModel):
    claim: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1)


class QualificationProbability(BaseModel):
    home: float = Field(ge=0, le=1)
    away: float = Field(ge=0, le=1)


class MatchPrediction(BaseModel):
    home_win: float = Field(ge=0, le=1)
    draw: float = Field(ge=0, le=1)
    away_win: float = Field(ge=0, le=1)
    qualification: QualificationProbability | None = None
    scorelines: list[str] = Field(min_length=1, max_length=3)
    supporting_factors: list[EvidenceFactor] = Field(min_length=1)
    counter_factors: list[EvidenceFactor] = Field(min_length=1)
    unknowns: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]


class GeneratedReport(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    executive_summary: str = Field(min_length=1, max_length=3000)
    sections: list[ReportSection] = Field(min_length=1, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=12)
    prediction: MatchPrediction | None = None


class TokenUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class PredictionOpinion(BaseModel):
    role: Literal["form_analyst", "skeptic"]
    home_win: float = Field(ge=0, le=1)
    draw: float = Field(ge=0, le=1)
    away_win: float = Field(ge=0, le=1)
    key_claims: list[EvidenceFactor] = Field(min_length=1, max_length=6)
    unknowns: list[str] = Field(default_factory=list, max_length=8)
    confidence: Literal["low", "medium", "high"]


class ReportResponse(BaseModel):
    id: str
    status: Literal["completed"] = "completed"
    provider: str
    model: str
    prompt_version: str
    data_cutoff: datetime
    generated_at: datetime
    attempts: int = Field(ge=1, le=5)
    usage: TokenUsage
    report: GeneratedReport
