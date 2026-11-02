import asyncio, os, asyncpg


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    n = await c.fetchval(
        "SELECT count(*) FROM health WHERE component IN ('ingress','dispatch','worker') AND at>now()-interval '180 seconds'"
    )
    await c.close()
    if n != 3:
        raise SystemExit(1)


asyncio.run(main())
