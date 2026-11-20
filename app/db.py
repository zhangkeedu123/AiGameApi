import json

import asyncpg


async def connect_pool(database_url, max_size):
    async def setup(connection):
        await connection.set_type_codec(
            "jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads
        )

    return await asyncpg.create_pool(
        database_url, min_size=1, max_size=max_size, init=setup, command_timeout=15
    )
