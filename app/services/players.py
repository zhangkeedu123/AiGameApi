from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException

from app.security import token_digest

PLAYER_SOURCES = {"wechat": "微信", "douyin": "抖音"}
PROFILE_FIELDS = "id, nickname, source, openid, last_login_at, login_count, avatar_url"


def platform_fields(source, openid, avatar_url):
    source = source.strip() or None
    openid = openid.strip() or None
    avatar_url = avatar_url.strip() or None
    if source is not None and source not in PLAYER_SOURCES:
        raise HTTPException(422, "source 仅接受 wechat 或 douyin")
    if openid is not None and (source is None or any(c.isspace() or c == "\0" for c in openid)):
        raise HTTPException(422, "openid 需要对应的平台来源，且不能包含空白或空字符")
    if avatar_url is not None:
        try:
            url = urlsplit(avatar_url)
            if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                    or any(c.isspace() or ord(c) < 32 for c in avatar_url)):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "avatar_url 必须为有效的 HTTP 或 HTTPS 地址") from None
    return source, openid, avatar_url


async def create_player(db, nickname, source, openid, avatar_url):
    user_id = uuid4()
    await db.execute(
        """INSERT INTO users (id, nickname, active, created_at, source, openid, avatar_url, login_count)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
        user_id, nickname, True, datetime.now(UTC), source, openid, avatar_url, 0,
    )
    return user_id


async def update_platform(db, user_id, source, openid, avatar_url):
    updated = await db.fetchval(
        "UPDATE users SET source = $2, openid = $3, avatar_url = $4 WHERE id = $1 RETURNING id",
        user_id, source, openid, avatar_url,
    )
    if updated is None:
        raise HTTPException(404, "玩家不存在")


async def get_profile(db, user_id):
    row = await db.fetchrow(f"SELECT {PROFILE_FIELDS} FROM users WHERE id = $1", user_id)
    if row is None:
        raise HTTPException(404, "玩家不存在")
    return {**row, "login_count": row["login_count"] or 0}


async def record_login(db, user_id):
    async with db.acquire() as conn, conn.transaction():
        # 同一玩家的并发登录依次累计，旧玩家的空计数从零开始。
        row = await conn.fetchrow("SELECT login_count FROM users WHERE id = $1 AND active FOR UPDATE", user_id)
        if row is None:
            raise HTTPException(401, "玩家不可用")
        updated = await conn.fetchrow(
            f"""UPDATE users SET last_login_at = $2, login_count = $3 WHERE id = $1
                RETURNING {PROFILE_FIELDS}""", user_id, datetime.now(UTC), (row["login_count"] or 0) + 1,
        )
    return dict(updated)


async def update_token(db, user_id, token=None):
    async with db.acquire() as conn, conn.transaction():
        # 对同一玩家的签发、轮换和撤销按事务顺序执行。
        if not await conn.fetchval("SELECT id FROM users WHERE id = $1 FOR UPDATE", user_id):
            raise HTTPException(404, "玩家不存在")
        changed_at = datetime.now(UTC)
        await conn.execute(
            """UPDATE user_tokens SET revoked_at = $2
               WHERE user_id = $1 AND revoked_at IS NULL""", user_id, changed_at,
        )
        if token:
            await conn.execute(
                """INSERT INTO user_tokens (id, user_id, token_digest, issued_at)
                   VALUES ($1, $2, $3, $4)""",
                uuid4(), user_id, token_digest(token), changed_at,
            )
