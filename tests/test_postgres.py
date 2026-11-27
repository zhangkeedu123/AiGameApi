import asyncio
import copy
import io
import json
import os
import shutil
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.create_admin import insert_admin
from app.main import create_app
from app.security import hash_password
from app.services.campaign import current_month
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TEST_DATABASE_URL")
PASSWORD = "integration-password"


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
        self.schema_sql = "\n".join(line for line in (ROOT / "db/01_init_schema.sql").read_text(encoding="utf-8").splitlines()
                                     if not line.startswith(chr(92)))
        self.seed_sql = "\n".join(line for line in (ROOT / "db/02_seed_dev.sql").read_text(encoding="utf-8").splitlines()
                                     if not line.startswith(chr(92)))
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(self.schema_sql)
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

    async def test_daily_collection_and_repeat_redemption(self):
        from datetime import timedelta
        from app.services.collection import today

        app = create_app(self.settings)
        async with app.router.lifespan_context(app), self.client(app) as client:
            csrf = await self.login(client)
            admin_headers = {"X-CSRF-Token": csrf}
            source = io.BytesIO()
            Image.new("RGB", (80, 100), "teal").save(source, format="PNG")
            draft = await client.post("/admin/levels/generate", headers=admin_headers,
                                      files={"image": ("vase.png", source.getvalue(), "image/png")},
                                      data={"name": "瓷器", "pieces": "5", "category": "normal"})
            self.assertEqual(draft.status_code, 201, draft.text)
            level_id = draft.json()["id"]
            self.assertEqual((await client.post(f"/admin/levels/{level_id}/publish", headers=admin_headers)).status_code, 200)
            edited = await client.patch(f"/admin/levels/{level_id}", headers=admin_headers,
                                        data={"name": "青瓷", "category": "hard", "story": "瓷器介绍"})
            self.assertEqual(edited.status_code, 200, self.settings.error_log.read_text(encoding="utf-8") if edited.status_code == 500 else edited.text)
            self.assertEqual((await client.delete(f"/admin/levels/{level_id}", headers=admin_headers)).status_code, 200)
            self.assertEqual((await client.get("/api/v1/levels?category=hard")).json(), [])
            self.assertEqual((await client.post(f"/admin/levels/{level_id}/publish", headers=admin_headers)).status_code, 200)
            self.assertEqual((await client.get("/api/v1/levels?category=hard")).json()[0]["category"], "hard")

            guest = await client.post("/api/v1/me/guest", json={"device_id": "daily-test-device"})
            self.assertEqual(guest.status_code, 200, guest.text)
            player = guest.json()
            headers = {"Authorization": f"Bearer {player['token']}"}
            daily = (await client.post("/api/v1/me/daily/start", headers=headers)).json()
            group = {"ids": list(range(len(daily["level"]["shards"]))), "x": 10, "y": 10, "turn": 0}
            proof = {"date": daily["date"], "group": group, "elapsed_ms": 1200}
            wrong_day = {**proof, "date": (today() - timedelta(days=1)).isoformat()}
            self.assertEqual((await client.post("/api/v1/me/daily/complete", headers=headers, json=wrong_day)).status_code, 409)
            for response in await asyncio.gather(*(client.post("/api/v1/me/daily/complete", headers=headers, json=proof) for _ in range(2))):
                self.assertEqual(response.status_code, 200, response.text)
            collection = (await client.get("/api/v1/me/collection", headers=headers)).json()
            self.assertEqual(len(collection["items"]), 1)
            self.assertEqual(collection["balance"], 1)
            recorded = collection["items"][0]
            reopened = (await client.post("/api/v1/me/daily/start", headers=headers)).json()
            self.assertEqual(recorded["completed_at"], reopened["completed_at"])
            self.assertIsNotNone(datetime.fromisoformat(recorded["completed_at"]).utcoffset())

            async with app.state.db.acquire() as conn, conn.transaction():
                for offset in range(1, 61):
                    day = today() - timedelta(days=offset)
                    await conn.execute("INSERT INTO daily_editions VALUES ($1, $2)", day, level_id)
                    await conn.execute("""
                        INSERT INTO daily_attempts (user_id, day, completed_at, elapsed_ms, porcelain_name)
                        VALUES ($1, $2, now(), 1200, '青瓷')
                    """, UUID(player["id"]), day)
            claim = {"request_id": "test-request-one-0001", "recipient": "收件人",
                     "phone": "13800138000", "address": "某市某区某路 100 号"}
            endpoint = "/api/v1/me/collection/redemptions"
            first, retry = await asyncio.gather(*(client.post(endpoint, headers=headers, json=claim) for _ in range(2)))
            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(first.json()["id"], retry.json()["id"])
            second = await client.post(endpoint, headers=headers, json={**claim, "request_id": "test-request-two-0002"})
            self.assertEqual(second.status_code, 201, second.text)
            rejected = await client.post(endpoint, headers=headers, json={**claim, "request_id": "test-request-three-0003"})
            self.assertEqual(rejected.status_code, 409)
            rows = (await client.get("/admin/redemptions", headers=admin_headers)).json()
            self.assertEqual(rows["total"], 2)
            self.assertTrue(all(row["address"] == claim["address"] for row in rows["items"]))
            update = await client.patch("/admin/redemptions/" + first.json()["id"], headers=admin_headers,
                                        data={"status": "shipped", "carrier": "顺丰", "tracking_number": "SF12345"})
            self.assertEqual(update.status_code, 200, update.text)
            latest = (await client.get("/api/v1/me/collection", headers=headers)).json()
            self.assertEqual((len(latest["items"]), latest["balance"]), (61, 1))
            delivered = next(row for row in latest["claims"] if row["id"] == first.json()["id"])
            self.assertEqual(delivered["tracking_number"], "SF12345")
            self.assertNotIn("address", delivered)

    async def login(self, client):
        data = {"username": "admin", "password": PASSWORD}
        self.assertEqual((await client.post("/admin/login", data=data, headers={"X-CSRF-Token": "bad"})).status_code, 403)
        first = (await client.get("/admin/session")).json()["csrf"]
        response = await client.post("/admin/login", data=data, headers={"X-CSRF-Token": first})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf"]

    async def create_player(self, client, token, nickname):
        response = await client.post("/admin/users", data={"nickname": nickname}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 201, response.text)
        url = "/admin/users/" + response.json()["id"]
        return url, await self.issue(client, token, url)

    async def issue(self, client, csrf_token, url):
        issued = await client.post(f"{url}/tokens", headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(issued.headers["cache-control"], "no-store")
        return {"Authorization": "Bearer " + issued.json()["token"]}

    async def test_angular_regeneration_and_monthly_reward(self):
        app = create_app(self.settings)
        async with app.router.lifespan_context(app), self.client(app) as client:
            headers = {"X-CSRF-Token": await self.login(client)}
            source = io.BytesIO()
            Image.new("RGBA", (120, 150), "#387b69").save(source, format="PNG")
            raw = source.getvalue()

            async def add_level(number):
                response = await client.post("/admin/levels/generate", headers=headers,
                                             files={"image": ("test-vase.png", raw, "image/png")},
                                             data={"name": f"瓷器 {number}", "pieces": 7, "level_number": number,
                                                   "rotate_count": 3, "decoy_count": 2})
                self.assertEqual(response.status_code, 201, response.text)
                level_id = response.json()["id"]
                self.assertEqual((await client.post(f"/admin/levels/{level_id}/publish", headers=headers)).status_code, 200)
                return level_id

            async def guest(device):
                response = await client.post("/api/v1/me/guest", json={"device_id": device})
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()

            first, other, fastest = [await guest(f"monthly-guest-{i}") for i in range(3)]
            auth = {"Authorization": "Bearer " + fastest["token"]}
            level6 = await add_level(6)
            daily = (await client.post("/api/v1/me/daily/start", headers=auth)).json()
            level5 = await add_level(5)
            rows = (await client.get("/api/v1/levels?category=normal")).json()
            self.assertEqual([r["level_number"] for r in rows], [5, 6])
            old = (await client.get(f"/api/v1/levels/{level6}")).json()
            self.assertEqual((len(old["shards"]), old["rotate_count"], old["decoy_count"]), (9, 3, 2))
            self.assertTrue(any(len(seam["poly"]) > 4 for seam in old["seams"]))
            self.assertTrue(all(max(s["ids"]) < 7 for s in old["seams"]))
            self.assertTrue(all(min(shard["sprite"][2:]) > 0 for shard in old["shards"]))
            for seam in old["seams"]:
                for index in seam["ids"]:
                    poly = old["shards"][index]["poly"]
                    vertices = list(zip(poly[::2], poly[1::2]))
                    self.assertTrue(all(p in vertices for p in zip(seam["poly"][::2], seam["poly"][1::2])))
            original_key = await app.state.db.fetchval("SELECT original_key FROM levels WHERE id = $1", UUID(level6))
            self.assertEqual((self.settings.upload_dir / original_key).read_bytes(), raw)

            edited = await client.patch(f"/admin/levels/{level6}", headers=headers,
                                        data={"name": "重新生成", "category": "normal", "pieces": 12,
                                              "rotate_count": 5, "decoy_count": 4, "atlas_version": 1})
            self.assertEqual(edited.status_code, 200, edited.text)
            new = (await client.get(f"/api/v1/levels/{level6}")).json()
            self.assertEqual((new["atlas_version"], len(new["shards"]), new["rotate_count"]), (2, 16, 5))
            self.assertEqual((await client.get(f"/api/v1/levels/{level6}/atlas.png?v=1")).status_code, 200)
            reopened = (await client.post("/api/v1/me/daily/start", headers=auth)).json()
            self.assertEqual(reopened["level"]["atlas_version"], 1)
            group = {"ids": list(range(7)), "x": 10, "y": 20, "turn": 0}
            self.assertEqual((await client.post("/api/v1/me/daily/complete", headers=auth,
                               json={"date": daily["date"], "group": group, "elapsed_ms": 800})).status_code, 200)

            first_url = f"/api/v1/me/campaign/levels/{level5}/start"
            next_url = f"/api/v1/me/campaign/levels/{level6}/start"
            self.assertEqual((await client.post(next_url, headers=auth)).status_code, 403)
            started = await client.post(first_url, headers=auth)
            self.assertEqual(started.status_code, 200, started.text)
            finish = "/api/v1/me/campaign/attempts/" + started.json()["id"] + "/complete"
            proof = {"group": group, "elapsed_ms": 1200}
            self.assertEqual((await client.post(finish, headers=auth, json=proof)).status_code, 200)
            self.assertEqual((await client.post(next_url, headers=auth)).status_code, 200)
            live = (await client.get("/api/v1/ranking")).json()
            self.assertEqual(live["items"][0]["level_number"], 5)
            self.assertIsNone(live["winner"])

            prior = current_month() - timedelta(days=1)
            old_time = datetime(prior.year, prior.month, 10, 10, tzinfo=UTC)
            for account, level_id, number, elapsed in [(first, level5, 5, 1000), (other, level6, 6, 6000),
                                                       (fastest, level6, 6, 5000)]:
                await app.state.db.execute("""
                    INSERT INTO campaign_attempts (id, user_id, level_id, category, level_number, atlas_version,
                        piece_count, completed_at, elapsed_ms)
                    VALUES ($1, $2, $3, 'normal', $4, 1, 7, $5, $6)
                """, uuid4(), UUID(account["id"]), UUID(level_id), number, old_time, elapsed)
            month = prior.strftime("%Y-%m")
            ranked = await client.get("/api/v1/ranking", params={"month": month})
            self.assertEqual(ranked.status_code, 200, ranked.text)
            self.assertEqual([(r["level_number"], r["elapsed_ms"]) for r in ranked.json()["items"]],
                             [(6, 5000), (6, 6000), (5, 1000)])
            self.assertFalse(ranked.json()["winner"]["claimed"])
            collection = (await client.get("/api/v1/me/collection", headers=auth)).json()
            self.assertEqual(len(collection["awards"]), 1)
            self.assertEqual(collection["balance"], 1)
            claim = {"request_id": "test-monthly-claim-1", "reward_month": month, "recipient": "测试",
                     "phone": "13800138000", "address": "测试省测试区测试路 123 号"}
            url = "/api/v1/me/collection/redemptions"
            rejected = await client.post(url, json=claim, headers={"Authorization": "Bearer " + other["token"]})
            self.assertEqual(rejected.status_code, 409)
            first_claim = await client.post(url, json=claim, headers=auth)
            self.assertEqual(first_claim.status_code, 201, first_claim.text)
            self.assertEqual((await client.post(url, json=claim, headers=auth)).json()["id"], first_claim.json()["id"])
            self.assertEqual((await client.post(url, json={**claim, "request_id": "test-monthly-claim-2"},
                                               headers=auth)).status_code, 409)
            collection = (await client.get("/api/v1/me/collection", headers=auth)).json()
            self.assertEqual(collection["balance"], 1)
            self.assertEqual(collection["awards"], [])

    async def test_generated_artwork_and_ranking(self):
        app = create_app(self.settings)
        async with app.router.lifespan_context(app), self.client(app) as client:
            headers = {"X-CSRF-Token": await self.login(client)}
            image = (ROOT / "examples/three-shards.png").read_bytes()
            created = await client.post("/admin/levels/generate", headers=headers,
                data={"name":"测试瓷器","pieces":7,"rotate":"true"},
                files={"image":("ceramic.png",image,"image/png")})
            self.assertEqual(created.status_code, 201, created.text)
            self.assertLess(created.json()["bytes"], 200000)
            key = created.json()["id"]
            url = "/admin/levels/" + key
            self.assertEqual((await client.post(url + "/publish", headers=headers)).status_code, 200)
            detail = (await client.get("/api/v1/levels/" + key)).json()
            self.assertEqual((len(detail["shards"]), detail["rotations"], detail["points"]), (7,3,[]))
            _, auth = await self.create_player(client, headers["X-CSRF-Token"], "榜单玩家")
            result = await client.put("/api/v1/me/progress/" + key, headers=auth,
                json={"revision":0,"groups":[{"ids":list(range(7)),"x":-10,"y":110,"turn":0}],
                      "paint":[],"assembly_only":True})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["stage"], "complete")
            ranking = (await client.get("/admin/ranking")).json()
            self.assertEqual((ranking[0]["nickname"],ranking[0]["completions"]), ("榜单玩家",1))
            self.assertEqual((await client.delete(url,headers=headers)).status_code,200)
            self.assertEqual((await client.get("/api/v1/levels")).json(),[])
            self.assertEqual((await client.get("/api/v1/me/progress",headers=auth)).json()["progress"][0]["stage"],"complete")
            draft = await client.post("/admin/levels/generate", headers=headers,
                data={"name":"待删除","pieces":3}, files={"image":("ceramic.png",image,"image/png")})
            draft_id = draft.json()["id"]
            self.assertEqual((await client.delete("/admin/levels/" + draft_id,headers=headers)).status_code,200)
            self.assertFalse((self.settings.upload_dir / (draft_id + ".png")).exists())

    async def test_upload_publication_and_concurrent_progress(self):
        app = create_app(self.settings)
        async with app.router.lifespan_context(app), self.client(app) as client:
            self.assertEqual((await client.get("/health")).json(), {"status": "ok"})
            self.assertEqual((await client.get("/admin/levels")).status_code, 401)
            self.assertEqual((await client.post("/admin/users", data={"csrf": "bad", "nickname": "a"})).status_code, 401)
            self.assertEqual((await client.get("/api/v1/me/progress")).status_code, 401)
            token = await self.login(client)
            png = (ROOT / "examples/three-shards.png").read_bytes()
            config = json.loads((ROOT / "examples/three-shards.json").read_text(encoding="utf-8"))

            async def upload(image, definition):
                return await client.post("/admin/levels", headers={"X-CSRF-Token": token}, files={
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
                self.assertEqual(response.status_code, 201, response.text)
                admin_url = "/admin/levels/" + response.json()["id"]
                level_id = admin_url.rsplit("/", 1)[1]
                level_ids.append(level_id)
                api_url = f"/api/v1/levels/{level_id}"
                self.assertEqual((await client.get(api_url)).status_code, 404)
                self.assertEqual((await client.get(f"{api_url}/atlas.png")).status_code, 404)
                self.assertEqual((await client.get(f"{admin_url}/atlas.png")).content, png)
                response = await client.post(f"{admin_url}/publish", headers={"X-CSRF-Token": token})
                self.assertEqual(response.status_code, 200, response.text)
                detail = (await client.get(api_url)).json()
                self.assertEqual((detail["id"], detail["sort_order"]), (level_id, number))
                self.assertEqual((await client.get(detail["image"])).content, png)
                self.assertEqual(
                    (await client.post(f"{admin_url}/publish", headers={"X-CSRF-Token": token})).status_code, 200,
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
            self.assertEqual(detail.json()["current_level"], "关卡 2")
            rotations = await asyncio.gather(*(self.issue(client, token, player_url) for _ in range(2)))
            self.assertEqual((await client.get("/api/v1/me/progress", headers=auth)).status_code, 401)
            statuses = [(await client.get("/api/v1/me/progress", headers=headers)).status_code
                        for headers in rotations]
            self.assertEqual(sorted(statuses), [200, 401])
            rotated = rotations[statuses.index(200)]
            await client.delete(f"{player_url}/tokens", headers={"X-CSRF-Token": token})
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
            response = await client.post("/admin/users", data=fields, headers={"X-CSRF-Token": token})
            self.assertEqual(response.status_code, 201, response.text)
            user_url = "/admin/users/" + response.json()["id"]
            auth = await self.issue(client, token, user_url)
            profile = await client.get("/api/v1/me", headers=auth)
            self.assertEqual(profile.headers["cache-control"], "no-store")
            self.assertEqual(profile.json(), {
                "id": user_url.rsplit("/", 1)[1], "nickname": fields["nickname"],
                "source": "wechat", "openid": fields["openid"], "avatar_url": fields["avatar_url"],
                "last_login_at": None, "login_count": 0,
            })
            edit = {**fields, "source": "douyin", "openid": "douyin-test-id"}
            self.assertEqual((await client.patch(user_url, data=edit, headers={"X-CSRF-Token": token})).status_code, 200)
            invalid = {**edit, "source": "other"}
            self.assertEqual((await client.patch(user_url, data=invalid, headers={"X-CSRF-Token": token})).status_code, 422)
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

    async def test_schema_enforces_required_data_and_foreign_keys(self):
        app = create_app(self.settings)
        async with app.router.lifespan_context(app):
            db = app.state.db
            await db.execute("INSERT INTO users (id) VALUES ($1)", uuid4())
            for table in ("admins", "levels", "user_tokens"):
                with self.assertRaises(asyncpg.NotNullViolationError):
                    await db.execute(f"INSERT INTO {table} (id) VALUES ($1)", uuid4())
            with self.assertRaises(asyncpg.ForeignKeyViolationError):
                await db.execute("INSERT INTO user_level_progress (user_id, level_id, snapshot) VALUES ($1,$2,'{}')",
                                 uuid4(), uuid4())
