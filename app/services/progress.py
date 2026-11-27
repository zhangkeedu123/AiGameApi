from datetime import UTC, datetime

from fastapi import HTTPException


def progress_item(row, point_count):
    snapshot = row["snapshot"]
    paint = snapshot["paint"]
    stage = "puzzle"
    if len(snapshot["groups"]) == 1:
        stage = "complete" if (snapshot.get("assembly_only") or len(paint) == point_count) else "painting"
    return {
        "level_id": row["level_id"],
        **snapshot,
        "stage": stage,
        "revision": row["revision"],
        "completed": row["first_completed_at"] is not None,
        "first_completed_at": row["first_completed_at"],
        "saved_at": row["saved_at"],
    }


async def read_progress(db, user_id):
    # 在同一 MVCC 快照读取当前关卡和各关记录。
    async with db.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        current = await conn.fetchval("SELECT current_level_id FROM users WHERE id = $1", user_id)
        rows = await conn.fetch(
            """SELECT p.*, jsonb_array_length(l.config->'points') AS point_count FROM user_level_progress p JOIN levels l ON l.id = p.level_id
               WHERE p.user_id = $1 ORDER BY l.sort_order""", user_id,
        )
    return {"current_level_id": current, "progress": [progress_item(row, row["point_count"]) for row in rows]}


async def save_progress(db, user_id, level_id, data):
    async with db.acquire() as conn, conn.transaction():
        level = await conn.fetchrow(
            "SELECT sort_order, level_number, category, config FROM levels WHERE id = $1 AND status = 'published'", level_id
        )
        if level is None:
            raise HTTPException(404, "已发布关卡不存在")

        # 同一玩家的存档共用行锁，使解锁、首次通关和当前关卡按事务顺序提交。
        current = await conn.fetchrow(
            "SELECT current_level_id FROM users WHERE id = $1 AND active FOR UPDATE", user_id
        )
        if current is None:
            raise HTTPException(401, "玩家不可用")
        if level["level_number"] > 1 and not await conn.fetchval(
            """SELECT NOT EXISTS (SELECT 1 FROM levels prev WHERE prev.status='published'
               AND prev.category = $3 AND prev.level_number < $2 AND prev.config->>'archived' IS DISTINCT FROM 'true')
               OR EXISTS (SELECT 1 FROM user_level_progress p JOIN levels l ON l.id = p.level_id
               WHERE p.user_id = $1 AND l.level_number = (
                   SELECT max(prev.level_number) FROM levels prev WHERE prev.status='published'
                   AND prev.category = $3 AND prev.level_number < $2 AND prev.config->>'archived' IS DISTINCT FROM 'true'
               ) AND p.first_completed_at IS NOT NULL)""",
            user_id, level["level_number"], level["category"],
        ):
            raise HTTPException(403, "关卡尚未解锁")

        config = level["config"]
        ids = [shard_id for group in data.groups for shard_id in group.ids]
        real_count = len(config["shards"]) - config.get("decoy_count", 0)
        if sorted(ids) not in (list(range(len(config["shards"]))), list(range(real_count))) or (
            len(ids) == real_count and real_count != len(config["shards"]) and len(data.groups) != 1
        ):
            raise HTTPException(422, "groups.ids 必须无重复地覆盖所有碎片")
        if data.paint and len(data.groups) != 1:
            raise HTTPException(422, "paint 仅在碎片合成一组后可保存")
        if len(set(data.paint)) != len(data.paint) or any(i >= len(config["points"]) for i in data.paint):
            raise HTTPException(422, "paint 必须为不重复的有效金点索引")

        snapshot = data.model_dump(exclude={"revision"}, mode="json")
        if not data.assembly_only:
            snapshot.pop("assembly_only")
        row = await conn.fetchrow(
            "SELECT * FROM user_level_progress WHERE user_id = $1 AND level_id = $2",
            user_id, level_id,
        )
        actual_revision = row["revision"] if row else 0
        if row and data.revision <= actual_revision and row["snapshot"] == snapshot:
            # 重试已提交的旧请求时保留较新的当前关卡。
            if data.revision == actual_revision and current["current_level_id"] != level_id:
                await set_current(conn, user_id, level_id)
            return progress_item(row, len(config["points"]))
        if data.revision != actual_revision:
            raise HTTPException(409, "修订号不匹配，请重新读取存档")

        completed = len(data.groups) == 1 and (data.assembly_only or len(data.paint) == len(config["points"]))
        saved_at = datetime.now(UTC)
        first_completed_at = row["first_completed_at"] if row else None
        if completed and first_completed_at is None:
            first_completed_at = saved_at
        next_revision = actual_revision + 1
        # PostgreSQL 原子比较修订号。仅 revision=0 可插入首存。
        if row is None:
            saved = await conn.fetchrow(
                """INSERT INTO user_level_progress
                   (user_id, level_id, snapshot, revision, first_completed_at, saved_at)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (user_id, level_id) DO NOTHING RETURNING *""",
                user_id, level_id, snapshot, next_revision, first_completed_at, saved_at,
            )
        else:
            saved = await conn.fetchrow(
                """UPDATE user_level_progress SET snapshot = $3, revision = $4,
                   first_completed_at = $5, saved_at = $6
                   WHERE user_id = $1 AND level_id = $2 AND revision = $7 RETURNING *""",
                user_id, level_id, snapshot, next_revision, first_completed_at, saved_at, data.revision,
            )
        if saved is None:
            raise HTTPException(409, "修订号不匹配，请重新读取存档")
        await set_current(conn, user_id, level_id)
        return progress_item(saved, len(config["points"]))


async def set_current(conn, user_id, level_id):
    await conn.execute("UPDATE users SET current_level_id = $2 WHERE id = $1", user_id, level_id)
