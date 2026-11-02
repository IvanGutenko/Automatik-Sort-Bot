"""A shared, durable request gate for the service and its evaluation runner."""

import asyncio
import time
from weakref import WeakKeyDictionary
from .db import setting


_loops=WeakKeyDictionary()


async def wait_turn(db, model):
    # Database grants are shared across processes; a delayed DB response must
    # not compress parallel requests into a burst within this worker.
    gates=_loops.setdefault(asyncio.get_running_loop(),{})
    gate=gates.setdefault(model,{'lock':asyncio.Lock(),'last':0})
    async with gate['lock']:
        rpm=await setting(db,'ai_rpm',12)
        await asyncio.sleep(max(0,gate['last']+60.0/rpm-time.monotonic()))
        await _database_turn(db,model,rpm)
        gate['last']=time.monotonic()


async def _database_turn(db, model, rpm):
    interval = 60.0 / rpm
    await db.execute(
        "INSERT INTO ai_limits(model) VALUES($1) ON CONFLICT DO NOTHING", model
    )
    while True:
        granted = await db.fetchval(
            "UPDATE ai_limits SET next_at=clock_timestamp()+$2*interval '1 second' WHERE model=$1 AND next_at<=clock_timestamp() RETURNING true",
            model,
            interval,
        )
        if granted:
            return
        delay = await db.fetchval(
            "SELECT greatest(0,extract(epoch FROM next_at-clock_timestamp())) FROM ai_limits WHERE model=$1",
            model,
        )
        await asyncio.sleep(max(0.01, float(delay)))
