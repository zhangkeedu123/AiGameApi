"""纯文本联调指南，通过 ``GET /api.md`` 输出。

内容是给「读文档的人或 AI」看的，和 OpenAPI 描述同源、但更适合整段喂给模型：
包含调用顺序、curl 示例、字段结构、并发与解锁规则的坑。

改接口时请同步本文件与 app/openapi.py，否则两者会不一致。
"""

API_GUIDE = """# 金缮夜馆 · 后端联调指南

> 面向小游戏前端（含 AI 辅助开发）。机器可读版本：`/openapi.json`；
> 可交互调试：`/docs`。

## 1. 基础信息

| 项 | 值 |
| --- | --- |
| 联调地址 | `http://139.196.161.33:8000`（内测 HTTP，正式环境走 HTTPS） |
| 业务前缀 | `/api/v1` |
| 管理后台 | `/admin`（服务端渲染的 HTML 页面，给管理员用，不是 JSON 接口） |
| 数据格式 | JSON / UTF-8 |
| 时间格式 | ISO 8601 带时区，UTC |
| 关卡坐标系 | 图稿坐标，图集 `400 × 450`，原点左上角，`y` 轴向下 |

## 2. 鉴权

关卡查询接口**不需要**鉴权；`/api/v1/me/*` 需要玩家令牌：

```
Authorization: Bearer <player-token>
```

令牌获取：管理员打开 `/admin` → 登录 → 「玩家」→ 点开某位玩家 → 「签发或轮换令牌」。
明文只显示一次（库里只存 SHA-256 摘要），重新签发会让旧令牌立即失效。
令牌是联调用的，不要把真实令牌打进小游戏包里。

## 3. 接口速查

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/v1/levels` | 否 | 已发布关卡列表（按通关顺序） |
| GET | `/api/v1/levels/{level_id}` | 否 | 关卡几何配置 |
| GET | `/api/v1/levels/{level_id}/atlas.png` | 否 | 关卡图集贴图 |
| GET | `/api/v1/me` | 是 | 当前玩家资料 |
| POST | `/api/v1/me/login` | 是 | 登录打卡，累加登录次数 |
| GET | `/api/v1/me/progress` | 是 | 读取全部关卡存档与当前关卡 |
| PUT | `/api/v1/me/progress/{level_id}` | 是 | 保存某一关的存档（乐观锁） |
| GET | `/health` | 否 | 健康检查，`{"status":"ok"}` |

## 4. 关卡

### 4.1 关卡列表

```bash
curl -s http://139.196.161.33:8000/api/v1/levels
```

```json
[
  {
    "id": "3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60",
    "name": "青釉三片",
    "sort_order": 1,
    "published_at": "2026-11-20T16:04:07.512931+00:00",
    "difficulty": "入门"
  }
]
```

`sort_order` 就是关卡顺序，从 1 开始连续；草稿关卡不会出现。

### 4.2 关卡详情

```bash
curl -s http://139.196.161.33:8000/api/v1/levels/3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60
```

返回 = 上传的关卡 JSON 全部字段 + 服务端补充的四个字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 关卡 ID |
| `sort_order` | 第几关 |
| `published_at` | 发布时间 |
| `image` | 图集 PNG 的完整 URL，直接 `<img src>` 即可 |

关卡配置结构（与上传的 JSON 一致）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 关卡名 |
| `difficulty` | string | 难度文案 |
| `story` | string | 关卡介绍文本 |
| `rotations` | int 0–3 | 初始允许的旋转次数 |
| `snapDistance` | number > 0 | 吸附距离（图稿坐标单位） |
| `poster` | `[x, y, w, h]` | 完整瓷器在图集中的裁剪矩形，固定 `400 × 450` |
| `outline` | number[] | 瓷器外轮廓，交替排列 `x, y` |
| `shards` | object[] | 碎片数组，**索引即碎片 ID** |
| `shards[].cx` / `.cy` | number | 碎片中心点 |
| `shards[].box` | `[left, top, right, bottom]` | 碎片包围盒，宽高与 `sprite` 一致 |
| `shards[].poly` | number[] | 碎片多边形，交替 `x, y`，至少 3 个点 |
| `shards[].sprite` | `[x, y, w, h]` | 该碎片在图集中的裁剪矩形 |
| `seams` | object[] | 裂边，`{ids:[a,b], poly:[x,y,...]}`，相邻关系连通全部碎片 |
| `points` | `[[x, y], ...]` | 描金点，固定 3 个 |

### 4.3 图集贴图

`GET /api/v1/levels/{level_id}/atlas.png` 返回 `image/png`，带
`Cache-Control: public, max-age=86400, immutable`，可以放心让浏览器缓存。
只有已发布关卡能取到，草稿返回 404。

## 5. 玩家与存档

### 5.1 玩家资料

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://139.196.161.33:8000/api/v1/me
```

```json
{
  "id": "9a1f0c22-5d3b-4e77-8c1a-0b7d2e5f8a31",
  "nickname": "测试玩家",
  "source": "wechat",
  "openid": "oXyz...",
  "last_login_at": "2026-11-20T16:04:07.512931+00:00",
  "login_count": 12,
  "avatar_url": null
}
```

`POST /api/v1/me/login` 用同一份返回结构，仅把 `last_login_at` 更新为当前时间、`login_count` 加 1。
进入游戏时调一次即可，不要每帧调用。

### 5.2 读取存档

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://139.196.161.33:8000/api/v1/me/progress
```

```json
{
  "current_level_id": "3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60",
  "progress": [
    {
      "level_id": "3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60",
      "groups": [{ "ids": [0, 1, 2], "x": 0, "y": 0, "turn": 0 }],
      "paint": [0, 2],
      "stage": "painting",
      "revision": 7,
      "completed": false,
      "first_completed_at": null,
      "saved_at": "2026-11-20T16:04:07.512931+00:00"
    }
  ]
}
```

* `progress` 只包含有存档的关卡，按关卡顺序排列；新玩家是空数组。
* `stage` 由服务端推导，前端不必自己算：
  * `puzzle` — 还有多组碎片没拼合；
  * `painting` — 已合成一组，描金未点满；
  * `complete` — 一组且三个金点都点亮。
* `completed` / `first_completed_at` 记录**首次**通关，重复通关不会刷新。

### 5.3 保存存档

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"groups":[{"ids":[0,1,2],"x":0,"y":0,"turn":0}],"paint":[0,1],"revision":7}' \
  http://139.196.161.33:8000/api/v1/me/progress/3f6c1d9e-0a7b-4c2d-9f18-2b5e7c1d4a60
```

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `groups` | object[] | 当前所有碎片分组，1–256 组；`ids` 必须**无重复地覆盖全部碎片 ID** |
| `groups[].x` / `.y` | number | 该组在拼图板上的位置，`0 ≤ x ≤ 400`、`0 ≤ y ≤ 590` |
| `groups[].turn` | int 0–3 | 该组旋转 90° 的次数 |
| `paint` | int[] | 已点亮的描金点索引，最多 3 个且不重复；**仅在只存在一组碎片时可非空** |
| `revision` | int | 乐观锁版本号。首次保存填 `0`，之后填上次响应里的 `revision` |

成功后返回该关最新的存档对象（结构同 5.2 里的数组元素），`revision` 已加 1，直接存下来下次用。

服务端校验规则与对应错误：

| 场景 | 状态码 | 文案 |
| --- | --- | --- |
| 关卡不存在或未发布 | 404 | 已发布关卡不存在 |
| 上一关未通关就写下一关 | 403 | 关卡尚未解锁 |
| `ids` 没覆盖全部碎片或有重复 | 422 | groups.ids 必须无重复地覆盖所有碎片 |
| 多组时传了 `paint` | 422 | paint 仅在碎片合成一组后可保存 |
| `paint` 重复或索引越界 | 422 | paint 必须为不重复的有效金点索引 |
| `revision` 落后于服务端 | 409 | 修订号不匹配，请重新读取存档 |

**并发与重试**：`revision` 是唯一并发保护。收到 409 时不要盲目重发，
先 `GET /api/v1/me/progress` 拿到最新存档再合并。同一份快照用同一 `revision`
重试是幂等的，会原样返回已存记录，可以安全重试网络超时。

## 6. 联调顺序建议

1. `GET /health` 确认服务活着。
2. `GET /api/v1/levels` 拿关卡 ID；`GET /api/v1/levels/{id}` 拿几何和 `image`。
3. 后台给测试玩家签发令牌，`GET /api/v1/me` 确认身份。
4. `POST /api/v1/me/login` 打卡。
5. `GET /api/v1/me/progress` 恢复存档（新玩家为空）。
6. 玩一局，`PUT /api/v1/me/progress/{id}` 保存；用返回的 `revision` 继续下一次。

## 7. 常见坑

* **请求体多传字段会 422**：模型是 `extra="forbid"`，客户端内部字段（如 `selected`、`dragging`）不要一起提交。
* **`paint` 只能在合成一组后传**，且数量不能超过 3；多组时传非空 `paint` 直接 422。
* **`groups[].ids` 是扁平碎片 ID 列表**，必须覆盖 `0..碎片总数-1`，少一个都不行。
* **`y` 上限是 590 而不是 450**：拼图板比图集高，允许碎片暂放在下方。
* **未发布关卡不可见**：`/levels` 不返回草稿，详情与图集对草稿返回 404。
* **后台是 HTML 页面**，不要指望用 JSON 调 `/admin/*`；管理员操作在浏览器完成。
* **仅内测 HTTP**：`Cookie` 相关的后台登录在 `http://` 下可用，上 HTTPS 后应改回安全 Cookie，不影响 Bearer 接口。
"""
