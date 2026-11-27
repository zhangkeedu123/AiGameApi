from io import BytesIO
from secrets import token_urlsafe
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from starlette.concurrency import run_in_threadpool

from app.security import check_csrf, csrf_token, verify_password
from app.services.artwork import generate
from app.services.campaign import leaderboard, month_date
from app.services.collection import CATEGORY, SHIPMENT_STATUS, update_shipment
from app.services.levels import PNG_LIMIT, publish_level, store_level, update_level, upload_level
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
               category, config->>'story' AS story, sort_order, level_number, atlas_version, created_at,
               original_key IS NOT NULL AS has_original,
               jsonb_array_length(config->'shards') - coalesce((config->>'decoy_count')::int, 0) AS pieces,
               coalesce((config->>'decoy_count')::int, 0) AS decoy_count,
               coalesce((config->>'rotate_count')::int,
                   CASE WHEN (config->>'rotations')::int > 0 THEN ceil(jsonb_array_length(config->'shards') * 0.65)::int ELSE 0 END
               ) AS rotate_count
        FROM levels ORDER BY level_number NULLS LAST, category, created_at DESC, id
    """)
    return [dict(row) for row in rows]


@router.post("/levels", status_code=201, dependencies=protected)
async def upload(request: Request, atlas: UploadFile = File(), config: UploadFile = File(),
                 category: CATEGORY = Form("normal"), level_number: int | None = Form(None, ge=1, le=1000000)):
    state = request.app.state
    return {"id": await upload_level(state.db, state.settings.upload_dir, atlas, config, category, level_number)}


@router.post("/levels/generate", status_code=201, dependencies=protected)
async def upload_artwork(request: Request, image: UploadFile = File(), name: str = Form(max_length=120),
                         pieces: int = Form(ge=3, le=64), rotate: bool = Form(False),
                         category: CATEGORY = Form("normal"), level_number: int | None = Form(None, ge=1, le=1000000),
                         rotate_count: int | None = Form(None, ge=0, le=64), decoy_count: int = Form(0, ge=0, le=16),
                         story: str = Form("", max_length=4000)):
    state = request.app.state
    original = await image.read(PNG_LIMIT + 1)
    turns = rotate_count if rotate_count is not None else ((pieces * 65 + 99) // 100 if rotate else 0)
    png, config = await run_in_threadpool(generate, original, name, pieces, turns, decoy_count, story)
    key = await store_level(state.db, state.settings.upload_dir, png, config, category, level_number, original)
    return {"id": key, "bytes": len(png)}


@router.get("/levels/{level_id}/atlas.png", dependencies=protected)
async def preview(request: Request, level_id: UUID, v: int | None = Query(None, ge=1)):
    row = await preview_version(request.app.state.db, level_id, v)
    return FileResponse(request.app.state.settings.upload_dir / row["storage_key"], media_type="image/png")


@router.get("/levels/{level_id}/preview", dependencies=protected)
async def preview_config(request: Request, level_id: UUID, v: int | None = Query(None, ge=1)):
    row = await preview_version(request.app.state.db, level_id, v)
    return {**row["config"], "atlas_version": row["atlas_version"]}


async def preview_version(db, level_id, version):
    row = await db.fetchrow("SELECT config, storage_key, atlas_version FROM levels WHERE id = $1", level_id)
    if row is not None and version is not None and version != row["atlas_version"]:
        row = await db.fetchrow("""
            SELECT config, storage_key, version AS atlas_version
            FROM level_versions WHERE level_id = $1 AND version = $2
        """, level_id, version)
    if row is None:
        raise HTTPException(404, "关卡或图集版本不存在")
    return row


@router.post("/levels/{level_id}/publish", dependencies=protected)
async def publish(request: Request, level_id: UUID):
    await publish_level(request.app.state.db, level_id)
    return {"ok": True}


@router.patch("/levels/{level_id}", dependencies=protected)
async def edit_level(request: Request, level_id: UUID, name: str = Form(max_length=120),
                     category: CATEGORY = Form(), story: str = Form("", max_length=4000),
                     level_number: int | None = Form(None, ge=1, le=1000000),
                     atlas_version: int | None = Form(None, ge=1), pieces: int | None = Form(None, ge=3, le=64),
                     rotate_count: int = Form(0, ge=0, le=64), decoy_count: int = Form(0, ge=0, le=16),
                     image: UploadFile | None = File(None)):
    name, story = name.strip(), story.strip()
    if not name or "\0" in name or "\0" in story:
        raise HTTPException(422, "请填写有效的名称和介绍")
    state = request.app.state
    directory = state.settings.upload_dir
    original, generated = None, None
    if pieces is not None:
        row = await state.db.fetchrow("SELECT * FROM levels WHERE id = $1", level_id)
        if row is None:
            raise HTTPException(404, "关卡不存在")
        atlas_version = atlas_version or row["atlas_version"]
        if image:
            original = await image.read(PNG_LIMIT + 1)
            raw = original
        elif row["original_key"]:
            raw = await run_in_threadpool((directory / row["original_key"]).read_bytes)
        else:
            raw = await run_in_threadpool(restore_source, directory / row["storage_key"], row["config"]["poster"])
        generated = await run_in_threadpool(generate, raw, name, pieces, rotate_count, decoy_count, story)
    version = await update_level(state.db, directory, level_id, atlas_version, name, category, level_number, story,
                                 generated, original)
    return {"ok": True, "atlas_version": version}


def restore_source(path, poster):
    x, y, w, h = poster
    stream = BytesIO()
    with Image.open(path) as atlas:
        atlas.crop((x, y, x + w, y + h)).convert("RGBA").save(stream, format="PNG")
    return stream.getvalue()


@router.get("/redemptions", dependencies=protected)
async def redemptions(request: Request, status: SHIPMENT_STATUS | None = None,
                      page: int = Query(1, ge=1, le=100000), size: int = Query(20, ge=1, le=100)):
    db = request.app.state.db
    async with db.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        total = await conn.fetchval("""
            SELECT count(*) FROM porcelain_redemptions WHERE ($1::text IS NULL OR status = $1)
        """, status)
        rows = await conn.fetch("""
            SELECT r.id, r.user_id, u.nickname, u.source, r.recipient, r.phone, r.address,
                   r.status, r.carrier, r.tracking_number, r.created_at, r.updated_at, r.reward_month
            FROM porcelain_redemptions r JOIN users u ON u.id = r.user_id
            WHERE ($1::text IS NULL OR r.status = $1)
            ORDER BY r.created_at DESC, r.id LIMIT $2 OFFSET $3
        """, status, size, (page - 1) * size)
    return {"items": [dict(row) for row in rows], "total": total}


@router.patch("/redemptions/{claim_id}", dependencies=protected)
async def edit_redemption(request: Request, claim_id: UUID, status: SHIPMENT_STATUS = Form(),
                          carrier: str = Form("", max_length=80), tracking_number: str = Form("", max_length=100)):
    return await update_shipment(request.app.state.db, claim_id, status, carrier, tracking_number)


@router.delete("/levels/{level_id}", dependencies=protected)
async def remove_level(request: Request, level_id: UUID):
    state = request.app.state
    async with state.db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT status, storage_key, original_key FROM levels WHERE id = $1 FOR UPDATE", level_id)
        old_assets = []
        if row is None:
            raise HTTPException(404, "关卡不存在")
        if row["status"] == "published":
            await conn.execute("""
                UPDATE levels SET config = config || '{"archived": true}'::jsonb WHERE id = $1
            """, level_id)
        else:
            old_assets = [item["storage_key"] for item in await conn.fetch(
                "SELECT storage_key FROM level_versions WHERE level_id = $1", level_id)]
            old_assets += [row["storage_key"], row["original_key"]]
            await conn.execute("DELETE FROM levels WHERE id = $1", level_id)
    for key in filter(None, old_assets):
        await run_in_threadpool((state.settings.upload_dir / key).unlink, missing_ok=True)
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


@router.get("/monthly-ranking", dependencies=protected)
async def admin_monthly_ranking(request: Request, month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
                                source: str | None = Query(None, pattern="^(wechat|douyin)$")):
    return await leaderboard(request.app.state.db, month_date(month), source)


@router.get("/ranking", dependencies=protected)
async def ranking(request: Request, source: str | None = Query(None, pattern="^(wechat|douyin)$")):
    rows = await request.app.state.db.fetch("""
        SELECT u.id, u.nickname, u.source, count(*) AS completions, max(p.first_completed_at) AS last_completion
        FROM user_level_progress p JOIN users u ON u.id = p.user_id
        WHERE p.first_completed_at IS NOT NULL AND u.active AND ($1::text IS NULL OR u.source = $1)
        GROUP BY u.id ORDER BY completions DESC, last_completion, u.id LIMIT 100
    """, source)
    return [{"rank": index, **row} for index, row in enumerate(rows, 1)]
