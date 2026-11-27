from datetime import datetime
from typing import Annotated, Literal
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
    contours: list[list[Index]] | None = None


class Seam(InputModel):
    ids: tuple[Index, Index]
    poly: list[Coordinate]


class LevelConfig(InputModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    difficulty: Annotated[str, Field(min_length=1, max_length=80)]
    story: Annotated[str, Field(max_length=4000)]
    rotations: Turn
    rotate_count: Annotated[int, Field(ge=0, le=256)] | None = None
    decoy_count: Annotated[int, Field(ge=0, le=16)] = 0
    cutout: bool = False
    snapDistance: Annotated[Coordinate, Field(gt=0)]
    poster: Rect
    outline: list[Coordinate]
    contours: list[list[Index]] | None = None
    shards: Annotated[list[Shard], Field(min_length=1, max_length=256)]
    seams: list[Seam]
    points: Annotated[list[Point], Field(max_length=3)]


class Group(InputModel):
    ids: Annotated[list[Index], Field(min_length=1, max_length=256)]
    x: Annotated[Coordinate, Field(ge=-450, le=850)]
    y: Annotated[Coordinate, Field(ge=-450, le=1040)]
    turn: Turn


class ProgressInput(InputModel):
    groups: Annotated[list[Group], Field(min_length=1, max_length=256)]
    paint: Annotated[list[Index], Field(max_length=3)]
    assembly_only: bool = False
    revision: Annotated[int, Field(ge=0, le=9223372036854775807)]


class PlayerProfile(BaseModel):
    id: UUID
    nickname: str | None
    source: str | None
    openid: str | None
    last_login_at: datetime | None
    login_count: int
    avatar_url: str | None


class GuestLogin(InputModel):
    device_id: Annotated[str, Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")]
    nickname: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    source: Literal["wechat", "douyin"] = "wechat"


class GuestSession(PlayerProfile):
    token: str
