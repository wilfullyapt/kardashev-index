from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, JSON, Boolean, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .db import Base


class Company(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True, index=True)
    canonical_name = Column(String, unique=True, index=True)
    domain = Column(String, nullable=True)
    wikipedia_url = Column(String, nullable=True)
    crunchbase_id = Column(String, nullable=True)
    hq = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    employee_count = Column(String, nullable=True)
    funding_stage = Column(String, nullable=True)
    suggestion_attempts = Column(Integer, default=0, nullable=False)
    last_ingested_at = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Added in 0002 (entity resolution + published run pointer)
    official_name = Column(String, nullable=True)
    ticker = Column(String(16), nullable=True)
    exchange = Column(String(32), nullable=True)
    cik = Column(String(10), nullable=True)
    is_public = Column(Boolean, nullable=True)
    current_run_id = Column(Integer, nullable=True, index=True)
    # Added in 0005: resolved identity reused across runs (auto) or set by an admin (pinned)
    identity = Column(JSON, nullable=True)
    identity_status = Column(String(16), nullable=True)
    identity_resolved_at = Column(DateTime(timezone=True), nullable=True)

    scores = relationship("Score", back_populates="company")


class Suggestion(Base):
    __tablename__ = "suggestions"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    domain = Column(String, nullable=True, index=True)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())
    status = Column(String, default="pending")  # pending, approved, denied
    denial_reason = Column(Text, nullable=True)
    admin_id = Column(String, nullable=True)  # email or id


class Score(Base):
    __tablename__ = "scores"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    category = Column(String, index=True)  # ai_tech_acceleration, energy_kardashev, etc.
    score = Column(Float)
    justification = Column(Text)
    evidence_links = Column(JSON)
    model = Column(String)
    model_version = Column(String)
    judged_at = Column(DateTime(timezone=True), server_default=func.now())

    company = relationship("Company", back_populates="scores")


class IngestLog(Base):
    __tablename__ = "ingest_logs"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    suggestion_id = Column(Integer, ForeignKey("suggestions.id"), nullable=True)
    action = Column(String)
    details = Column(JSON)
    admin_id = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())


class ScoreHistory(Base):
    __tablename__ = "score_history"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    category = Column(String, index=True, nullable=False)
    score = Column(Float, nullable=False)
    justification = Column(Text)
    evidence_links = Column(JSON)
    model = Column(String)
    model_version = Column(String)
    judged_at = Column(DateTime(timezone=True))
    archived_at = Column(DateTime(timezone=True), server_default=func.now())

    company = relationship("Company")


# Shared constants
CATEGORIES = [
    "ai_tech_acceleration",
    "energy_kardashev",
    "market_competition",
    "regulatory_stance",
    "builder_culture",
    "overall",
]

# ===== v2: measured + judged pipeline (migration 0002) =====
# A judgment run is immutable history: every execution gets its own row, stages, sources,
# evidence, metrics and category scores. `companies.current_run_id` points at the published one.

ACTIVE_RUN_STATUSES = ("queued", "running")


class JudgmentRun(Base):
    __tablename__ = "judgment_runs"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="queued", index=True)  # queued|running|succeeded|failed
    trigger = Column(String(32))          # approve | rerun
    triggered_by = Column(String)
    suggestion_id = Column(Integer, nullable=True)
    attempt = Column(Integer, nullable=False, default=0)
    worker_id = Column(String(64))
    current_stage = Column(String(32))
    pipeline_version = Column(String(32))
    prompt_version = Column(String(32))
    rubric_version = Column(String(32))
    weights_version = Column(String(32))
    prompt_hash = Column(String(64))
    code_version = Column(String(64))     # deployed git commit (RENDER_GIT_COMMIT), 0003
    model = Column(String)                # requested (XAI_MODEL)
    model_returned = Column(String)       # what the API reported
    budget_usd = Column(Float)
    queued_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    heartbeat_at = Column(DateTime(timezone=True))
    duration_ms = Column(Integer)
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    reasoning_tokens = Column(Integer)
    tool_calls = Column(Integer)
    cost_usd = Column(Float)
    index_score = Column(Float)
    measured_score = Column(Float)
    judged_score = Column(Float)
    k_equivalent = Column(Float)
    avg_power_w = Column(Float)
    confidence = Column(Float)
    coverage = Column(Float)
    ranked = Column(Boolean)
    published = Column(Boolean)
    error_type = Column(String(64))
    error_message = Column(Text)
    summary = Column(JSON)
    # Added in 0005 (reliability)
    checkpoint = Column(JSON)                          # per-stage outputs for resume
    next_attempt_at = Column(DateTime(timezone=True))  # automatic retry not before
    error_class = Column(String(16))                   # transient | permanent
    degraded = Column(JSON)                            # list of degraded stages / reasons

    company = relationship("Company", foreign_keys=[company_id])
    stages = relationship("JudgmentStage", order_by="JudgmentStage.id", back_populates="run")
    category_scores = relationship("CategoryScore", order_by="CategoryScore.id", back_populates="run")

    __table_args__ = (
        # At most one queued/running run per company (enforced by the database).
        Index(
            "uq_judgment_runs_active_company", "company_id", unique=True,
            postgresql_where=status.in_(ACTIVE_RUN_STATUSES),
            sqlite_where=status.in_(ACTIVE_RUN_STATUSES),
        ),
    )


class JudgmentStage(Base):
    __tablename__ = "judgment_stages"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("judgment_runs.id"), nullable=False, index=True)
    seq = Column(Integer)
    stage = Column(String(32), nullable=False)
    attempt = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False)   # running|succeeded|failed|skipped|interrupted
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    duration_ms = Column(Integer)
    model = Column(String)
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    reasoning_tokens = Column(Integer)
    tool_calls = Column(Integer)
    cost_usd = Column(Float)
    error = Column(Text)
    detail = Column(JSON)

    run = relationship("JudgmentRun", back_populates="stages")


class Source(Base):
    __tablename__ = "sources"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("judgment_runs.id"), nullable=False, index=True)
    url = Column(Text, nullable=False)
    final_url = Column(Text)
    origin = Column(String(16))        # research | citation | edgar | resolve
    http_status = Column(Integer)
    content_type = Column(String)
    title = Column(Text)
    fetched_at = Column(DateTime(timezone=True))
    sha256 = Column(String(64))
    byte_size = Column(Integer)
    text = Column(Text)                # snapshot of extracted text (truncated)
    status = Column(String(16))        # ok | dead | soft_404 | blocked | error | skipped
    reject_reason = Column(Text)
    is_primary = Column(Boolean)
    archive_url = Column(Text)                 # Wayback Machine copy used instead of the original (0005)
    archive_timestamp = Column(String(14))


class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("judgment_runs.id"), nullable=False, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    category = Column(String(32))
    kind = Column(String(16))          # figure | claim
    claim = Column(Text)
    quote = Column(Text)
    quote_verified = Column(Boolean)
    context = Column(Text)
    metric_key = Column(String(48))
    value = Column(Float)
    unit = Column(String(24))
    period = Column(String(16))
    scope = Column(Text)
    rejected_reason = Column(Text)
    note = Column(Text)                        # e.g. why a figure was reclassified (0005)


class Metric(Base):
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("judgment_runs.id"), nullable=False, index=True)
    metric_key = Column(String(48), nullable=False)
    value = Column(Float)
    unit = Column(String(24))
    value_si = Column(Float)
    unit_si = Column(String(16))
    period = Column(String(16))
    method = Column(Text)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    evidence_ids = Column(JSON)


class CategoryScore(Base):
    __tablename__ = "category_scores"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("judgment_runs.id"), nullable=False, index=True)
    category = Column(String(32), nullable=False)
    family = Column(String(16))        # measured | judged
    score = Column(Float)              # null = insufficient data
    confidence = Column(Float)
    weight = Column(Float)
    rationale = Column(Text)
    evidence_ids = Column(JSON)
    inputs = Column(JSON)
    insufficient = Column(Boolean)
    rubric_version = Column(String(32))

    run = relationship("JudgmentRun", back_populates="category_scores")
