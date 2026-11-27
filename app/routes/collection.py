from fastapi import APIRouter, HTTPException, Request

from app.routes.api import Player
from app.services.collection import Completion, Redemption, collection, complete_daily, redeem, start_daily

router = APIRouter(prefix="/api/v1/me", tags=["每日一瓷"])


@router.post("/daily/start", summary="开启北京时间当天的每日一瓷")
async def daily_start(request: Request, user_id: Player):
    state = request.app.state
    return await start_daily(state.db, user_id, state.settings.upload_dir)


@router.post("/daily/complete", summary="提交每日一瓷并自动收藏")
async def daily_complete(request: Request, user_id: Player, data: Completion):
    return await complete_daily(request.app.state.db, user_id, data)


@router.get("/collection", summary="读取个人收藏、兑换余量和物流进度")
async def my_collection(request: Request, user_id: Player):
    return await collection(request.app.state.db, user_id)


@router.post("/collection/redemptions", status_code=201, summary="提交实物瓷器兑换")
async def create_redemption(request: Request, user_id: Player, data: Redemption):
    if request.app.state.settings.cookie_secure and request.url.scheme != "https":
        raise HTTPException(403, "请使用 HTTPS 提交收货信息")
    return await redeem(request.app.state.db, user_id, data)

