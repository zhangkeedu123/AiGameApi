import asyncio
import copy
import json
import os
import re
import shutil
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
from httpx import ASGITransport, AsyncClient

from app.create_admin import insert_admin
from app.main import create_app
from app.security import hash_password
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TEST_DATABASE_URL")
PASSWORD = "integration-password"


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def save_body(revision, *, joined=False, paint=(), x=150):
    groups = [[0, 1, 2]] if joined else [[0], [1], [2]]
    return {
        "revision": revision, "paint": list(paint),
        "groups": [{"ids": ids, "x": x, "y": 250, "turn": 0} for ids in groups],
    }


@unittest.skipUnless(DATABASE_URL, "需要 TEST_DATABASE_URL")
class PostgreSQLTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        run_id = uuid4().hex
        directory = ROOT / "var" / f"test_{run_id}"
        directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, directory)
        schema = f"test_{run_id}"
        control = await asyncpg.connect(DATABASE_URL)
        self.addAsyncCleanup(control.close)
        await control.execute(f"CREATE SCHEMA {schema}")
        self.addAsyncCleanup(control.execute, f"DROP SCHEMA {schema} CASCADE")
        url = urlsplit(DATABASE_URL)
        options = dict(parse_qsl(url.query))
        options["search_path"] = schema
        dsn = urlunsplit(url._replace(query=urlencode(options)))
        self.settings = Settings(dsn, "test-secret-" * 4, directory / "uploads",
                                 directory / "errors.log", cookie_secure=False)
        self.schema_sql = (ROOT / "db/01_init_schema.sql").read_text(encoding="utf-8")
        self.seed_sql = (ROOT / "db/02_seed_dev.sql").read_text(encoding="utf-8")
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(self.schema_sql)
            missing_comments = await conn.fetchval("""
                SELECT count(*) FROM pg_class c
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE c.relnamespace = current_schema()::regnamespace
                  AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
                  AND (obj_description(c.oid, 'pg_class') IS NULL
                       OR col_description(c.oid, a.attnum) IS NULL)
            """)
            self.assertEqual(missing_comments, 0)
            await conn.execute(self.seed_sql)
            await conn.execute("UPDATE users SET nickname = 'changed'")
            await conn.execute(self.seed_sql)
            self.assertEqual(await conn.fetch("SELECT nickname FROM users"), [("changed",)])
        finally:
            await conn.close()
        prior = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = dsn
        try:
            self.assertIsNotNone(await insert_admin("admin", await asyncio.to_thread(hash_password, PASSWORD)))
        finally:
            if prior is None:
                os.environ.pop("DATABASE_URL")
            else:
                os.environ["DATABASE_URL"] = prior

    def client(self, app):
        return AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )

    async def login(self, client):
        data = {"username": "admin", "password": PASSWORD}
        self.assertEqual((await client.post("/admin/login", data={**data, "csrf": "bad"})).status_code, 403)
        data["csrf"] = csrf(await client.get("/admin/login"))
        response = await client.post("/admin/login", data=data)
        self.assertEqual(response.status_code, 303, response.text)
        return csrf(await client.get("/admin/levels"))

    async def create_player(self, client, token, nickname):
        response = await client.post("/admin/users", data={"csrf": token, "nickname": nickname})
        self.assertEqual(response.status_code, 303, response.text)
        url = response.headers["location"]
        return url, await self.issue(client, token, url)

    async def issue(self, client, csrf_token, url):
        issued = await client.post(f"{url}/tokens", data={"csrf": csrf_token})
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(issued.headers["cache-control"], "no-store")
        return {"Authorization": "Bearer " + re.search(r'id="issued-token">([^<]+)', issued.text)[1]}

    async def test_upload_publication_and_concurrent_progress(self):
        app = create_app(self.settings)
        async with app.router.lifespan_context(app), self.client(app) as client:
            self.assertEqual((await client.get("/health")).json(), {"status": "ok"})
            self.assertEqual((await client.get("/admin/levels")).status_code, 303)
            self.assertEqual((await client.post("/admin/users", data={"csrf": "bad", "nickname": "a"})).status_code, 401)
            self.assertEqual((await client.get("/api/v1/me/progress")).status_code, 401)
            token = await self.login(client)
            png = (ROOT / "examples/three-shards.png").read_bytes()
            config = json.loads((ROOT / "examples/three-shards.json").read_text(encoding="utf-8"))

            async def upload(image, definition):
                return await client.post("/admin/levels", data={"csrf": token}, files={
                    "atlas": ("atlas.png", image, "image/png"),
                    "config": ("level.json", json.dumps(definition), "application/json"),
                })

            for image, field, value, expected in [
                (b"invalid", "seams", config["seams"], "atlas"),
                (png, "poster", [1, 0, 400, 450], "poster"),
                (png, "seams", [{"ids": [0, 3], "poly": [0, 0, 5, 5]}], "seams.0.ids"),
                (png, "seams", config["seams"][:1], "seams"),
            ]:
                response = await upload(image, {**config, field: value})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(expected, response.text)
            self.assertEqual(await app.state.db.fetchval("SELECT count(*) FROM levels"), 0)
            self.assertEqual(list(self.settings.upload_dir.iterdir()), [])

            level_ids = []
            for number in (1, 2):
                response = await upload(png, {**config, "name": f"关卡 {number}"})
                self.assertEqual(response.status_code, 303, response.text)
                admin_url = response.headers["location"]
                level_id = admin_url.rsplit("/", 1)[1]
                level_ids.append(level_id)
                api_url = f"/api/v1/levels/{level_id}"
                self.assertEqual((await client.get(api_url)).status_code, 404)
                self.assertEqual((await client.get(f"{api_url}/atlas.png")).status_code, 404)
                self.assertEqual((await client.get(f"{admin_url}/atlas.png")).content, png)
                response = await client.post(f"{admin_url}/publish", data={"csrf": token})
                self.assertEqual(response.status_code, 303, response.text)
                detail = (await client.get(api_url)).json()
                self.assertEqual((detail["id"], detail["sort_order"]), (level_id, number))
                self.assertEqual((await client.get(detail["image"])).content, png)
                self.assertEqual(
                    (await client.post(f"{admin_url}/publish", data={"csrf": token})).status_code, 303,
                )
                self.assertEqual((await client.get(api_url)).json(), detail)
            self.assertEqual([item["id"] for item in (await client.get("/api/v1/levels")).json()], level_ids)

            player_url, auth = await self.create_player(client, token, "甲")
            _, other = await self.create_player(client, token, "乙")
            first, second = [f"/api/v1/me/progress/{level_id}" for level_id in level_ids]
            self.assertEqual((await client.put(first, json=save_body(0))).status_code, 401)
            self.assertEqual((await client.put(second, json=save_body(0), headers=auth)).status_code, 403)
            self.assertEqual((await client.put(first, json=save_body(0, paint=[0]), headers=auth)).status_code, 422)
            nonfinite = json.dumps(save_body(0)).replace('"x": 150', '"x": 1e400')
            response = await client.put(
                first, content=nonfinite, headers={**auth, "Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("finite", response.text)
            invalid = copy.deepcopy(save_body(0))
            invalid["groups"][0]["ids"] = [1]
            self.assertEqual((await client.put(first, json=invalid, headers=auth)).status_code, 422)
            invalid["user_id"] = player_url.rsplit("/", 1)[1]
            self.assertEqual((await client.put(first, json=invalid, headers=other)).status_code, 422)

            responses = await asyncio.gather(*(
                client.put(first, json=save_body(0, x=x), headers=auth) for x in (150, 160)
            ))
            self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
            winner = next(r.json() for r in responses if r.status_code == 200)
            replay = await client.put(first, json=save_body(0, x=winner["groups"][0]["x"]), headers=auth)
            self.assertEqual(replay.json(), winner)
            responses = await asyncio.gather(*(
                client.put(first, json=save_body(1, joined=True, x=x), headers=auth) for x in (150, 160)
            ))
            self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
            joined = next(r.json() for r in responses if r.status_code == 200)
            self.assertEqual((joined["stage"], joined["revision"]), ("painting", 2))
            partial = await client.put(first, json=save_body(2, joined=True, paint=[0]), headers=auth)
            self.assertEqual(partial.status_code, 200, partial.text)
            self.assertEqual((await client.get("/api/v1/me/progress", headers=other)).json()["progress"], [])
            self.assertEqual((await client.put(first, json=save_body(0), headers=other)).status_code, 200)
            await app.state.db.execute(self.schema_sql)

        restarted = create_app(self.settings)
        async with restarted.router.lifespan_context(restarted), self.client(restarted) as client:
            restored = (await client.get("/api/v1/me/progress", headers=auth)).json()
            self.assertEqual(restored, {"current_level_id": level_ids[0], "progress": [partial.json()]})
            done = await client.put(first, json=save_body(3, joined=True, paint=[0, 1, 2]), headers=auth)
            self.assertTrue(done.json()["completed"])
            reset = await client.put(first, json=save_body(4), headers=auth)
            self.assertEqual(reset.json()["stage"], "puzzle")
            self.assertEqual(reset.json()["first_completed_at"], done.json()["first_completed_at"])
            self.assertEqual((await client.put(second, json=save_body(0), headers=auth)).status_code, 200)
            self.assertEqual((await client.put(second, json=save_body(0), headers=other)).status_code, 403)
            token = await self.login(client)
            detail = await client.get(player_url)
            self.assertIn("当前关卡 关卡 2", detail.text)
            rotations = await asyncio.gather(*(self.issue(client, token, player_url) for _ in range(2)))
            self.assertEqual((await client.get("/api/v1/me/progress", headers=auth)).status_code, 401)
            statuses = [(await client.get("/api/v1/me/progress", headers=headers)).status_code
                        for headers in rotations]
            self.assertEqual(sorted(statuses), [200, 401])
            rotated = rotations[statuses.index(200)]
            await client.post(f"{player_url}/tokens/revoke", data={"csrf": token})
            self.assertEqual((await client.get("/api/v1/me/progress", headers=rotated)).status_code, 401)

    async def test_unhandled_errors_are_logged(self):
        app = create_app(self.settings)

        @app.get("/test-error")
        async def fail():
            raise RuntimeError("test file logging")

        async with app.router.lifespan_context(app), self.client(app) as client:
            response = await client.get("/test-error")
        self.assertEqual(response.status_code, 500)
        log = self.settings.error_log.read_text(encoding="utf-8")
        self.assertIn(response.json()["error_id"], log)
        self.assertIn("RuntimeError: test file logging", log)

    async def test_player_platform_and_concurrent_login(self):
        app = create_app(self.settings)
        async with app.router.lifespan_context(app), self.client(app) as client:
            self.assertEqual((await client.get("/api/v1/me")).status_code, 401)
            self.assertEqual((await client.post("/api/v1/me/login")).status_code, 401)
            token = await self.login(client)
            fields = {
                "csrf": token, "nickname": "平台玩家", "source": "wechat",
                "openid": "wechat-test-id", "avatar_url": "https://example.test/avatar.png",
            }
            response = await client.post("/admin/users", data=fields)
            self.assertEqual(response.status_code, 303, response.text)
            user_url = response.headers["location"]
            auth = await self.issue(client, token, user_url)
            profile = await client.get("/api/v1/me", headers=auth)
            self.assertEqual(profile.headers["cache-control"], "no-store")
            self.assertEqual(profile.json(), {
                "id": user_url.rsplit("/", 1)[1], "nickname": fields["nickname"],
                "source": "wechat", "openid": fields["openid"], "avatar_url": fields["avatar_url"],
                "last_login_at": None, "login_count": 0,
            })
            edit = {**fields, "source": "douyin", "openid": "douyin-test-id"}
            self.assertEqual((await client.post(f"{user_url}/platform", data=edit)).status_code, 303)
            invalid = {**edit, "source": "other"}
            self.assertEqual((await client.post(f"{user_url}/platform", data=invalid)).status_code, 422)
            self.assertIn("douyin-test-id", (await client.get(user_url)).text)

            legacy = await self.issue(client, token, "/admin/users/d4c82460-f5f4-4267-8c96-3e29d08d87ec")
            old_profile = (await client.get("/api/v1/me", headers=legacy)).json()
            self.assertEqual((old_profile["openid"], old_profile["login_count"]), (None, 0))
            responses = await asyncio.gather(*(
                client.post("/api/v1/me/login", headers=legacy) for _ in range(3)
            ))
            self.assertTrue(all(response.status_code == 200 for response in responses))
            self.assertEqual(sorted(response.json()["login_count"] for response in responses), [1, 2, 3])
            final = (await client.get("/api/v1/me", headers=legacy)).json()
            self.assertEqual(final["login_count"], 3)
            self.assertIsNotNone(final["last_login_at"])
            self.assertEqual((await client.get("/api/v1/me", headers=auth)).json()["login_count"], 0)

    async def test_schema_accepts_null_non_key_fields_and_standalone_ids(self):
        app = create_app(self.settings)
        async with app.router.lifespan_context(app):
            db = app.state.db
            for table in ("admins", "levels", "users", "user_tokens"):
                await db.execute(f"INSERT INTO {table} (id) VALUES ($1), ($2)", uuid4(), uuid4())
            await db.execute("INSERT INTO user_level_progress (user_id, level_id) VALUES ($1, $2)",
                             uuid4(), uuid4())
            required = await db.fetch("""
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = current_schema() AND is_nullable = 'NO'
                ORDER BY table_name, column_name
            """)
            self.assertEqual([tuple(row) for row in required], [
                ("admins", "id"), ("levels", "id"), ("user_level_progress", "level_id"),
                ("user_level_progress", "user_id"), ("user_tokens", "id"), ("users", "id"),
            ])
            foreign_keys = await db.fetchval("""
                SELECT count(*) FROM pg_constraint
                WHERE connamespace = current_schema()::regnamespace AND contype = 'f'
            """)
            self.assertEqual(foreign_keys, 0)
