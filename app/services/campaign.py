from datetime import UTC, date, datetime, timedelta, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field

from app.schemas import Group, InputModel

BEIJING = timezone(timedelta(hours=8))
SCORE_ORDER = "level_number DESC, elapsed_ms, completed_at, user_id"


class Finish(InputModel):
    group: Group
    elapsed_ms: Annotated[int, Field(ge=1, le=604800000)]


def current_month():
    return datetime.now(BEIJING).date().replace(day=1)


def month_date(value):
    if value is None:
        return current_month()
    try:
        return date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise HTTPException(422, "月份格式为 YYYY-MM") from exc


async def completed_levels(db, user_id):
    rows = await db.fetch("""
        SELECT level_id FROM campaign_attempts WHERE user_id = $1 AND completed_at IS NOT NULL
        UNION SELECT level_id FROM user_level_progress WHERE user_id = $1 AND first_completed_at IS NOT NULL
    """, user_id)
    return [row["level_id"] for row in rows]


async def start_campaign(db, user_id, level_id):
    row = await db.fetchrow("""
        SELECT id, category, level_number, atlas_version,
            jsonb_array_length(config->'shards') - coalesce((config->>'decoy_count')::int, 0) AS pieces
        FROM levels WHERE id = $1 AND status = 'published' AND config->>'archived' IS DISTINCT FROM 'true'
    """, level_id)
    if row is None:
        raise HTTPException(404, "该关卡暂无开放")
    previous = await db.fetchval("""
        SELECT id FROM levels WHERE category = $1 AND level_number < $2 AND status = 'published'
            AND config->>'archived' IS DISTINCT FROM 'true' ORDER BY level_number DESC LIMIT 1
    """, row["category"], row["level_number"])
    if previous is not None and previous not in await completed_levels(db, user_id):
        raise HTTPException(403, "请先完成上一关")
    attempt_id = await db.fetchval("""
        INSERT INTO campaign_attempts (id, user_id, level_id, category, level_number, atlas_version, piece_count)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (user_id, level_id, atlas_version) WHERE completed_at IS NULL
        DO UPDATE SET category = excluded.category, level_number = excluded.level_number
        RETURNING id
    """, uuid4(), user_id, level_id, row["category"], row["level_number"], row["atlas_version"], row["pieces"])
    return {"id": attempt_id, "atlas_version": row["atlas_version"]}


async def finish_campaign(db, user_id, attempt_id, data):
    async with db.acquire() as conn, conn.transaction():
        # 结算使用同一把锁，避免跨过月界时漏掉正在提交的成绩。
        await conn.execute("SELECT pg_advisory_xact_lock_shared(421903, 2)")
        attempt = await conn.fetchrow("""
            SELECT * FROM campaign_attempts WHERE id = $1 AND user_id = $2 FOR UPDATE
        """, attempt_id, user_id)
        if attempt is None:
            raise HTTPException(404, "找不到本次挑战记录")
        if attempt["completed_at"]:
            return {"elapsed_ms": attempt["elapsed_ms"]}
        if sorted(data.group.ids) != list(range(attempt["piece_count"])):
            raise HTTPException(422, "请先拼合全部正确碎片")
        now = datetime.now(UTC)
        wall_ms = max(1, int((now - attempt["started_at"]).total_seconds() * 1000))
        if data.elapsed_ms > wall_ms + 5000:
            raise HTTPException(422, "本次计时超出服务器记录，请重新开始")
        await conn.execute("""
            UPDATE campaign_attempts SET elapsed_ms = $2, completed_at = $3 WHERE id = $1
        """, attempt_id, data.elapsed_ms, now)
        await conn.execute("""
            INSERT INTO user_level_progress (user_id, level_id, snapshot, revision, first_completed_at, saved_at)
            SELECT $1, id, $3, 1, $4, $4 FROM levels WHERE id = $2 AND atlas_version = $5
            ON CONFLICT (user_id, level_id) DO UPDATE SET
                first_completed_at = coalesce(user_level_progress.first_completed_at, excluded.first_completed_at),
                revision = user_level_progress.revision + 1, snapshot = excluded.snapshot, saved_at = excluded.saved_at
        """, user_id, attempt["level_id"], {"groups": [data.group.model_dump(mode="json")], "paint": [], "assembly_only": True},
            now, attempt["atlas_version"])
    return {"elapsed_ms": data.elapsed_ms}


async def settle_months(db, player_id=None, month=None):
    cutoff = current_month()
    if month is not None and month >= cutoff:
        return
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(421903, 2)")
        await conn.execute(f"""
            WITH months AS (
                SELECT DISTINCT date_trunc('month', a.completed_at AT TIME ZONE 'Asia/Shanghai')::date AS month
                FROM campaign_attempts a
                WHERE a.completed_at < ($1::date::timestamp AT TIME ZONE 'Asia/Shanghai')
                    AND ($2::uuid IS NULL OR a.user_id = $2)
                    AND ($3::date IS NULL OR (a.completed_at AT TIME ZONE 'Asia/Shanghai')::date >= $3
                        AND (a.completed_at AT TIME ZONE 'Asia/Shanghai')::date < $3 + interval '1 month')
            )
            INSERT INTO monthly_porcelain_awards (month, user_id, category, level_number, elapsed_ms)
            SELECT m.month, winner.user_id, winner.category, winner.level_number, winner.elapsed_ms
            FROM months m
            CROSS JOIN LATERAL (
                SELECT a.* FROM campaign_attempts a
                WHERE a.completed_at >= (m.month::timestamp AT TIME ZONE 'Asia/Shanghai')
                    AND a.completed_at < ((m.month + interval '1 month') AT TIME ZONE 'Asia/Shanghai')
                ORDER BY {SCORE_ORDER} LIMIT 1
            ) winner
            WHERE NOT EXISTS (SELECT 1 FROM monthly_porcelain_awards w WHERE w.month = m.month)
            ON CONFLICT (month) DO NOTHING
        """, cutoff, player_id, month)


async def leaderboard(db, month, source=None):
    if month < current_month():
        await settle_months(db, month=month)
    rows = await db.fetch(f"""
        WITH best AS (
            SELECT DISTINCT ON (a.user_id) a.user_id, a.category, a.level_number, a.elapsed_ms, a.completed_at,
                u.nickname, u.source
            FROM campaign_attempts a JOIN users u ON u.id = a.user_id
            WHERE a.completed_at >= ($1::date::timestamp AT TIME ZONE 'Asia/Shanghai')
                AND a.completed_at < (($1::date + interval '1 month') AT TIME ZONE 'Asia/Shanghai')
                AND ($2::text IS NULL OR u.source = $2)
            ORDER BY a.user_id, a.level_number DESC, a.elapsed_ms, a.completed_at
        )
        SELECT * FROM best ORDER BY {SCORE_ORDER} LIMIT 30
    """, month, source)
    data = [{"rank": index, "nickname": row["nickname"], "source": row["source"],
             "category": row["category"], "level_number": row["level_number"], "elapsed_ms": row["elapsed_ms"]}
            for index, row in enumerate(rows, 1)]
    award = await db.fetchrow("""
        SELECT coalesce(u.nickname, '玩家') AS nickname, w.category, w.level_number, w.elapsed_ms,
            EXISTS (SELECT 1 FROM porcelain_redemptions r WHERE r.reward_month = w.month) AS claimed
        FROM monthly_porcelain_awards w JOIN users u ON u.id = w.user_id WHERE w.month = $1
    """, month)
    return {"month": month.strftime("%Y-%m"), "current_month": current_month().strftime("%Y-%m"),
            "items": data, "winner": dict(award) if award else None}
