from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.routes.api import Player
from app.services.campaign import Finish, completed_levels, finish_campaign, leaderboard, month_date, start_campaign

router = APIRouter(prefix="/api/v1", tags=["闯关与月榜"])


@router.get("/me/campaign")
async def campaign_progress(request: Request, user_id: Player):
    return {"completed": await completed_levels(request.app.state.db, user_id)}


@router.post("/me/campaign/levels/{level_id}/start")
async def play_level(request: Request, user_id: Player, level_id: UUID):
    return await start_campaign(request.app.state.db, user_id, level_id)


@router.post("/me/campaign/attempts/{attempt_id}/complete")
async def complete_level(request: Request, user_id: Player, attempt_id: UUID, data: Finish):
    return await finish_campaign(request.app.state.db, user_id, attempt_id, data)


@router.get("/ranking")
async def monthly_ranking(request: Request, month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$")):
    return await leaderboard(request.app.state.db, month_date(month))
