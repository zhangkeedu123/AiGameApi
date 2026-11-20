import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware

from app.db import connect_pool
from app.routes import admin, api
from app.settings import Settings


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

    app = FastAPI(title="金缮夜馆 API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware, secret_key=settings.session_secret,
        session_cookie="jinyao_admin", same_site="lax", https_only=settings.cookie_secure,
        max_age=4 * 60 * 60, path="/admin",
    )
    app.add_middleware(PrivateResponseHeaders)
    app.include_router(admin.router)
    app.include_router(api.router)

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

    @app.get("/health")
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
