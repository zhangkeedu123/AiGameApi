# 金缮夜馆后端11

使用 FastAPI、asyncpg 和 PostgreSQL。管理员在 `/admin` 管理关卡、玩家和联调令牌，小游戏通过 `/api/v1` 读取关卡并保存进度。

接口文档有三个入口，内容同源：

| 入口 | 用途 |
| --- | --- |
| `/docs` | Swagger UI，可直接发请求；静态资源随镜像分发，不依赖外网 CDN |
| `/openapi.json` | OpenAPI 3.1 描述，可导入 Apifox / Postman / 代码生成器 |
| `/api.md` | 纯文本联调指南：接口清单、curl 示例、存档并发规则、常见坑，适合直接贴给 AI |

逐接口说明写在 `app/routes/api.py` 的 `summary` / `description` 里，跨接口约定（鉴权、错误结构、坐标系）与分组说明集中在 `app/openapi.py`，纯文本指南在 `app/api_guide.py`。改接口后需要同步这三处，否则文档与实现不一致。`/admin` 是服务端渲染的 HTML 页面，需要会话与 CSRF，不列入 OpenAPI。

## Linux Docker 部署

在服务器进入 `AiGameApi`，准备应用运行目录和配置。密码含有 URL 保留字符时，单独对连接串中的密码做 URL 编码。

```bash
sudo install -d -m 0750 -o 10001 -g 10001 /srv/jinyao/api
install -m 0600 .env.docker.example .env.docker
```

**复用已有 PostgreSQL。** 将 `.env.docker` 的 `DATABASE_URL` 改为现有应用库的连接串，并设置至少 32 个字符的随机 `SESSION_SECRET`。数据库装在同一 Linux 宿主机时，连接串主机可填写 `host.docker.internal`，PostgreSQL 必须允许来自 Docker 网桥的连接。

如果已有 PostgreSQL 实例尚无项目专用数据库，用具备权限的角色在该实例执行 `db/00_create_database.sql`。以下是在使用本机 peer 认证的 Linux PostgreSQL 上执行的示例。

```bash
read -rsp '设置 jinyao_app 应用账号密码: ' JINYAO_DB_PASSWORD
printf '\n'
export JINYAO_DB_PASSWORD
sudo --preserve-env=JINYAO_DB_PASSWORD -u postgres psql -X -d postgres \
  -v db_name=jinyao -v app_role=jinyao_app -f - < db/00_create_database.sql
unset JINYAO_DB_PASSWORD
psql -X -h 127.0.0.1 -U jinyao_app -d jinyao -v ON_ERROR_STOP=1 -f db/01_init_schema.sql
```

如果应用库已存在，使用现有应用账号凭据，并由具备表结构权限的角色执行 `db/01_init_schema.sql`。确认连接串和表结构就绪后启动 API。

```bash
docker compose --env-file .env.docker up -d --build --wait
curl -fsS http://127.0.0.1:8000/health
docker compose --env-file .env.docker exec api python -m app.create_admin admin
```

**从空目录新建 PostgreSQL。** 保留连接串主机 `db`，在 `.env.docker` 填写 `POSTGRES_PASSWORD` 和 `JINYAO_DB_PASSWORD`，并把连接串的密码设为应用账号的密码。首次启动会执行现有建库和建表脚本，数据库通过 Compose 网络提供给 API。

```bash
sudo install -d -m 0750 /srv/jinyao/postgres/data
docker compose --env-file .env.docker -f compose.yaml -f compose.postgres.yaml up -d --build --wait
curl -fsS http://127.0.0.1:8000/health
```

新建容器数据库将数据写入宿主机的 `PG_DATA_DIR`，默认是 `/srv/jinyao/postgres/data`。删除容器或镜像后可用同一个目录和 PostgreSQL 17 镜像重建。已有宿主机数据库继续使用其原数据目录。图集和错误日志写入 `API_STATE_DIR`，默认位于 `/srv/jinyao/api`。API 默认通过回环地址发布，公网访问由 HTTPS 反代提供。反代场景可将 `FORWARDED_ALLOW_IPS` 设置为实际可信的反代地址。

## 安装和首次部署

准备 Python 3.11+、PostgreSQL 15+ 及对应的 `psql` 客户端。在仓库根目录执行下列 PowerShell 命令。

```powershell
cd AiGameApi
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

1. 在维护库以具备 `CREATEROLE`、`CREATEDB` 及必要角色权限的部署账号执行建库脚本。以下示例使用本地 PostgreSQL 管理账号。生产部署可由密钥管理器注入 `JINYAO_DB_PASSWORD`。已有应用库和角色时可跳过此步。

   ```powershell
   $appCredential = Read-Host '输入应用数据库角色密码' -AsSecureString
   $env:JINYAO_DB_PASSWORD = [System.Net.NetworkCredential]::new('', $appCredential).Password
   psql -X -h 127.0.0.1 -U postgres -d postgres -v db_name=jinyao -v app_role=jinyao_app -f db/00_create_database.sql
   Remove-Item Env:JINYAO_DB_PASSWORD
   ```

2. 在应用库以库所有者或拥有目标 schema 的建表权限的角色执行结构脚本。应用角色应对相关表具备读写权限。开发库可追加第三条脚本，重复导入会保留已存在的演示玩家及其进度。

   ```powershell
   $env:PGCLIENTENCODING = 'UTF8'
   psql -X -h 127.0.0.1 -U jinyao_app -d jinyao -v ON_ERROR_STOP=1 -f db/01_init_schema.sql
   psql -X -h 127.0.0.1 -U jinyao_app -d jinyao -v ON_ERROR_STOP=1 -f db/02_seed_dev.sql
   ```

   `01_init_schema.sql` 定义主键、唯一约束、索引和 PostgreSQL 数据字典中的中文表注释与字段注释。所有非主键字段允许 `NULL`，关联 ID 使用普通 UUID 字段。发布、账号初始状态、令牌轮换和通关规则由应用代码处理。

   已使用本项目当前结构的库，可以重新执行 `db/01_init_schema.sql` 补齐玩家平台资料和登录统计字段，已有数据会保留。其他版本或项目的同名表需要先检查其 DDL。

3. 根据 [配置示例](.env.example) 在进程环境中设置运行参数。连接串中的密码特殊字符需要进行 URL 编码。以下为本地 HTTP 联调示例，线上使用 HTTPS 并设置 `COOKIE_SECURE=true`。

   ```powershell
   $env:DATABASE_URL = 'postgresql://jinyao_app:replace_me@127.0.0.1:5432/jinyao'
   $env:SESSION_SECRET = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
   $env:COOKIE_SECURE = 'false'
   .\.venv\Scripts\python.exe -m app.create_admin admin
   .\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
   ```

管理员创建命令会隐藏密码输入并二次确认。重启时保持同一个 `SESSION_SECRET`。默认上传目录为 `var/uploads`，需与数据库一起持久化。默认错误日志为 `var/logs/errors.log`，5 MiB 轮转并保留 3 份归档。500 响应的 `error_id` 可用于匹配异常堆栈。单个 Uvicorn worker 通过异步连接池处理并发请求。部署多个进程时给每个进程设置独立的 `ERROR_LOG`，多个实例需共享持久化图集目录。HTTPS 反代需传递可信的主机和协议，以生成正确的图集 URL，并将请求体限制设为 13 MiB。

## 管理后台

管理页面位于同级 `AiGameAdmin`，请按该项目的 README 启动。API 保留 `/admin/*` 的 JSON 管理接口。先请求 `GET /admin/session` 获取会话和 CSRF，提交表单登录后使用新的 CSRF。所有后续写请求用 `X-CSRF-Token` 头。

单图上传由管理员调用 `POST /admin/levels/generate`，字段为 `image`、`name`、`pieces`、`rotate`。旧图集加 JSON 可继续用 `POST /admin/levels`。发布后小游戏可获取图集和关卡；删除草稿清理图集，删除已发布关卡执行下架并保留历史。

## 小游戏 API

| 接口 | 响应 |
| --- | --- |
| `GET /health` | PostgreSQL 可用时返回 200，数据库不可用时返回 503 |
| `GET /api/v1/levels` | 按 `sort_order` 升序排列的已发布关卡摘要，`id` 是稳定 UUID |
| `GET /api/v1/levels/{level_id}` | 关卡完整配置、`id`、发布顺序、发布时间及 PNG 的绝对 URL `image` |
| `GET /api/v1/me` | 用玩家令牌读取自己的资料，含 `source`、`openid`、`last_login_at`、`login_count` 和 `avatar_url` |
| `POST /api/v1/me/login` | 用玩家令牌登记一次成功登录，更新最近登录时间、累计次数并返回玩家资料。请求无需提交正文 |
| `GET /api/v1/me/progress` | `current_level_id` 和 `progress` 数组，无存档时分别为 `null` 和 `[]` |
| `PUT /api/v1/me/progress/{level_id}` | 保存完整快照，返回保存结果和最新修订号 |

```powershell
$levels = Invoke-RestMethod "$base/api/v1/levels"
$levelId = $levels[0].id
Invoke-RestMethod "$base/api/v1/levels/$levelId"
$auth = @{ Authorization = 'Bearer 替换为签发的令牌' }
Invoke-RestMethod "$base/api/v1/me/login" -Method Post -Headers $auth
Invoke-RestMethod "$base/api/v1/me" -Headers $auth
$snapshot = @{
    revision = 0
    groups = @(@{ ids = @(0,1,2); x = 200; y = 290; turn = 0 })
    paint = @(0)
}
Invoke-RestMethod "$base/api/v1/me/progress/$levelId" -Method Put -Headers $auth -ContentType 'application/json' -Body ($snapshot | ConvertTo-Json -Depth 8)
Invoke-RestMethod "$base/api/v1/me/progress" -Headers $auth
```

每次成功调用 `POST /api/v1/me/login` 计一次登录。读取资料、保存进度及后台签发令牌均不改变登录统计。历史玩家从零次开始累计，首次登录前的 `last_login_at` 为 `null`。

首存传 `revision: 0`，更新时传上次读到的修订号。存档项包含 `level_id`、`groups`、`paint`、`revision`、`stage`、`completed`、`first_completed_at`、`saved_at`。阶段为 `puzzle`、`painting` 或 `complete`。`completed` 表示曾经通关，重开时仍保留。第一关默认解锁，达到单组且三个金点全部点亮后解锁下一关。

| 状态 | 含义 |
| --- | --- |
| 401 | 进度接口的令牌缺失或无效 |
| 403 | 关卡尚未解锁，或管理写请求的 CSRF 无效 |
| 404 | 关卡不存在或未发布 |
| 409 | 修订号冲突。重新获取进度再处理。重复提交当前相同的快照可返回原结果 |
| 422 | 请求字段或关卡内容校验失败，查看响应的 `detail` |

## 每日一瓷与实物兑换

升级已有数据库时，先使用应用账号重新执行 `db/01_init_schema.sql`。脚本会新增每日挑战与兑换记录表，保留已有数据，旧关卡默认归入普通分类。随后部署 API 和管理后台。

后台在「瓷器关卡」为瓷器选择普通、困难、极难或每日一瓷分类并上架。每日挑战按北京时间从上架的「每日一瓷」分类选题，当天分配后保持固定；此前的每日挑战和收藏仍可查阅。

同一玩家每天首次完成线上每日一瓷时计入 1 件可兑换藏品。每份实物申请消耗 30 件额度，完整收藏记录和剩余额度继续保留。后台「瓷器兑换」负责处理收货信息和更新物流。发货或签收时需要物流公司和单号。

正式环境的兑换申请需通过 HTTPS 提交。反向代理须转发 `X-Forwarded-Proto`，并在 API 的 `FORWARDED_ALLOW_IPS` 中配置可信的代理地址。本机环回地址可用于开发联调。

## 关卡生成、版本与月榜

升级时重新运行 `db/01_init_schema.sql`。已有上架关卡保留原顺序编号，草稿可在上架时自动分配编号。编号同分类唯一，小游戏按编号从小到大读取已上架关卡。

后台生成角状多边形切片，参数包括拼合片数、需要旋转的拼合片数、同一原图生成的干扰片数。保存原始上传文件，修改关卡时可使用原图重新生成或替换原图。没有原图的旧关卡可从现有图集提取瓷器。重新生成产生新的图集版本，历史收藏和已开始的每日一瓷继续读取原版本。

闯关以实际通关编号倒序、用时升序排名。每名玩家每月取一条最佳成绩，完全相同则先登记的成绩优先。服务端按北京时间登记与切换月份。历史月份榜单或相关玩家收藏首次查询时结算该月冠军，冠军可在藏品阁提交一次收货信息，月榜奖品独立于 30 件藏品兑换。管理员可在「瓷器兑换」查看奖品来源和发货状态。

## 代码与验证

- `app/routes/` 处理 HTTP 请求、后台页面和响应。
- `app/services/` 实现图集校验、发布和进度事务。
- `app/db.py` 管理异步连接池和 JSONB 编解码，`app/schemas.py` 定义请求结构。
- `app/security.py` 实现会话 CSRF、密码校验和玩家令牌鉴权。

为测试提供独立的 PostgreSQL 库及具备 CREATE SCHEMA 权限的账号。测试会创建并清理随机命名的 schema。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:TEST_DATABASE_URL = 'postgresql://jinyao_app:replace_me@127.0.0.1:5432/jinyao_test'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Windows 本地 npm 启动

在本地 `.env` 填写 `LOCAL_DATABASE_HOST=服务器数据库地址`，并在 `.env.docker` 配置数据库连接串和 `SESSION_SECRET`。`npm start` 使用 `.env` 指定的数据库主机，沿用连接串里的端口、库名和账号凭据。

```powershell
npm start
```

在另一个 PowerShell 窗口执行 `npm stop` 可以停止本地 API，也可以在运行窗口按 `Ctrl+C`。需要用 Docker 启动或停止时，分别运行 `npm run start:docker` 和 `npm run stop:docker`。

## 配置文件与密钥

本机配置文件都在 `.gitignore` 里，**不要提交**。仓库只保留模板，新环境复制后自行填写：

| 文件 | 是否入库 | 用途 | 模板 |
| --- | --- | --- | --- |
| `.env.local` | 否 | `dev.cmd` 直接读取，本机连开发库 | 参照 `.env.example` |
| `.env` | 否 | `npm start` 读取 `LOCAL_DATABASE_HOST` | 参照 `.env.example` |
| `.env.docker` | 否 | 服务器 Compose 与 `npm start` 的连接串、`SESSION_SECRET` | [`.env.docker.example`](.env.docker.example) |
| `.env.example` | 是 | 本机进程环境变量模板 | — |
| `.env.docker.example` | 是 | Docker 部署模板 | — |

规则：`DATABASE_URL` 里的密码、`SESSION_SECRET`、`POSTGRES_PASSWORD`、`JINYAO_DB_PASSWORD` 只写在本机文件或服务器环境里，不写进代码、文档和提交记录。`SESSION_SECRET` 至少 32 个字符且重启不更换，否则会话失效。`.env.*` 其余文件、`.vscode/`、`var/`、`node_modules/` 同样被忽略。
