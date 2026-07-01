from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class ReportType(StrEnum):
    DAILY_FOOTBALL_DIGEST = "daily_football_digest"
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
    source_id: str = Field(default="legacy-source", min_length=1, max_length=100)
    trust_tier: str = Field(default="S2", min_length=1, max_length=40)
    evidence_kind: Literal["official", "verified", "structured", "discovery"] = (
        "verified"
    )
    verification_status: Literal[
        "official", "corroborated", "publisher_report", "unverified_lead"
    ] = "publisher_report"
    story_cluster_id: str | None = Field(default=None, max_length=100)
    source_independence_key: str | None = Field(default=None, max_length=100)

    @field_validator("published_at")
    @classmethod
    def published_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        return value


class RecentMatchSample(BaseModel):
    goals_for: int = Field(ge=0, le=20)
    goals_against: int = Field(ge=0, le=20)
    neutral: bool = True


class MatchModelContext(BaseModel):
    home_team: str = Field(min_length=1, max_length=120)
    away_team: str = Field(min_length=1, max_length=120)
    home_recent: list[RecentMatchSample] = Field(default_factory=list, max_length=20)
    away_recent: list[RecentMatchSample] = Field(default_factory=list, max_length=20)
    home_elo: float | None = Field(default=None, ge=500, le=3000)
    away_elo: float | None = Field(default=None, ge=500, le=3000)
    evidence_ids: list[str] = Field(default_factory=list)


class ReportRequest(BaseModel):
    report_type: ReportType
    subject: str = Field(min_length=1, max_length=300)
    report_date: date
    data_cutoff: datetime
    length: ReportLength = ReportLength.STANDARD
    focus: list[str] = Field(default_factory=list, max_length=8)
    match_stage: MatchStage | None = None
    evidence: list[Evidence] = Field(min_length=1, max_length=100)
    match_context: MatchModelContext | None = None

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
        if self.report_type != ReportType.MATCH_PREDICTION and self.match_context:
            raise ValueError("match_context is only valid for match prediction reports")
        if self.match_context and set(self.match_context.evidence_ids) - set(ids):
            raise ValueError("match_context cites unknown evidence IDs")
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


class ExternalPrediction(BaseModel):
    source_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    home_win: float | None = Field(default=None, ge=0, le=1)
    draw: float | None = Field(default=None, ge=0, le=1)
    away_win: float | None = Field(default=None, ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_probability_set(self) -> ExternalPrediction:
        values = [self.home_win, self.draw, self.away_win]
        supplied = [value is not None for value in values]
        if any(supplied) and not all(supplied):
            raise ValueError("external prediction probabilities must be all or none")
        total = sum(value for value in values if value is not None)
        if all(supplied) and abs(total - 1) > 0.001:
            raise ValueError("external prediction probabilities must equal 1")
        return self


class PlayerMetric(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=80)


class PlayerSpotlight(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    media_search_name: str | None = Field(default=None, max_length=120)
    related_clubs: list[str] = Field(default_factory=list, max_length=4)
    position: str | None = Field(default=None, max_length=80)
    narrative: str = Field(min_length=1, max_length=1200)
    metrics: list[PlayerMetric] = Field(default_factory=list, max_length=8)
    evidence_ids: list[str] = Field(min_length=1)


class MatchTimelineEvent(BaseModel):
    minute: str = Field(pattern=r"^(?:\d{1,3}(?:\+\d{1,2})?|赛前|半场|终场)$")
    event_type: Literal[
        "goal", "own_goal", "penalty", "card", "substitution", "key_moment"
    ]
    player: str | None = Field(default=None, max_length=120)
    team: str | None = Field(default=None, max_length=120)
    score_after: str | None = Field(
        default=None, pattern=r"^\d{1,2}-\d{1,2}$"
    )
    description: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(min_length=1)


class MediaAsset(BaseModel):
    asset_type: Literal["image", "video"]
    title: str = Field(min_length=1, max_length=300)
    url: HttpUrl
    thumbnail_url: HttpUrl | None = None
    provider: str = Field(min_length=1, max_length=120)
    license: str = Field(min_length=1, max_length=120)
    attribution: str = Field(min_length=1, max_length=500)
    rights_status: Literal["approved", "review_required"]


class EditorialEnrichment(BaseModel):
    player_spotlights: list[PlayerSpotlight] = Field(
        default_factory=list, max_length=6
    )
    match_timeline: list[MatchTimelineEvent] = Field(
        default_factory=list, max_length=20
    )
    media_assets: list[MediaAsset] = Field(default_factory=list, max_length=8)


class StatisticalBaseline(BaseModel):
    method: Literal["poisson", "elo_poisson"]
    home_win: float = Field(ge=0, le=1)
    draw: float = Field(ge=0, le=1)
    away_win: float = Field(ge=0, le=1)
    expected_home_goals: float = Field(ge=0, le=10)
    expected_away_goals: float = Field(ge=0, le=10)
    sample_size_home: int = Field(ge=0)
    sample_size_away: int = Field(ge=0)
    evidence_ids: list[str] = Field(default_factory=list)


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
    external_predictions: list[ExternalPrediction] = Field(
        default_factory=list, max_length=6
    )
    statistical_baseline: StatisticalBaseline | None = None


class GeneratedReport(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    executive_summary: str = Field(min_length=1, max_length=3000)
    sections: list[ReportSection] = Field(min_length=1, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=12)
    prediction: MatchPrediction | None = None
    enrichment: EditorialEnrichment = Field(default_factory=EditorialEnrichment)


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


class DeskBrief(BaseModel):
    desk: Literal["match_news", "transfer_market"]
    key_items: list[EvidenceFactor] = Field(min_length=1, max_length=12)
    rumor_items: list[EvidenceFactor] = Field(default_factory=list, max_length=12)
    conflicts: list[str] = Field(default_factory=list, max_length=8)
    unknowns: list[str] = Field(default_factory=list, max_length=8)


class DeskDraft(BaseModel):
    desk: Literal["match_news", "transfer_market"]
    heading: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    sections: list[ReportSection] = Field(min_length=1, max_length=6)
    warnings: list[str] = Field(default_factory=list, max_length=8)


class ReportResponse(BaseModel):
    id: str
    status: Literal["completed"] = "completed"
    provider: str
    model: str
    prompt_version: str
    data_cutoff: datetime
    generated_at: datetime
    attempts: int = Field(ge=1, le=8)
    usage: TokenUsage
    report: GeneratedReport
