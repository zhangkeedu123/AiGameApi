from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

Coordinate = Annotated[FiniteFloat, Field(strict=True)]
Index = Annotated[int, Field(ge=0)]
Turn = Annotated[int, Field(ge=0, le=3)]
Point = tuple[Coordinate, Coordinate]
Box = tuple[Coordinate, Coordinate, Coordinate, Coordinate]
Rect = tuple[Index, Index, Index, Index]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Shard(InputModel):
    cx: Coordinate
    cy: Coordinate
    box: Box
    poly: list[Coordinate]
    sprite: Rect


class Seam(InputModel):
    ids: tuple[Index, Index]
    poly: list[Coordinate]


class LevelConfig(InputModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    difficulty: Annotated[str, Field(min_length=1, max_length=80)]
    story: Annotated[str, Field(max_length=4000)]
    rotations: Turn
    snapDistance: Annotated[Coordinate, Field(gt=0)]
    poster: Rect
    outline: list[Coordinate]
    shards: Annotated[list[Shard], Field(min_length=1, max_length=256)]
    seams: list[Seam]
    points: Annotated[list[Point], Field(min_length=3, max_length=3)]


class Group(InputModel):
    ids: Annotated[list[Index], Field(min_length=1, max_length=256)]
    x: Annotated[Coordinate, Field(ge=0, le=400)]
    y: Annotated[Coordinate, Field(ge=0, le=590)]
    turn: Turn


class ProgressInput(InputModel):
    groups: Annotated[list[Group], Field(min_length=1, max_length=256)]
    paint: Annotated[list[Index], Field(max_length=3)]
    revision: Annotated[int, Field(ge=0, le=9223372036854775807)]


class PlayerProfile(BaseModel):
    id: UUID
    nickname: str | None
    source: str | None
    openid: str | None
    last_login_at: datetime | None
    login_count: int
    avatar_url: str | None
