from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemoryCandidateCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="daily", max_length=120)


class MemoryCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    text: str
    source: str
    status: str
    created_at: datetime
    reviewed_at: datetime | None


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    instructions: str
    status: str
    version: int
    source_candidate_id: str | None
    history: list
    created_at: datetime
    updated_at: datetime


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    instructions: str | None = Field(default=None, min_length=1, max_length=12000)
