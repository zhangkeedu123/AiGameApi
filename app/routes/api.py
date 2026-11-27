from secrets import token_urlsafe
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.openapi import error_response
from app.schemas import GuestLogin, GuestSession, PlayerProfile, ProgressInput
from app.security import player
from app.services.collection import CATEGORY
from app.services.players import get_profile, guest_user, record_login, update_token
from app.services.progress import read_progress, save_progress
from app.services.silhouette import with_silhouettes

router = APIRouter(prefix="/api/v1")
Player = Annotated[UUID, Depends(player)]
LevelId = Annotated[UUID, Path(description="关卡 ID（UUID）")]

LEVEL = "关卡"
PROGRESS = "玩家进度"

LEVEL_LIST_EXAMPLE = [
    {
        "id": "3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60",
        "name": "青釉三片",
        "sort_order": 1,
        "published_at": "2026-11-20T16:04:07.512931+00:00",
        "difficulty": "入门",
    }
]

LEVEL_DETAIL_EXAMPLE = {
    "name": "青釉三片",
    "difficulty": "入门",
    "story": "拼合三片青釉瓷，沿裂纹点亮三处金点。",
    "rotations": 2,
    "snapDistance": 18,
    "poster": [0, 0, 400, 450],
    "outline": [100, 35, 300, 35, 365, 140, 355, 350, 305, 405, 95, 405, 45, 350, 35, 140],
    "shards": [
        {
            "cx": 82.5,
            "cy": 220,
            "box": [35, 35, 130, 405],
            "poly": [100, 35, 130, 35, 130, 405, 95, 405, 45, 350, 35, 140],
            "sprite": [0, 0, 130, 450],
        },
        {
            "cx": 235,
            "cy": 220,
            "box": [130, 35, 365, 405],
            "poly": [130, 35, 300, 35, 365, 140, 355, 350, 305, 405, 130, 405],
            "sprite": [130, 0, 270, 450],
        },
    ],
    "seams": [{"ids": [0, 1], "poly": [130, 35, 130, 405]}],
    "points": [[150, 120], [240, 200], [300, 330]],
    "id": "3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60",
    "sort_order": 1,
    "published_at": "2026-11-20T16:04:07.512931+00:00",
    "image": "http://139.196.161.33:8000/api/v1/levels/3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60/atlas.png",
}

PROFILE_EXAMPLE = {
    "id": "9a1f0c22-5d3b-4e77-8c1a-0b7d2e5f8a31",
    "nickname": "测试玩家",
    "source": "wechat",
    "openid": "oXyz1234567890",
    "last_login_at": "2026-11-20T16:04:07.512931+00:00",
    "login_count": 12,
    "avatar_url": None,
}

SAVED_LEVEL_EXAMPLE = {
    "level_id": "3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60",
    "groups": [{"ids": [0, 1, 2], "x": 0, "y": 0, "turn": 0}],
    "paint": [0, 2],
    "stage": "painting",
    "revision": 8,
    "completed": False,
    "first_completed_at": None,
    "saved_at": "2026-11-20T16:04:07.512931+00:00",
}

PROGRESS_EXAMPLE = {
    "current_level_id": "3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60",
    "progress": [SAVED_LEVEL_EXAMPLE],
}

AUTH_RESPONSE = {
    401: error_response("未带令牌、令牌无效或已撤销", "需要有效的玩家令牌", 401),
}


@router.get(
    "/levels",
    tags=[LEVEL],
    operation_id="listLevels",
    summary="已发布关卡列表",
    description="""按通关顺序返回全部已发布关卡，**无需鉴权**。

草稿关卡不会出现；想调整顺序由后台在发布时决定 `sort_order`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string(UUID) | 关卡 ID，详情和存档都用它 |
| `name` | string | 关卡名 |
| `sort_order` | integer | 第几关，从 1 开始连续 |
| `published_at` | string(ISO 8601) | 发布时间（UTC） |
| `difficulty` | string | 难度文案，取自关卡配置 |
| `category` | string | 普通 normal、困难 hard、极难 extreme，可用同名查询参数筛选 |
""",
    response_description="关卡摘要数组，没有已发布关卡时为空数组",
    responses={200: {"content": {"application/json": {"example": LEVEL_LIST_EXAMPLE}}}},
)
async def list_levels(request: Request, category: CATEGORY | None = None):
    rows = await request.app.state.db.fetch(
        """SELECT id, name, category, sort_order, level_number, atlas_version, published_at, config->>'difficulty' AS difficulty
           FROM levels WHERE status = 'published' AND config->>'archived' IS DISTINCT FROM 'true'
           AND ($1::text IS NULL OR category = $1) ORDER BY level_number, category, sort_order""", category,
    )
    return [dict(row) for row in rows]


async def published_level(request, level_id):
    row = await request.app.state.db.fetchrow(
        """SELECT id, category, sort_order, level_number, atlas_version, published_at, config, storage_key
           FROM levels WHERE id = $1 AND status = 'published'""", level_id,
    )
    if row is None:
        raise HTTPException(404, "已发布关卡不存在")
    return row


@router.get(
    "/levels/{level_id}",
    tags=[LEVEL],
    operation_id="getLevel",
    summary="关卡几何配置",
    description="""返回该关的完整配置，**无需鉴权**。

响应 = 上传的关卡 JSON 原样字段 + 服务端补充的下列字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 关卡 ID |
| `sort_order` | 第几关 |
| `published_at` | 发布时间 |
| `image` | 图集 PNG 的绝对 URL，可直接作为 `<img src>` |

关卡 JSON 结构（与上传文件一致）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` / `difficulty` / `story` | string | 名称、难度、介绍 |
| `rotations` | int 0–3 | 允许的旋转次数 |
| `snapDistance` | number > 0 | 吸附距离（图稿坐标） |
| `poster` | `[x, y, w, h]` | 完整瓷器的裁剪矩形，固定 `400 × 450` |
| `outline` | number[] | 外轮廓，交替 `x, y` |
| `shards[]` | object | 碎片，**数组索引即碎片 ID** |
| `shards[].cx` / `cy` | number | 碎片中心 |
| `shards[].box` | `[l, t, r, b]` | 包围盒，宽高与 `sprite` 一致 |
| `shards[].poly` | number[] | 多边形，交替 `x, y`，≥ 3 点 |
| `shards[].sprite` | `[x, y, w, h]` | 图集裁剪矩形 |
| `seams[]` | `{ids:[a,b], poly:[...]}` | 裂边邻接关系 |
| `points` | `[[x, y], ...]` | 描金点，可为空或固定 3 个 |

坐标为图稿坐标系：图集 `400 × 450`，原点左上角，`y` 轴向下。
""",
    response_description="关卡配置对象，`image` 为可直接访问的图集地址",
    responses={
        200: {"content": {"application/json": {"example": LEVEL_DETAIL_EXAMPLE}}},
        404: error_response("关卡不存在，或仍是未发布的草稿", "已发布关卡不存在", 404),
    },
)
async def level_detail(request: Request, level_id: LevelId):
    row = await published_level(request, level_id)
    path = request.app.state.settings.upload_dir / row["storage_key"]
    config = await run_in_threadpool(with_silhouettes, row["config"], path)
    return {
        **config, "id": row["id"], "sort_order": row["sort_order"],
        "published_at": row["published_at"], "category": row["category"],
        "level_number": row["level_number"], "atlas_version": row["atlas_version"],
        "image": str(request.url_for("public_atlas", level_id=level_id).include_query_params(v=row["atlas_version"])),
    }


@router.get(
    "/levels/{level_id}/atlas.png",
    name="public_atlas",
    tags=[LEVEL],
    operation_id="getLevelAtlas",
    summary="关卡图集贴图（PNG）",
    description="""下载该关的图集 PNG，**无需鉴权**。

* 只有已发布关卡可读，草稿返回 404。
* 响应头带 `Cache-Control: public, max-age=86400, immutable`，浏览器/引擎可长期缓存。
* 上传目录不对外暴露，文件按数据库里的存储键读取，URL 猜不到其它资源。
""",
    response_class=Response,
    response_description="PNG 二进制",
    responses={
        200: {
            "description": "图集 PNG",
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
        },
        404: error_response("关卡不存在或未发布", "已发布关卡不存在", 404),
    },
)
async def public_atlas(request: Request, level_id: LevelId, v: int | None = Query(None, ge=1)):
    row = await published_level(request, level_id)
    if v is not None and v != row["atlas_version"]:
        row = await request.app.state.db.fetchrow("""
            SELECT storage_key FROM level_versions WHERE level_id = $1 AND version = $2
        """, level_id, v)
        if row is None:
            raise HTTPException(404, "该图集版本不存在")
    # 通过数据库查到的服务端存储键读取文件，上传目录不挂载为静态目录。
    path = request.app.state.settings.upload_dir / row["storage_key"]
    return FileResponse(path, media_type="image/png", headers={
        "Cache-Control": "public, max-age=86400, immutable", "X-Content-Type-Options": "nosniff"
    })


@router.get(
    "/me",
    response_model=PlayerProfile,
    tags=[PROGRESS],
    operation_id="getMe",
    summary="当前玩家资料",
    description="""用令牌换取玩家资料，**需要 Bearer 令牌**。

| 字段 | 说明 |
| --- | --- |
| `id` | 玩家 UUID，与后台「玩家管理」里的一致 |
| `nickname` | 昵称，可为空 |
| `source` / `openid` | 平台来源与平台 OpenID，由后台维护 |
| `avatar_url` | 头像地址，可为空 |
| `last_login_at` | 最近一次 `me/login` 时间，未登录过为 `null` |
| `login_count` | 累计登录次数 |
""",
    response_description="玩家资料",
    responses={200: {"content": {"application/json": {"example": PROFILE_EXAMPLE}}}, **AUTH_RESPONSE},
)
async def player_profile(request: Request, user_id: Player):
    return await get_profile(request.app.state.db, user_id)


@router.post(
    "/me/login",
    response_model=PlayerProfile,
    tags=[PROGRESS],
    operation_id="playerLogin",
    summary="登录打卡",
    description="""记录一次登录并把统计写回，返回结构与 `GET /me` 相同。

* 需要 Bearer 令牌，身份完全由令牌决定，**没有请求体**。
* `last_login_at` 更新为当前时间，`login_count` 加 1。
* 进入游戏时调一次即可；这是唯一会写 `users` 表的玩家接口，不要轮询。
""",
    response_description="更新后的玩家资料",
    responses={200: {"content": {"application/json": {"example": PROFILE_EXAMPLE}}}, **AUTH_RESPONSE},
)
async def player_login(request: Request, user_id: Player):
    # 联调令牌确定身份。平台来源和 OpenID 由后台资料提供。
    return await record_login(request.app.state.db, user_id)


@router.post(
    "/me/guest",
    response_model=GuestSession,
    tags=[PROGRESS],
    operation_id="guestSession",
    summary="设备号换令牌（免登录）",
    description="""用小游戏客户端生成的设备号换取玩家令牌，**无需鉴权**。

客户端首次启动调用一次，把返回的 `token` 存本地；之后所有需要鉴权的接口带
`Authorization: Bearer <token>` 即可，不需要玩家做任何操作。

| 请求字段 | 约束 |
| --- | --- |
| `device_id` | 8–120 位，仅 `A-Z a-z 0-9 _ -`，客户端生成并持久化 |
| `nickname` | 可选，首次创建时写入；已有昵称不会被覆盖 |
| `source` | `wechat` 或 `douyin`，默认 `wechat` |

同一 `device_id` 重复调用返回同一个玩家，但会**轮换令牌**（旧令牌立即失效），
因此不要在多台设备上使用同一个 `device_id`。
""",
    response_description="新令牌与玩家资料",
    responses={
        200: {"content": {"application/json": {"example": {"token": "5xQ...", **PROFILE_EXAMPLE}}}},
        403: error_response("玩家已被后台停用", "玩家已被停用", 403),
        422: error_response("device_id 格式不合法", "device_id 格式不合法", 422),
    },
)
async def guest_session(request: Request, data: GuestLogin):
    db = request.app.state.db
    user_id, active = await guest_user(db, data.device_id, data.nickname, data.source)
    if not active:
        raise HTTPException(403, "玩家已被停用")
    token = token_urlsafe(32)
    await update_token(db, user_id, token)
    return {"token": token, **await record_login(db, user_id)}


@router.get(
    "/me/progress",
    tags=[PROGRESS],
    operation_id="getProgress",
    summary="读取全部关卡存档",
    description="""返回当前关卡 + 所有已存档关卡的快照，**需要 Bearer 令牌**。

`progress` 只含有存档的关卡，按关卡顺序排列；新玩家是空数组。

每个元素除存档快照外还带服务端计算字段：

| 字段 | 说明 |
| --- | --- |
| `stage` | `puzzle` 未拼合 / `painting` 已合成但有金点未亮 / `complete` 全部完成 |
| `revision` | 乐观锁版本号，保存时原样回传 |
| `completed` | 是否已通关（历史最好成绩） |
| `first_completed_at` | **首次**通关时间，重复通关不刷新 |
| `saved_at` | 最后一次保存时间 |

`current_level_id` 是玩家最后进入的关卡，新玩家为 `null`。
""",
    response_description="当前关卡与存档数组",
    responses={
        200: {"content": {"application/json": {"example": PROGRESS_EXAMPLE}}},
        **AUTH_RESPONSE,
    },
)
async def get_progress(request: Request, user_id: Player):
    return await read_progress(request.app.state.db, user_id)


@router.put(
    "/me/progress/{level_id}",
    tags=[PROGRESS],
    operation_id="saveProgress",
    summary="保存关卡存档（乐观锁）",
    description="""覆盖写某一关的存档，**需要 Bearer 令牌**。

请求体规则：

| 字段 | 约束 |
| --- | --- |
| `groups[]` | 1–256 组；所有 `ids` 必须**无重复地覆盖** `0..碎片数-1` |
| `groups[].x` / `y` | `-450 ≤ x ≤ 850`、`-450 ≤ y ≤ 1040`（拼图板比图集高） |
| `groups[].turn` | 0–3，旋转 90° 的次数 |
| `paint[]` | 描金点索引，≤ 3 个且不重复；**仅当只剩一组碎片时可为非空** |
| `assembly_only` | 可选。在线关卡客户端以拼齐为通关，传 true |
| `revision` | 首次保存填 `0`；之后必须等于上次响应的 `revision` |

服务端行为：

* `revision` 不等于库里的值 → **409**，前端应重新 `GET /me/progress` 合并后再存。
* 同一 `revision` + 同一快照重复提交是幂等的，返回已存记录，可安全重试超时请求。
* 保存成功会同时把该关设为玩家的 `current_level_id`，返回 `revision + 1`。
* 只剩一组碎片，且描金完成、关卡无金点或使用拼齐即通关选项时判定通关，写入 `first_completed_at`。
* 关卡未发布 → 404；上一关未通关 → 403（关卡按 `sort_order` 顺序解锁）。
""",
    response_description="保存后的存档对象（`revision` 已递增）",
    responses={
        200: {"content": {"application/json": {"example": SAVED_LEVEL_EXAMPLE}}},
        **AUTH_RESPONSE,
        403: error_response("上一关还没通关，关卡未解锁", "关卡尚未解锁", 403),
        404: error_response("关卡不存在或未发布", "已发布关卡不存在", 404),
        409: error_response("revision 与服务器不一致，需重新读档", "修订号不匹配，请重新读取存档", 409),
        422: error_response(
            "请求体字段不合法（碎片覆盖不全、paint 越界等）",
            "groups.ids 必须无重复地覆盖所有碎片", 422,
        ),
    },
)
async def put_progress(
    request: Request, user_id: Player, level_id: LevelId, data: ProgressInput,
):
    return await save_progress(request.app.state.db, user_id, level_id, data)
