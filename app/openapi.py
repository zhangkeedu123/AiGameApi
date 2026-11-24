"""OpenAPI 文档的集中配置。

每个接口的说明写在 app/routes/api.py 的 summary / description 里，
跨接口的约定（鉴权、错误结构、坐标系、示例）集中在本文件，
避免文档信息散落在多处。三个文档入口内容同源：

* ``/docs``         Swagger UI，可直接发请求
* ``/openapi.json`` OpenAPI 3.1，供 Apifox / Postman / AI 读取
* ``/api.md``       纯文本联调指南，适合直接贴给 AI 或写进前端 README
"""

from fastapi.openapi.utils import get_openapi

# externalDocs 的 url 必须是绝对地址，这里与联调地址保持一致。
GUIDE_URL = "http://139.196.161.33:8000/api.md"

# Swagger UI 顶部的分组说明，顺序即接口列表里的分组顺序。
TAGS_METADATA = [
    {
        "name": "关卡",
        "description": "读取已发布关卡的列表、几何配置和图集贴图。无需鉴权，可直接调用。",
        "externalDocs": {"description": "接口联调指南", "url": GUIDE_URL},
    },
    {
        "name": "玩家进度",
        "description": (
            "玩家资料与存档读写。全部需要 `Authorization: Bearer <玩家令牌>`，"
            "令牌由管理员在 `/admin/users/{玩家 ID}` 页面签发，明文只显示一次。"
        ),
        "externalDocs": {"description": "接口联调指南", "url": GUIDE_URL},
    },
    {
        "name": "运维",
        "description": "健康检查与文档入口，部署和排障时使用。",
    },
]

SERVERS = [
    {"url": "/", "description": "当前部署（相对路径，Swagger 里直接调试用这个）"},
    {
        "url": "http://139.196.161.33:8000",
        "description": "线上联调环境（HTTP，内测阶段，未上 HTTPS）",
    },
]

API_DESCRIPTION = """金缮夜馆（拼图 + 描金）小游戏后端。业务接口统一以 `/api/v1` 开头，管理后台 `/admin` 是服务端渲染的
HTML 页面（浏览器操作，不在本文档的接口列表里）。

## 快速开始

1. 管理员打开 `/admin` 登录 → 「玩家」→ 选中玩家 → 「签发或轮换令牌」，得到一次性明文令牌。
2. 联调请求加上请求头 `Authorization: Bearer <令牌>`。
3. `GET /api/v1/levels` 取关卡列表 → `GET /api/v1/levels/{id}` 取几何配置，返回里的 `image` 就是图集 PNG 地址。
4. 进入关卡时 `POST /api/v1/me/login` 打卡，`GET /api/v1/me/progress` 恢复存档。
5. 保存进度用 `PUT /api/v1/me/progress/{level_id}`，`revision` 必须等于上次读到的值，成功后返回 `revision + 1`。

## 鉴权

带锁图标的接口使用 HTTP Bearer：

```
Authorization: Bearer <player-token>
```

令牌按玩家签发，数据库只存 SHA-256 摘要；撤销后立即失效，重新签发会让旧令牌同时失效。
令牌只用于联调，正式接入时应换成平台登录态（微信 / 抖音）。

## 通用约定

| 项目 | 约定 |
| --- | --- |
| 数据格式 | JSON，UTF-8 |
| ID | UUID 字符串，如 `3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60` |
| 时间 | ISO 8601 带时区 UTC，如 `2026-11-20T16:04:07.512931+00:00` |
| 坐标 | 图稿坐标系，整幅图集 `400 × 450`，原点左上角，`y` 轴向下 |
| 数值 | 必须为有限浮点数；`NaN`、`Infinity`、字符串数字会被拒绝 |
| 请求体 | 拒绝未知字段（`extra="forbid"`），多传字段直接 422，不要夹带客户端内部字段 |
| 精度 | 坐标原样回存，服务端不取整；吸附与容差由客户端决定 |

## 错误响应

失败统一返回 `detail`，是中文可读原因；422 时 `detail` 是字段错误数组：

```json
{"detail": "已发布关卡不存在"}
```

```json
{"detail": [{"loc": ["body", "revision"], "msg": "Input should be greater than or equal to 0", "type": "greater_than_equal"}]}
```

| 状态码 | 场景 | 前端处理建议 |
| --- | --- | --- |
| 401 | 未带令牌、令牌无效或已撤销 | 提示重新授权，别自动重试 |
| 403 | 关卡未解锁（上一关未通关） | 引导玩家先通关上一关 |
| 404 | 关卡不存在或仍是草稿 | 回关卡列表刷新 |
| 409 | 存档 `revision` 冲突（多端同时写） | 重新 `GET /api/v1/me/progress` 合并后再保存 |
| 422 | 字段不合法 | 按 `detail[].loc` 定位字段 |
| 500 | 服务内部错误，响应含 `error_id` | 记录 `error_id` 反馈后端 |

## 文档入口

* Swagger UI：`/docs`
* OpenAPI JSON：`/openapi.json`（可直接导入 Apifox / Postman）
* 纯文本指南：`/api.md`（接口清单 + curl 示例，适合喂给 AI）
"""

# 通用错误响应体，供各接口 responses 引用。
ERROR_SCHEMA = {
    "title": "Error",
    "type": "object",
    "description": "统一错误响应体。422 时 detail 为字段错误数组。",
    "properties": {
        "detail": {
            "description": "中文错误原因；422 时为 {loc, msg, type} 数组",
            "oneOf": [
                {"type": "string"},
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "loc": {"type": "array", "items": {"type": "string"}},
                            "msg": {"type": "string"},
                            "type": {"type": "string"},
                        },
                    },
                },
            ],
        },
        "error_id": {"type": "string", "description": "仅 500 响应出现，用于对日志"},
    },
    "required": ["detail"],
    "example": {"detail": "已发布关卡不存在"},
}


def error_response(description: str, example: str, status: int = 401) -> dict:
    """构造单个错误响应定义，让 Swagger 里能直接看到具体报错文案。"""
    headers = {"WWW-Authenticate": {"schema": {"type": "string"}, "description": "Bearer"}} \
        if status == 401 else None
    response = {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/Error"},
                "example": {"detail": example},
            }
        },
    }
    if headers:
        response["headers"] = headers
    return response


# 请求体示例挂在组件 schema 上，Swagger 的 "Example Value" 会直接显示，
# 不用把示例硬编码进接口定义。
COMPONENT_EXAMPLES = {
    "ProgressInput": {
        "groups": [
            {"ids": [0, 1, 2], "x": 0, "y": 0, "turn": 0},
        ],
        "paint": [0, 1],
        "revision": 7,
    },
    "Group": {"ids": [0, 1], "x": 12.5, "y": 40, "turn": 2},
}


def build_openapi(app):
    """生成文档并补上 FastAPI 默认不输出的中文说明与示例。"""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=API_DESCRIPTION,
        routes=app.routes,
        tags=TAGS_METADATA,
        servers=SERVERS,
    )

    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.setdefault("Error", ERROR_SCHEMA)

    for name, example in COMPONENT_EXAMPLES.items():
        node = schemas.get(name)
        if node is not None:
            node["example"] = example

    for scheme in components.get("securitySchemes", {}).values():
        if scheme.get("scheme") == "bearer":
            scheme.setdefault(
                "description",
                "玩家联调令牌。管理员在 AiGameAdmin 玩家管理中签发，明文只显示一次；"
                "请求头写法：Authorization: Bearer <令牌>",
            )

    app.openapi_schema = schema
    return schema
