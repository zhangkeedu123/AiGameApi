from secrets import token_urlsafe
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.security import check_csrf, csrf_token, verify_password
from app.services.artwork import generate
from app.services.levels import PNG_LIMIT, publish_level, store_level, upload_level
from app.services.players import create_player, get_profile, platform_fields, update_platform, update_token
from app.services.progress import read_progress

router = APIRouter(prefix="/admin", include_in_schema=False)


async def session_user(request):
    admin_id = request.session.get("admin_id")
    if admin_id:
        return await request.app.state.db.fetchval(
            "SELECT username FROM admins WHERE id = $1 AND active", UUID(admin_id))
    return None


async def require_admin(request: Request):
    if not await session_user(request):
        raise HTTPException(401, "需要管理员登录")
    if request.method != "GET":
        check_csrf(request, request.headers.get("X-CSRF-Token", ""))


protected = [Depends(require_admin)]


@router.get("/session")
async def session(request: Request):
    return {"username": await session_user(request), "csrf": csrf_token(request)}


@router.post("/login")
async def login(request: Request, username: str = Form(max_length=120), password: str = Form(max_length=256)):
    check_csrf(request, request.headers.get("X-CSRF-Token", ""))
    if "\0" in username:
        raise HTTPException(401, "账号或密码错误")
    row = await request.app.state.db.fetchrow(
        "SELECT id, password_hash FROM admins WHERE username = $1 AND active", username.strip()
    )
    if row is None or not await verify_password(password, row["password_hash"]):
        raise HTTPException(401, "账号或密码错误")
    request.session.clear()
    request.session["admin_id"] = str(row["id"])
    return {"username": username.strip(), "csrf": csrf_token(request)}


@router.post("/logout", dependencies=protected)
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/overview", dependencies=protected)
async def overview(request: Request):
    row = await request.app.state.db.fetchrow("""
        SELECT (SELECT count(*) FROM levels WHERE status = 'published'
                AND config->>'archived' IS DISTINCT FROM 'true') AS published,
               (SELECT count(*) FROM levels WHERE status = 'draft') AS drafts,
               (SELECT count(*) FROM users) AS players,
               (SELECT count(*) FROM user_level_progress WHERE first_completed_at IS NOT NULL) AS completions
    """)
    return dict(row)


@router.get("/levels", dependencies=protected)
async def levels(request: Request):
    rows = await request.app.state.db.fetch("""
        SELECT id, name, CASE WHEN config->>'archived' = 'true' THEN 'archived' ELSE status END AS status,
               sort_order, created_at, jsonb_array_length(config->'shards') AS pieces
        FROM levels ORDER BY created_at DESC, id
    """)
    return [dict(row) for row in rows]


@router.post("/levels", status_code=201, dependencies=protected)
async def upload(request: Request, atlas: UploadFile = File(), config: UploadFile = File()):
    state = request.app.state
    return {"id": await upload_level(state.db, state.settings.upload_dir, atlas, config)}


@router.post("/levels/generate", status_code=201, dependencies=protected)
async def upload_artwork(request: Request, image: UploadFile = File(), name: str = Form(max_length=120),
                         pieces: int = Form(ge=3, le=64), rotate: bool = Form(False)):
    state = request.app.state
    png, config = await run_in_threadpool(generate, await image.read(PNG_LIMIT + 1), name, pieces, rotate)
    key = await store_level(state.db, state.settings.upload_dir, png, config)
    return {"id": key, "bytes": len(png)}


@router.get("/levels/{level_id}/atlas.png", dependencies=protected)
async def preview(request: Request, level_id: UUID):
    row = await request.app.state.db.fetchrow("SELECT storage_key FROM levels WHERE id = $1", level_id)
    if not row:
        raise HTTPException(404, "关卡不存在")
    return FileResponse(request.app.state.settings.upload_dir / row["storage_key"], media_type="image/png")


@router.post("/levels/{level_id}/publish", dependencies=protected)
async def publish(request: Request, level_id: UUID):
    await publish_level(request.app.state.db, level_id)
    return {"ok": True}


@router.delete("/levels/{level_id}", dependencies=protected)
async def remove_level(request: Request, level_id: UUID):
    state = request.app.state
    async with state.db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT status, storage_key FROM levels WHERE id = $1 FOR UPDATE", level_id)
        if row is None:
            raise HTTPException(404, "关卡不存在")
        if row["status"] == "published":
            await conn.execute("""
                UPDATE levels SET config = config || '{"archived": true}'::jsonb WHERE id = $1
            """, level_id)
        else:
            await conn.execute("DELETE FROM levels WHERE id = $1", level_id)
    if row["status"] == "draft":
        await run_in_threadpool((state.settings.upload_dir / row["storage_key"]).unlink, missing_ok=True)
    return {"ok": True}


@router.get("/users", dependencies=protected)
async def users(request: Request, q: str = Query("", max_length=80), page: int = Query(1, ge=1, le=100000),
                size: int = Query(20, ge=1, le=100)):
    db = request.app.state.db
    condition = "strpos(lower(coalesce(nickname, '')), lower($1)) > 0"
    async with db.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        total = await conn.fetchval(f"SELECT count(*) FROM users WHERE {condition}", q)
        rows = await conn.fetch(f"""
            SELECT id, nickname, source, last_login_at, login_count, active
            FROM users WHERE {condition} ORDER BY created_at DESC, id LIMIT $2 OFFSET $3
        """, q, size, (page - 1) * size)
    return {"items": [dict(row) for row in rows], "total": total}


@router.post("/users", status_code=201, dependencies=protected)
async def add_user(request: Request, nickname: str = Form(max_length=80), source: str = Form("", max_length=16),
                   openid: str = Form("", max_length=255), avatar_url: str = Form("", max_length=2048)):
    nickname = nickname.strip()
    if not nickname or "\0" in nickname:
        raise HTTPException(422, "请填写有效昵称")
    return {"id": await create_player(request.app.state.db, nickname, *platform_fields(source, openid, avatar_url))}


@router.get("/users/{user_id}", dependencies=protected)
async def user(request: Request, user_id: UUID):
    db = request.app.state.db
    profile = await get_profile(db, user_id)
    progress = await read_progress(db, user_id)
    names = {row["id"]: row["name"] for row in await db.fetch("SELECT id, name FROM levels")}
    return {"profile": profile, "current_level": names.get(progress["current_level_id"]), "progress": [
        {"name": names.get(item["level_id"]), **item} for item in progress["progress"]
    ]}


@router.patch("/users/{user_id}", dependencies=protected)
async def edit_user(request: Request, user_id: UUID, source: str = Form("", max_length=16),
                    openid: str = Form("", max_length=255), avatar_url: str = Form("", max_length=2048)):
    await update_platform(request.app.state.db, user_id, *platform_fields(source, openid, avatar_url))
    return {"ok": True}


@router.post("/users/{user_id}/tokens", dependencies=protected)
async def issue_token(request: Request, user_id: UUID):
    token = token_urlsafe(32)
    await update_token(request.app.state.db, user_id, token)
    return {"token": token}


@router.delete("/users/{user_id}/tokens", dependencies=protected)
async def revoke_token(request: Request, user_id: UUID):
    await update_token(request.app.state.db, user_id)
    return {"ok": True}


@router.get("/ranking", dependencies=protected)
async def ranking(request: Request, source: str | None = Query(None, pattern="^(wechat|douyin)$")):
    rows = await request.app.state.db.fetch("""
        SELECT u.id, u.nickname, u.source, count(*) AS completions, max(p.first_completed_at) AS last_completion
        FROM user_level_progress p JOIN users u ON u.id = p.user_id
        WHERE p.first_completed_at IS NOT NULL AND u.active AND ($1::text IS NULL OR u.source = $1)
        GROUP BY u.id ORDER BY completions DESC, last_completion, u.id LIMIT 100
    """, source)
    return [{"rank": index, **row} for index, row in enumerate(rows, 1)]
