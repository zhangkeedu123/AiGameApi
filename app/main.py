import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware

from app.api_guide import API_GUIDE
from app.db import connect_pool
from app.openapi import build_openapi
from app.routes import admin, api
from app.settings import Settings

# Swagger UI 资源随代码分发，内网和国内网络都不必再访问 jsdelivr 之类的 CDN。
SWAGGER_ASSETS = Path(__file__).parent / "static" / "swagger"


class PrivateResponseHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # 会话页面、签发结果和玩家进度禁止由浏览器或代理缓存。
        if scope["type"] != "http" or not scope["path"].startswith(("/admin", "/api/v1/me")):
            return await self.app(scope, receive, send)

        async def send_private(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_private)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    logger = logging.getLogger(f"jinyao.errors.{uuid4()}")
    logger.propagate = False

    @asynccontextmanager
    async def lifespan(app):
        settings.error_log.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            settings.error_log, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        try:
            settings.upload_dir.mkdir(parents=True, exist_ok=True)
            app.state.db = await connect_pool(settings.database_url, settings.pool_max)
            try:
                yield
            finally:
                await app.state.db.close()
        except Exception:
            logger.exception("Application lifecycle failed")
            raise
        finally:
            logger.removeHandler(handler)
            handler.close()

    app = FastAPI(title="金缮夜馆 API", version="1.0.0", lifespan=lifespan, docs_url=None)
    # 用自定义生成器补上中文接口说明、示例和鉴权说明，见 app/openapi.py。
    app.openapi = lambda: build_openapi(app)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware, secret_key=settings.session_secret,
        session_cookie="jinyao_admin", same_site="lax", https_only=settings.cookie_secure,
        max_age=4 * 60 * 60, path="/admin",
    )
    app.add_middleware(PrivateResponseHeaders)
    # 最后添加的中间件在最外层：预检请求要在会话和响应头处理之前就被 CORS 拦下。
    # 小游戏用 Bearer 令牌、不依赖 Cookie，所以 allow_credentials 保持关闭。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Content-Length"],
        max_age=600,
    )
    app.include_router(admin.router)
    app.include_router(api.router)

    if (SWAGGER_ASSETS / "swagger-ui-bundle.js").is_file():
        app.mount("/static/swagger", StaticFiles(directory=SWAGGER_ASSETS), name="swagger-ui")

        @app.get("/docs", include_in_schema=False)
        async def swagger_ui():
            return get_swagger_ui_html(
                openapi_url=app.openapi_url,
                title=f"{app.title} - Swagger UI",
                swagger_js_url="/static/swagger/swagger-ui-bundle.js",
                swagger_css_url="/static/swagger/swagger-ui.css",
                swagger_favicon_url="/static/swagger/favicon-32x32.png",
            )

    @app.get(
        "/api.md",
        tags=["运维"],
        operation_id="getApiGuide",
        summary="纯文本联调指南（Markdown）",
        description="""面向前端和 AI 的整段联调说明：接口清单、curl 示例、
        关卡字段结构、存档并发规则、错误码与常见坑。内容与 `/openapi.json` 同源，
        但更适合直接贴进上下文或写进前端 README。""",
        response_class=Response,
        responses={200: {"content": {"text/markdown": {"schema": {"type": "string"}}}}},
    )
    async def api_guide():
        return Response(content=API_GUIDE, media_type="text/markdown; charset=utf-8")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # 只返回字段和原因，避免回显令牌、密码及无法序列化的非有限数值。
        errors = [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]
        return JSONResponse(
            {"detail": errors}, status_code=422, headers={"Cache-Control": "no-store"}
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        error_id = uuid4().hex
        route = getattr(request.scope.get("route"), "path", "<unmatched>")
        await run_in_threadpool(
            logger.error, "%s %s error_id=%s", request.method, route, error_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            {"detail": "服务内部错误", "error_id": error_id}, status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/health",
        tags=["运维"],
        operation_id="health",
        summary="健康检查",
        description="执行一次 `SELECT 1` 探活；数据库不可用时返回 503。用于部署脚本和监控。",
        responses={
            200: {"content": {"application/json": {"example": {"status": "ok"}}}},
            503: {"content": {"application/json": {"example": {"status": "unavailable"}}}},
        },
    )
    async def health(request: Request):
        try:
            await request.app.state.db.fetchval("SELECT 1", timeout=2)
        except Exception as exc:
            await run_in_threadpool(
                logger.error, "Health check: PostgreSQL unavailable",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    return app
