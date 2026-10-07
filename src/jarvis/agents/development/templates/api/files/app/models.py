"""Request and response models (validated by pydantic)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ItemBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120, examples=["Notebook"])
    description: str = Field("", max_length=2000)
    price: float = Field(0, ge=0, le=1_000_000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    done: bool = False

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, tags: list[str]) -> list[str]:
        seen: list[str] = []
        for tag in tags:
            tag = tag.strip().lower()[:30]
            if tag and tag not in seen:
                seen.append(tag)
        return seen


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    """Partial update: only the fields that are sent change."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    price: float | None = Field(None, ge=0, le=1_000_000)
    tags: list[str] | None = Field(None, max_length=10)
    done: bool | None = None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, tags: list[str] | None) -> list[str] | None:
        return None if tags is None else ItemBase.clean_tags(tags)


class Item(ItemBase):
    id: int
    created_at: datetime
    updated_at: datetime


class ItemPage(BaseModel):
    items: list[Item]
    total: int
    limit: int
    offset: int
