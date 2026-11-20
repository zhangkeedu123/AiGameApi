# 金缮夜馆后端

使用 FastAPI、asyncpg 和 PostgreSQL。管理员在 `/admin` 管理关卡、玩家和联调令牌，小游戏通过 `/api/v1` 读取关卡并保存进度。API 的交互文档位于 `/docs`。

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

## 管理后台联调

登录 `http://127.0.0.1:8000/admin`，上传 [三片关卡 JSON](examples/three-shards.json) 和 [PNG 图集](examples/three-shards.png)。打开草稿查看图集并发布。再次上传该示例可生成第二关，用于检查顺序解锁。打开玩家页面创建玩家，进入详情页签发或轮换令牌并复制本次显示的值。

下面是等价的 HTTP 请求示例。浏览器提交的写操作需要会话 Cookie 和表单中的 `csrf`。登录会重置 CSRF，登录后需重新读取。请求示例沿用同一个 `$session`。

```powershell
$base = 'http://127.0.0.1:8000'
$page = Invoke-WebRequest -UseBasicParsing "$base/admin/login" -SessionVariable session
$csrf = [regex]::Match($page.Content, 'name="csrf" value="([^"]+)"').Groups[1].Value
$adminCredential = Get-Credential -UserName admin -Message '输入管理员账号和密码'
$page = Invoke-WebRequest -UseBasicParsing "$base/admin/login" -WebSession $session -Method Post -Body @{
    username = $adminCredential.UserName
    password = $adminCredential.GetNetworkCredential().Password
    csrf = $csrf
}
$csrf = [regex]::Match($page.Content, 'name="csrf" value="([^"]+)"').Groups[1].Value
$cookie = $session.Cookies.GetCookieHeader([uri]"$base/admin/levels")

# 303 响应的 Location 提供草稿地址。后续 POST 使用该地址发布。
curl.exe -i -b $cookie -F "csrf=$csrf" -F "atlas=@examples/three-shards.png;type=image/png" -F "config=@examples/three-shards.json;type=application/json" "$base/admin/levels"
$adminLevel = '/admin/levels/替换为草稿ID'
curl.exe -i -b $cookie --data-urlencode "csrf=$csrf" "$base$adminLevel/publish"

# 303 响应的 Location 提供玩家地址。签发响应的 issued-token 元素包含令牌。
curl.exe -i -b $cookie --data-urlencode "csrf=$csrf" --data-urlencode 'nickname=demo' "$base/admin/users"
$adminUser = '/admin/users/替换为玩家ID'
curl.exe -s -b $cookie --data-urlencode "csrf=$csrf" "$base$adminUser/tokens"
# 玩家的联调结束后，可撤销当前令牌
# curl.exe -i -b $cookie --data-urlencode "csrf=$csrf" "$base$adminUser/tokens/revoke"
```

单个玩家同一时刻有一个有效联调令牌。重新签发会替换旧令牌。正式的微信和抖音登录、账号绑定、旧客户端存档导入与小游戏源码接入另行安排。联调令牌仅用于测试，勿打包进小游戏。

后台创建玩家时可填写来源、OpenID 和头像地址，也可在玩家详情补填或修改。来源的接口值为 `wechat` 或 `douyin`，分别对应微信和抖音。填写 OpenID 时需同时指定来源。

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
