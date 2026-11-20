import argparse
import asyncio
import os
from datetime import UTC, datetime
from getpass import getpass
from uuid import uuid4

import asyncpg

from app.security import hash_password


async def insert_admin(username, password_hash):
    connection = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        return await connection.fetchval(
            """INSERT INTO admins (id, username, password_hash, active, created_at)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (username) DO NOTHING RETURNING id""",
            uuid4(), username, password_hash, True, datetime.now(UTC),
        )
    finally:
        await connection.close()


def main():
    parser = argparse.ArgumentParser(description="创建管理员账号")
    parser.add_argument("username")
    username = parser.parse_args().username.strip()
    if not 1 <= len(username) <= 120 or "\0" in username:
        parser.error("账号须为 1 至 120 个有效字符")
    password = getpass("Password: ")
    if password != getpass("Repeat password: "):
        parser.error("两次密码不一致")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        parser.error(str(exc))
    if not asyncio.run(insert_admin(username, password_hash)):
        parser.error("管理员已存在")
    print("管理员已创建")


if __name__ == "__main__":
    main()
