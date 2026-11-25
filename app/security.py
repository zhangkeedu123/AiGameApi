from hashlib import sha256
from secrets import compare_digest, token_urlsafe

import bcrypt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

bearer = HTTPBearer(auto_error=False)


def token_digest(token):
    return sha256(token.encode()).digest()


def hash_password(password):
    if len(password) < 6:
        raise ValueError("密码至少需要 6 个字符")
    raw = password.encode()
    if len(raw) > 72:
        raise ValueError("密码不能超过 72 个 UTF-8 字节")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode()


async def verify_password(password, password_hash):
    raw = password.encode()
    if not 1 <= len(raw) <= 72:
        return False
    return await run_in_threadpool(bcrypt.checkpw, raw, password_hash.encode())


async def player(request: Request, auth: HTTPAuthorizationCredentials = Depends(bearer)):
    if auth and len(auth.credentials) <= 128:
        user_id = await request.app.state.db.fetchval(
            """SELECT u.id FROM user_tokens t JOIN users u ON u.id = t.user_id
               WHERE t.token_digest = $1 AND t.revoked_at IS NULL AND u.active""",
            token_digest(auth.credentials),
        )
        if user_id:
            return user_id
    raise HTTPException(401, "需要有效的玩家令牌", headers={"WWW-Authenticate": "Bearer"})


def csrf_token(request):
    return request.session.setdefault("csrf", token_urlsafe(32))


def check_csrf(request, submitted):
    saved = request.session.get("csrf")
    if not saved or not submitted.isascii() or not compare_digest(saved, submitted):
        raise HTTPException(403, "CSRF 校验失败")
