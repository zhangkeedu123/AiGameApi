from html import escape
from secrets import token_urlsafe
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.security import check_csrf, csrf_token, verify_password
from app.services.levels import publish_level, upload_level
from app.services.players import (
    PLAYER_SOURCES, create_player, get_profile, platform_fields, update_platform, update_token,
)
from app.services.progress import read_progress

# 后台是服务端渲染的 HTML 表单页，不是 JSON 接口：不进入 OpenAPI，
# 免得前端在 Swagger 里把 /admin/* 当成可调用的 API（它们需要会话和 CSRF）。
router = APIRouter(prefix="/admin", include_in_schema=False)


def h(value):
    return escape(str(value))


def page(title, content, status=200):
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{h(title)} · 金缮夜馆</title>
        <style>
        body{{font:16px/1.6 system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}}
        label{{display:block;margin:.6rem 0}} input,button{{font:inherit}}
        nav a,form button{{margin-right:.8rem}} img{{max-width:100%;height:auto}}
        td,th{{padding:.4rem .8rem;text-align:left}} code{{overflow-wrap:anywhere}}
        </style>
        <nav><a href="/admin/levels">关卡</a><a href="/admin/users">玩家</a></nav>
        <main><h1>{h(title)}</h1>{content}</main></html>""", status_code=status,
    )


def form(request, action, fields, button, upload=False):
    encoding = ' enctype="multipart/form-data"' if upload else ""
    return (
        f'<form method="post" action="{h(action)}"{encoding}>'
        f'<input type="hidden" name="csrf" value="{h(csrf_token(request))}">'
        f'{fields}<button type="submit">{h(button)}</button></form>'
    )


async def admin_required(request: Request):
    admin_id = request.session.get("admin_id")
    if admin_id and await request.app.state.db.fetchval(
        "SELECT active FROM admins WHERE id = $1", UUID(admin_id)
    ):
        return
    if request.method == "GET":
        raise HTTPException(303, headers={"Location": "/admin/login"})
    raise HTTPException(401, "需要管理员登录")


protected = [Depends(admin_required)]


def login_page(request, error="", status=200):
    fields = (
        '<label>账号 <input name="username" required autocomplete="username"></label>'
        '<label>密码 <input type="password" name="password" required autocomplete="current-password"></label>'
    )
    return page("管理员登录", f"<p>{h(error)}</p>" +
                form(request, "/admin/login", fields, "登录"), status)


@router.get("")
async def admin_home():
    return RedirectResponse("/admin/levels", 303)


@router.get("/login")
async def login(request: Request):
    return login_page(request)


@router.post("/login")
async def submit_login(
    request: Request, username: str = Form(max_length=120),
    password: str = Form(max_length=256), csrf: str = Form(),
):
    check_csrf(request, csrf)
    if "\0" in username:
        return login_page(request, "账号或密码错误", 401)
    row = await request.app.state.db.fetchrow(
        "SELECT id, password_hash FROM admins WHERE username = $1 AND active", username.strip(),
    )
    if row is None or not await verify_password(password, row["password_hash"]):
        return login_page(request, "账号或密码错误", 401)
    request.session.clear()
    request.session["admin_id"] = str(row["id"])
    csrf_token(request)
    return RedirectResponse("/admin/levels", 303)


@router.post("/logout", dependencies=protected)
async def logout(request: Request, csrf: str = Form()):
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/admin/login", 303)


@router.get("/levels", dependencies=protected)
async def levels_page(request: Request):
    rows = await request.app.state.db.fetch(
        "SELECT id, name, status, sort_order FROM levels ORDER BY sort_order NULLS LAST, created_at"
    )
    items = []
    for row in rows:
        url = f"/admin/levels/{row['id']}"
        status = f"已发布 · 第 {row['sort_order']} 关" if row["sort_order"] else "草稿"
        items.append(f'<li><a href="{url}">{h(row["name"])}</a> · {status}</li>')
    fields = (
        '<label>图集 PNG <input type="file" name="atlas" accept=".png" required></label>'
        '<label>关卡 JSON <input type="file" name="config" accept=".json" required></label>'
    )
    content = form(request, "/admin/logout", "", "退出登录")
    content += form(request, "/admin/levels", fields, "上传草稿", upload=True)
    return page("关卡管理", content + "<ul>" + "".join(items) + "</ul>")


@router.post("/levels", dependencies=protected)
async def upload(
    request: Request, atlas: UploadFile = File(), config: UploadFile = File(), csrf: str = Form(),
):
    check_csrf(request, csrf)
    try:
        level_id = await upload_level(request.app.state.db, request.app.state.settings.upload_dir, atlas, config)
    except HTTPException as exc:
        return page("上传失败", f'<p>{h(exc.detail)}</p><a href="/admin/levels">返回关卡</a>', exc.status_code)
    return RedirectResponse(f"/admin/levels/{level_id}", 303)


async def admin_level(request, level_id):
    row = await request.app.state.db.fetchrow("SELECT * FROM levels WHERE id = $1", level_id)
    if row is None:
        raise HTTPException(404, "关卡不存在")
    return row


@router.get("/levels/{level_id}", dependencies=protected)
async def level_page(request: Request, level_id: UUID):
    row = await admin_level(request, level_id)
    url = f"/admin/levels/{level_id}"
    if row["status"] == "draft":
        publication = "<p>草稿</p>" + form(request, f"{url}/publish", "", "发布关卡")
    else:
        publication = f"<p>已发布 · 第 {row['sort_order']} 关</p>"
    return page(row["name"], publication + f'<img src="{url}/atlas.png" alt="拼图图集预览">')


@router.get("/levels/{level_id}/atlas.png", dependencies=protected)
async def preview(request: Request, level_id: UUID):
    row = await admin_level(request, level_id)
    return FileResponse(request.app.state.settings.upload_dir / row["storage_key"], media_type="image/png")


@router.post("/levels/{level_id}/publish", dependencies=protected)
async def publish(request: Request, level_id: UUID, csrf: str = Form()):
    check_csrf(request, csrf)
    await publish_level(request.app.state.db, level_id)
    return RedirectResponse(f"/admin/levels/{level_id}", 303)


def platform_inputs(source=None, openid=None, avatar_url=None):
    options = ['<option value="">未设置</option>']
    for value, label in PLAYER_SOURCES.items():
        selected = " selected" if value == source else ""
        options.append(f'<option value="{value}"{selected}>{label}</option>')
    return (
        f'<label>来源 <select name="source">{"".join(options)}</select></label>'
        f'<label>OpenID <input name="openid" maxlength="255" value="{h(openid or "")}"></label>'
        f'<label>头像地址 <input name="avatar_url" type="url" maxlength="2048" value="{h(avatar_url or "")}"></label>'
    )


@router.get("/users", dependencies=protected)
async def users_page(request: Request):
    rows = await request.app.state.db.fetch("SELECT id, nickname, source FROM users ORDER BY created_at, id")
    items = "".join(
        f'<li><a href="/admin/users/{row["id"]}">{h(row["nickname"] or "未设置昵称")}</a>'
        f' · {PLAYER_SOURCES.get(row["source"], "来源未设置")}</li>' for row in rows
    )
    fields = '<label>昵称 <input name="nickname" maxlength="80" required></label>'
    return page("玩家管理", form(request, "/admin/users", fields + platform_inputs(), "创建玩家")
                + f"<ul>{items}</ul>")


@router.post("/users", dependencies=protected)
async def create_user(
    request: Request, nickname: str = Form(max_length=80), csrf: str = Form(),
    source: str = Form("", max_length=16), openid: str = Form("", max_length=255),
    avatar_url: str = Form("", max_length=2048),
):
    check_csrf(request, csrf)
    nickname = nickname.strip()
    if not nickname or "\0" in nickname:
        raise HTTPException(422, "nickname 必须为有效昵称")
    user_id = await create_player(
        request.app.state.db, nickname, *platform_fields(source, openid, avatar_url),
    )
    return RedirectResponse(f"/admin/users/{user_id}", 303)


@router.get("/users/{user_id}", dependencies=protected)
async def user_page(request: Request, user_id: UUID):
    db = request.app.state.db
    profile = await get_profile(db, user_id)
    data = await read_progress(db, user_id)
    current = await db.fetchval("SELECT name FROM levels WHERE id = $1", data["current_level_id"])
    names = {r["id"]: r["name"] for r in await db.fetch("SELECT id, name FROM levels WHERE status = 'published'")}
    stages = {"puzzle": "拼图", "painting": "描金", "complete": "全部完成"}
    rows = []
    for item in data["progress"]:
        rows.append(
            f'<tr><td>{h(names[item["level_id"]])}</td><td>{stages[item["stage"]]}</td>'
            f'<td>{len(item["paint"])} / 3</td><td>{h(item["first_completed_at"] or "待通关")}</td>'
            f'<td>{h(item["saved_at"])}</td></tr>'
        )
    url = f"/admin/users/{user_id}"
    fields = platform_inputs(profile["source"], profile["openid"], profile["avatar_url"])
    return page(profile["nickname"] or "未设置昵称",
                f"<p>玩家 ID <code>{user_id}</code></p><p>当前关卡 {h(current or '待开始')}</p>"
                f"<p>最近登录 {h(profile['last_login_at'] or '暂无记录')} · 登录次数 {profile['login_count']}</p>"
                + form(request, f"{url}/platform", fields, "保存平台资料")
                + form(request, f"{url}/tokens", "", "签发或轮换令牌")
                + form(request, f"{url}/tokens/revoke", "", "撤销令牌")
                + "<table><tr><th>关卡</th><th>阶段</th><th>描金</th><th>首次通关</th><th>最后保存</th></tr>"
                + "".join(rows) + "</table>")


@router.post("/users/{user_id}/platform", dependencies=protected)
async def edit_user_platform(
    request: Request, user_id: UUID, csrf: str = Form(),
    source: str = Form("", max_length=16), openid: str = Form("", max_length=255),
    avatar_url: str = Form("", max_length=2048),
):
    check_csrf(request, csrf)
    await update_platform(request.app.state.db, user_id, *platform_fields(source, openid, avatar_url))
    return RedirectResponse(f"/admin/users/{user_id}", 303)


@router.post("/users/{user_id}/tokens", dependencies=protected)
async def issue_token(request: Request, user_id: UUID, csrf: str = Form()):
    check_csrf(request, csrf)
    token = token_urlsafe(32)
    await update_token(request.app.state.db, user_id, token)
    return page("玩家联调令牌", f'<p>令牌仅展示一次。<br><code id="issued-token">{h(token)}</code></p>'
                f'<a href="/admin/users/{user_id}">返回玩家详情</a>')


@router.post("/users/{user_id}/tokens/revoke", dependencies=protected)
async def revoke_token(request: Request, user_id: UUID, csrf: str = Form()):
    check_csrf(request, csrf)
    await update_token(request.app.state.db, user_id)
    return RedirectResponse(f"/admin/users/{user_id}", 303)
