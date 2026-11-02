import os, json
from pathlib import Path
import asyncpg


async def connect():
    async def init(c):
        for name in ("json", "jsonb"):
            await c.set_type_codec(
                name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )

    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=1, max_size=5, init=init
    )
    async with pool.acquire() as c:
        await c.execute(Path("/app/app/schema.sql").read_text())
    return pool


async def setting(db, key, default=None):
    r = await db.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return default if r is None else r


async def put(db, key, value):
    if value is None:
        await db.execute("DELETE FROM settings WHERE key=$1", key)
        return
    await db.execute(
        "INSERT INTO settings VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        key,
        value,
    )
