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
    rectangle("poster", config.poster, size)
    if config.poster[2:] != (400, 450):
        invalid("poster", "完整瓷器裁剪尺寸必须为 400 × 450")
    polygon("outline", config.outline, 3)
    neighbors = [set() for _ in config.shards]
    for index, shard in enumerate(config.shards):
        field = f"shards.{index}"
        rectangle(f"{field}.sprite", shard.sprite, size)
        polygon(f"{field}.poly", shard.poly, 3)
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
    return config.model_dump(mode="json")


async def upload_level(db, directory: Path, atlas, config_file):
    png = await atlas.read(PNG_LIMIT + 1)
    config_json = await config_file.read(CONFIG_LIMIT + 1)
    if len(png) > PNG_LIMIT:
        invalid("atlas", "PNG 最大 12 MiB")
    if len(config_json) > CONFIG_LIMIT:
        invalid("config", "JSON 最大 512 KiB")
    config = await run_in_threadpool(validate_upload, png, config_json)
    level_id = uuid4()
    key = f"{level_id}.png"
    path = directory / key
    try:
        await run_in_threadpool(path.write_bytes, png)
    except OSError:
        await run_in_threadpool(path.unlink, missing_ok=True)
        raise
    # 文件写入后才登记草稿。公开入口始终先查询发布状态。
    # 数据库连接中断时提交结果可能未知，保留文件以免删除已登记资源。
    await db.execute(
        """INSERT INTO levels (id, name, storage_key, config, status, created_at)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        level_id, config["name"], key, config, "draft", datetime.now(UTC),
    )
    return level_id


async def publish_level(db, level_id):
    async with db.acquire() as conn, conn.transaction():
        # 只串行化发布操作，确保发布顺序连续且不同管理员不会取到同一编号。
        await conn.execute("SELECT pg_advisory_xact_lock(421903, 1)")
        row = await conn.fetchrow("SELECT status FROM levels WHERE id = $1", level_id)
        if row is None:
            raise HTTPException(404, "关卡不存在")
        if row["status"] == "published":
            return
        if row["status"] != "draft":
            raise HTTPException(409, "只有草稿关卡可以发布")

        # 发布后保持原关卡 ID、顺序与几何。重复发布直接返回。
        last_order = await conn.fetchval("SELECT MAX(sort_order) FROM levels")
        await conn.execute(
            "UPDATE levels SET status = $2, published_at = $3, sort_order = $4 WHERE id = $1",
            level_id, "published", datetime.now(UTC), (last_order or 0) + 1,
        )
