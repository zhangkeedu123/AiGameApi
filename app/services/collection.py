from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field, field_validator
from starlette.concurrency import run_in_threadpool

from app.schemas import Group, InputModel
from app.services.campaign import month_date, settle_months
from app.services.silhouette import with_silhouettes

CATEGORY = Literal["normal", "hard", "extreme", "daily"]
SHIPMENT_STATUS = Literal["pending", "processing", "shipped", "delivered"]
COST = 30
BEIJING = timezone(timedelta(hours=8))
FIRST_DAY = datetime(1970, 1, 1).date()
CLAIM_FIELDS = "id, request_id, status, carrier, tracking_number, created_at, updated_at, reward_month"


def today():
    return datetime.now(BEIJING).date()


class Completion(InputModel):
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    group: Group
    elapsed_ms: Annotated[int, Field(ge=0, le=86400000)]


class Redemption(InputModel):
    reward_month: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")] | None = None
    request_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{16,100}$")]
    recipient: Annotated[str, Field(min_length=1, max_length=60)]
    phone: Annotated[str, Field(pattern=r"^(?:\+86[ -]?)?1[3-9][0-9]{9}$")]
    address: Annotated[str, Field(min_length=6, max_length=240)]

    @field_validator("recipient", "phone", "address", mode="before")
    @classmethod
    def clean(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise ValueError("请填写有效的收货信息")
        return value


async def start_daily(db, user_id, upload_dir):
    day = today()
    async with db.acquire() as conn, conn.transaction():
        edition = await conn.fetchrow(
            "SELECT level_id FROM daily_editions WHERE day = $1", day
        )
        if edition is None:
            ids = await conn.fetch("""
                SELECT id, atlas_version FROM levels WHERE status = 'published' AND category = 'daily'
                    AND config->>'archived' IS DISTINCT FROM 'true' ORDER BY level_number, id
            """)
            if not ids:
                return {"date": day, "level": None}
            chosen = ids[(day - FIRST_DAY).days % len(ids)]
            await conn.execute("""
                INSERT INTO daily_editions (day, level_id, atlas_version) VALUES ($1, $2, $3)
                ON CONFLICT (day) DO NOTHING
            """, day, chosen["id"], chosen["atlas_version"])
        level = await conn.fetchrow("""
            SELECT l.id, coalesce(v.config, l.config) AS config, l.category, d.atlas_version,
                   coalesce(v.storage_key, l.storage_key) AS storage_key, a.completed_at, a.elapsed_ms
            FROM daily_editions d JOIN levels l ON l.id = d.level_id
            LEFT JOIN level_versions v ON v.level_id = l.id AND v.version = d.atlas_version
            LEFT JOIN daily_attempts a ON a.day = d.day AND a.user_id = $2
            WHERE d.day = $1
        """, day, user_id)
        await conn.execute("""
            INSERT INTO daily_attempts (day, user_id) VALUES ($1, $2)
            ON CONFLICT (day, user_id) DO NOTHING
        """, day, user_id)
    config = await run_in_threadpool(with_silhouettes, level["config"], upload_dir / level["storage_key"])
    return {"date": day, "level": {**config, "id": level["id"], "category": level["category"],
                                    "atlas_version": level["atlas_version"]},
            "completed": level["completed_at"] is not None, "elapsed_ms": level["elapsed_ms"],
            "completed_at": level["completed_at"]}


async def complete_daily(db, user_id, data):
    day = today()
    if day.isoformat() != data.date:
        raise HTTPException(409, "本期每日一瓷已结束")
    async with db.acquire() as conn, conn.transaction():
        attempt = await conn.fetchrow("""
            SELECT a.started_at, a.completed_at, a.elapsed_ms, coalesce(v.config, l.config)->>'name' AS name,
                   jsonb_array_length(coalesce(v.config, l.config)->'shards') -
                   coalesce((coalesce(v.config, l.config)->>'decoy_count')::int, 0) AS pieces
            FROM daily_attempts a JOIN daily_editions d ON d.day = a.day
            JOIN levels l ON l.id = d.level_id
            LEFT JOIN level_versions v ON v.level_id = l.id AND v.version = d.atlas_version
            WHERE a.user_id = $1 AND a.day = $2 FOR UPDATE OF a
        """, user_id, day)
        if attempt is None:
            raise HTTPException(409, "请先开启今日挑战")
        if attempt["completed_at"] is not None:
            return {"date": day, "elapsed_ms": attempt["elapsed_ms"]}
        if sorted(data.group.ids) != list(range(attempt["pieces"])):
            raise HTTPException(422, "请先拼合全部碎片")
        now = datetime.now(UTC)
        elapsed = max(1000, min(data.elapsed_ms, int((now - attempt["started_at"]).total_seconds() * 1000)))
        await conn.execute("""
            UPDATE daily_attempts SET completed_at = $3, elapsed_ms = $4, porcelain_name = $5
            WHERE user_id = $1 AND day = $2
        """, user_id, day, now, elapsed, attempt["name"])
    return {"date": day, "elapsed_ms": elapsed}


async def collection(db, user_id):
    await settle_months(db, player_id=user_id)
    async with db.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        items = await conn.fetch("""
            SELECT a.day AS date, d.level_id, d.atlas_version, a.porcelain_name AS name, a.elapsed_ms, a.completed_at,
                   coalesce(v.config, l.config)->'poster' AS poster
            FROM daily_attempts a JOIN daily_editions d ON d.day = a.day
            JOIN levels l ON l.id = d.level_id
            LEFT JOIN level_versions v ON v.level_id = l.id AND v.version = d.atlas_version
            WHERE a.user_id = $1 AND a.completed_at IS NOT NULL ORDER BY a.day DESC
        """, user_id)
        claims = await conn.fetch(f"""
            SELECT {CLAIM_FIELDS} FROM porcelain_redemptions WHERE user_id = $1 ORDER BY created_at DESC, id
        """, user_id)
        awards = await conn.fetch("""
            SELECT month, category, level_number, elapsed_ms FROM monthly_porcelain_awards a
            WHERE a.user_id = $1 AND NOT EXISTS (SELECT 1 FROM porcelain_redemptions r WHERE r.reward_month = a.month)
            ORDER BY month DESC
        """, user_id)
    spent = sum(row["reward_month"] is None for row in claims)
    return {"items": [dict(row) for row in items], "claims": [dict(row) for row in claims],
            "awards": [dict(row) for row in awards], "cost": COST, "balance": max(0, len(items) - COST * spent)}


async def redeem(db, user_id, data):
    async with db.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT id FROM users WHERE id = $1 AND active FOR UPDATE", user_id):
            raise HTTPException(401, "玩家不可用")
        existing = await conn.fetchrow(f"""
            SELECT {CLAIM_FIELDS} FROM porcelain_redemptions WHERE user_id = $1 AND request_id = $2
        """, user_id, data.request_id)
        if existing is not None:
            return dict(existing)
        reward_month = month_date(data.reward_month) if data.reward_month else None
        if reward_month:
            award = await conn.fetchval("""
                SELECT 1 FROM monthly_porcelain_awards a WHERE a.user_id = $1 AND a.month = $2
                AND NOT EXISTS (SELECT 1 FROM porcelain_redemptions r WHERE r.reward_month = a.month)
            """, user_id, reward_month)
            if not award:
                raise HTTPException(409, "这份月榜奖励已领取或不可用")
        else:
            balance = await conn.fetchval("""
                SELECT (SELECT count(*) FROM daily_attempts WHERE user_id = $1 AND completed_at IS NOT NULL)
                    - $2 * (SELECT count(*) FROM porcelain_redemptions WHERE user_id = $1 AND reward_month IS NULL)
            """, user_id, COST)
            if balance < COST:
                raise HTTPException(409, "还未集齐 30 件可兑换藏品")
        row = await conn.fetchrow(f"""
            INSERT INTO porcelain_redemptions (id, user_id, request_id, recipient, phone, address, reward_month)
            VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING {CLAIM_FIELDS}
        """, uuid4(), user_id, data.request_id, data.recipient, data.phone, data.address, reward_month)
        return dict(row)


async def update_shipment(db, claim_id, status, carrier, tracking_number):
    carrier, tracking_number = carrier.strip(), tracking_number.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in carrier + tracking_number):
        raise HTTPException(422, "请填写有效物流信息")
    if status in ("shipped", "delivered") and (not carrier or not tracking_number):
        raise HTTPException(422, "发货前请填写物流公司和单号")
    row = await db.fetchrow("""
        UPDATE porcelain_redemptions SET status = $2, carrier = $3, tracking_number = $4,
            updated_at = now()
        WHERE id = $1 RETURNING id
    """, claim_id, status, carrier or None, tracking_number or None)
    if row is None:
        raise HTTPException(404, "兑换记录不存在")
    return {"ok": True}

