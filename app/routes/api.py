from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.schemas import PlayerProfile, ProgressInput
from app.security import player
from app.services.players import get_profile, record_login
from app.services.progress import read_progress, save_progress

router = APIRouter(prefix="/api/v1")
Player = Annotated[UUID, Depends(player)]


@router.get("/levels")
async def list_levels(request: Request):
    rows = await request.app.state.db.fetch(
        """SELECT id, name, sort_order, published_at, config->>'difficulty' AS difficulty
           FROM levels WHERE status = 'published' ORDER BY sort_order"""
    )
    return [dict(row) for row in rows]


async def published_level(request, level_id):
    row = await request.app.state.db.fetchrow(
        """SELECT id, sort_order, published_at, config, storage_key
           FROM levels WHERE id = $1 AND status = 'published'""", level_id,
    )
    if row is None:
        raise HTTPException(404, "已发布关卡不存在")
    return row


@router.get("/levels/{level_id}")
async def level_detail(request: Request, level_id: UUID):
    row = await published_level(request, level_id)
    return {
        **row["config"], "id": row["id"], "sort_order": row["sort_order"],
        "published_at": row["published_at"],
        "image": str(request.url_for("public_atlas", level_id=level_id)),
    }


@router.get("/levels/{level_id}/atlas.png", name="public_atlas")
async def public_atlas(request: Request, level_id: UUID):
    row = await published_level(request, level_id)
    # 通过数据库查到的服务端存储键读取文件，上传目录不挂载为静态目录。
    path = request.app.state.settings.upload_dir / row["storage_key"]
    return FileResponse(path, media_type="image/png", headers={
        "Cache-Control": "public, max-age=86400, immutable", "X-Content-Type-Options": "nosniff"
    })


@router.get("/me", response_model=PlayerProfile)
async def player_profile(request: Request, user_id: Player):
    return await get_profile(request.app.state.db, user_id)


@router.post("/me/login", response_model=PlayerProfile)
async def player_login(request: Request, user_id: Player):
    # 联调令牌确定身份。平台来源和 OpenID 由后台资料提供。
    return await record_login(request.app.state.db, user_id)


@router.get("/me/progress")
async def get_progress(request: Request, user_id: Player):
    return await read_progress(request.app.state.db, user_id)


@router.put("/me/progress/{level_id}")
async def put_progress(request: Request, user_id: Player, level_id: UUID, data: ProgressInput):
    return await save_progress(request.app.state.db, user_id, level_id, data)
