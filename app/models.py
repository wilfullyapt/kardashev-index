from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, JSON
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