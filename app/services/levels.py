import io
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.schemas import LevelConfig

PNG_LIMIT = 12 * 1024 * 1024
CONFIG_LIMIT = 512 * 1024


def invalid(field, message):
    raise HTTPException(422, f"{field}：{message}")


def polygon(field, values, minimum):
    if len(values) % 2 or len(values) < minimum * 2:
        invalid(field, f"必须为交替的 x、y 坐标，至少 {minimum} 个点")


def rectangle(field, rect, size):
    x, y, width, height = rect
    if min(width, height) <= 0 or x + width > size[0] or y + height > size[1]:
        invalid(field, "裁剪矩形必须完整落在图集内且宽高为正")


def validate_upload(png, config_json):
    # 先检查尺寸，再做完整解码，限制单次上传占用的内存。
    try:
        with Image.open(io.BytesIO(png), formats=["PNG"]) as img:
            size = img.size
            if not all(1 <= value <= 4096 for value in size):
                invalid("atlas", "图集宽高必须为 1–4096 像素")
            if getattr(img, "n_frames", 1) != 1:
                invalid("atlas", "图集必须为单帧 PNG")
            img.verify()
        with Image.open(io.BytesIO(png), formats=["PNG"]) as img:
            img.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        invalid("atlas", "需要可完整解码的 PNG")

    try:
        config = LevelConfig.model_validate_json(config_json)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        invalid("config", "；".join(
            f"{'.'.join(map(str, error['loc'])) or 'JSON'} {error['msg']}"
            for error in errors[:12]
        ))
    config.name = config.name.strip()
    if not config.name or any("\0" in value for value in (config.name, config.difficulty, config.story)):
        invalid("name / difficulty / story", "名称不能为空，文本不得包含空字符")
    if len(config.points) not in (0, 3):
        invalid("points", "无需描金用空数组，描金关卡必须配置三个点")
    rectangle("poster", config.poster, size)
    if config.poster[2:] != (400, 450):
        invalid("poster", "完整瓷器裁剪尺寸必须为 400 × 450")
    polygon("outline", config.outline, 3)
    for index, contour in enumerate(config.contours or []):
        polygon(f"contours.{index}", contour, 3)
    real_count = len(config.shards) - config.decoy_count
    if real_count < 1 or (config.rotate_count is not None and config.rotate_count > real_count):
        invalid("rotate_count / decoy_count", "旋转片和干扰片数量无效")
    neighbors = [set() for _ in range(real_count)]
    for index, shard in enumerate(config.shards):
        field = f"shards.{index}"
        rectangle(f"{field}.sprite", shard.sprite, size)
        polygon(f"{field}.poly", shard.poly, 3)
        for number, contour in enumerate(shard.contours or []):
            polygon(f"{field}.contours.{number}", contour, 3)
        left, top, right, bottom = shard.box
        if (right - left, bottom - top) != shard.sprite[2:]:
            invalid(f"{field}.box", "宽高必须与 sprite 一致")
    for index, seam in enumerate(config.seams):
        a, b = seam.ids
        if a == b or max(a, b) >= len(neighbors):
            invalid(f"seams.{index}.ids", "必须引用两个不同的有效碎片")
        polygon(f"seams.{index}.poly", seam.poly, 2)
        neighbors[a].add(b)
        neighbors[b].add(a)
    visited, pending = {0}, [0]
    while pending:
        unseen = neighbors[pending.pop()] - visited
        visited.update(unseen)
        pending.extend(unseen)
    if len(visited) != len(neighbors):
        invalid("seams", "裂边邻接图必须连通所有碎片")
    return config.model_dump(mode="json", exclude_none=True)


async def upload_level(db, directory: Path, atlas, config_file, category="normal", level_number=None):
    png = await atlas.read(PNG_LIMIT + 1)
    config_json = await config_file.read(CONFIG_LIMIT + 1)
    if len(png) > PNG_LIMIT:
        invalid("atlas", "PNG 最大 12 MiB")
    if len(config_json) > CONFIG_LIMIT:
        invalid("config", "JSON 最大 512 KiB")
    config = await run_in_threadpool(validate_upload, png, config_json)
    return await store_level(db, directory, png, config, category, level_number)


async def save_asset(directory, key, data):
    path = directory / key
    try:
        await run_in_threadpool(path.write_bytes, data)
    except OSError:
        await run_in_threadpool(path.unlink, missing_ok=True)
        raise


async def check_level_number(conn, category, level_number, level_id):
    if level_number and await conn.fetchval("""
        SELECT 1 FROM levels WHERE category = $1 AND level_number = $2 AND id <> $3
    """, category, level_number, level_id):
        raise HTTPException(409, "该分类已有这个关卡编号")


async def store_level(db, directory, png, config, category="normal", level_number=None, original=None):
    level_id = uuid4()
    key = f"{level_id}.png"
    original_key = f"{level_id}.source" if original is not None else None
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(421903, 1)")
        await check_level_number(conn, category, level_number, level_id)
        await save_asset(directory, key, png)
        if original_key:
            await save_asset(directory, original_key, original)
        await conn.execute("""
            INSERT INTO levels (id, name, storage_key, config, category, level_number, original_key)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, level_id, config["name"], key, config, category, level_number, original_key)
    return level_id


async def update_level(db, directory, level_id, previous_version, name, category, level_number, story,
                       generated=None, original=None):
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(421903, 1)")
        row = await conn.fetchrow("SELECT * FROM levels WHERE id = $1 FOR UPDATE", level_id)
        if row is None:
            raise HTTPException(404, "关卡不存在")
        if previous_version is not None and previous_version != row["atlas_version"]:
            raise HTTPException(409, "关卡图片已更新，请刷新后重试")
        number = level_number or row["level_number"]
        await check_level_number(conn, category, number, level_id)
        cfg = {**row["config"], "name": name, "story": story}
        key, version, original_key = row["storage_key"], row["atlas_version"], row["original_key"]
        if generated:
            png, new_config = generated
            await conn.execute("""
                INSERT INTO level_versions (level_id, version, storage_key, config)
                VALUES ($1, $2, $3, $4)
            """, level_id, version, key, row["config"])
            version += 1
            key = f"{level_id}-v{version}.png"
            await save_asset(directory, key, png)
            if original is not None:
                original_key = f"{level_id}-{uuid4().hex}.source"
                await save_asset(directory, original_key, original)
            cfg = {**new_config, **({"archived": True} if cfg.get("archived") else {})}
        await conn.execute("""
            UPDATE levels SET name = $2, category = $3, level_number = $4, config = $5,
                storage_key = $6, atlas_version = $7, original_key = $8
            WHERE id = $1
        """, level_id, name, category, number, cfg, key, version, original_key)
    if row["original_key"] and row["original_key"] != original_key:
        await run_in_threadpool((directory / row["original_key"]).unlink, missing_ok=True)
    return version


async def publish_level(db, level_id):
    async with db.acquire() as conn, conn.transaction():
        # 只串行化发布操作，确保发布顺序连续且不同管理员不会取到同一编号。
        await conn.execute("SELECT pg_advisory_xact_lock(421903, 1)")
        row = await conn.fetchrow("SELECT status, category, level_number FROM levels WHERE id = $1 FOR UPDATE", level_id)
        if row is None:
            raise HTTPException(404, "关卡不存在")
        if row["status"] == "published":
            await conn.execute("UPDATE levels SET config = config - 'archived' WHERE id = $1", level_id)
            return
        if row["status"] != "draft":
            raise HTTPException(409, "只有草稿关卡可以发布")

        # 发布后保持原关卡 ID、顺序与几何。重复发布直接返回。
        last_order = await conn.fetchval("SELECT MAX(sort_order) FROM levels")
        number = row["level_number"] or await conn.fetchval("""
            SELECT coalesce(max(level_number), 0) + 1 FROM levels WHERE category = $1
        """, row["category"])
        await conn.execute("""
            UPDATE levels SET status = $2, published_at = $3, sort_order = $4, level_number = $5 WHERE id = $1
        """, level_id, "published", datetime.now(UTC), (last_order or 0) + 1, number)
