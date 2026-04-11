from pydantic import BaseModel, Field, field_validator


class AuditCreateBody(BaseModel):
    primary_domain: str = Field(..., min_length=1, max_length=512)
    competitor_domains: list[str] = Field(default_factory=list)
    industry: str | None = Field(default=None, max_length=200)

    @field_validator("competitor_domains", mode="before")
    @classmethod
    def at_most_three_competitors(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()][:3]


class CompetitorScoreOut(BaseModel):
    domain: str
    score: float
    label: str | None = None


class PromptRowOut(BaseModel):
    id: str
    text: str
    mentioned: bool
    score: float
    intent: str | None = None
    explanation: str | None = None
    competitor_mentions: list[str] = Field(default_factory=list)


class ContentBriefOut(BaseModel):
    title: str
    body: str


class RecommendationOut(BaseModel):
    id: str
    title: str
    rationale: str
    priority_score: float
    brief: ContentBriefOut | None = None


class AuditSummaryOut(BaseModel):
    id: str
    primary_domain: str
    status: str
    stage: str
    progress_percent: int
    visibility_score: float | None
    target_mention_rate: float | None = None
    created_at: str


class AuditDetailOut(AuditSummaryOut):
    industry: str | None
    competitor_domains: list[str]
    competitor_scores: list[CompetitorScoreOut]
    prompts: list[PromptRowOut]
    recommendations: list[RecommendationOut]
    crawl_summary: dict = Field(default_factory=dict)
    error_message: str | None = None


class BriefResponse(BaseModel):
    recommendation_id: str
    brief: ContentBriefOut
