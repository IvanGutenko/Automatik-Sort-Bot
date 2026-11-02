import asyncio, os, json, time
from .db import connect, setting, put
from .telegram import Telegram
from .pipeline import ingest, worker
from . import admin


async def ingress(db, tg):
    while True:
        try:
            offset = await setting(db, "offset", 0)
            batch = await tg.call(
                "get_updates",
                offset=offset,
                timeout=25,
                allowed_updates=[
                    "message",
                    "callback_query",
                    "channel_post",
                    "edited_channel_post",
                    "my_chat_member",
                ],
            )
            async with db.acquire() as c, c.transaction():
                for u in batch:
                    await c.execute(
                        "INSERT INTO updates(id,payload) VALUES($1,$2) ON CONFLICT DO NOTHING",
                        u["update_id"],
                        u,
                    )
                if batch:
                    await put(c, "offset", batch[-1]["update_id"] + 1)
                await c.execute(
                    "INSERT INTO health VALUES('ingress',now()) ON CONFLICT(component) DO UPDATE SET at=now()"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("ingress:", type(e).__name__, str(e)[:200])
            await asyncio.sleep(4)


async def dispatch(db, tg):
    while True:
        rows = await db.fetch(
            "SELECT * FROM updates WHERE NOT done ORDER BY id LIMIT 30"
        )
        for row in rows:
            u = row["payload"]
            try:
                if "channel_post" in u or "edited_channel_post" in u:
                    await ingest(db, u.get("channel_post") or u["edited_channel_post"])
                elif "my_chat_member" in u:
                    event = u["my_chat_member"]
                    channel = event["chat"]
                    member = event["new_chat_member"]
                    if channel.get("type") == "channel":
                        await db.execute(
                            "INSERT INTO available_channels(id,title,status,can_post) VALUES($1,$2,$3,$4) ON CONFLICT(id) DO UPDATE SET title=$2,status=$3,can_post=$4,updated_at=now()",
                            channel["id"],
                            channel["title"],
                            member["status"],
                            member.get("can_post_messages", False),
                        )
                elif "message" in u or "callback_query" in u:
                    await admin.handle(db, tg, u)
                await db.execute("UPDATE updates SET done=true WHERE id=$1", row["id"])
            except (ValueError, KeyError) as e:
                m = u.get("callback_query", {}).get("message") or u.get("message", {})
                if m.get("chat", {}).get("type") == "private":
                    await tg.say(m["chat"]["id"], str(e)[:600])
                await db.execute("UPDATE updates SET done=true WHERE id=$1", row["id"])
            except Exception as e:
                print("dispatch", row["id"], type(e).__name__, str(e)[:200])
                # Admin requests may be retried by the user, channel ingress must never be lost.
                if "channel_post" not in u and "edited_channel_post" not in u:
                    m = u.get("callback_query", {}).get("message") or u.get(
                        "message", {}
                    )
                    if m.get("chat", {}).get("type") == "private":
                        try:
                            await tg.say(
                                m["chat"]["id"],
                                "Операция не выполнена: " + str(e)[:500],
                            )
                        except Exception:
                            pass
                    await db.execute(
                        "UPDATE updates SET done=true WHERE id=$1", row["id"]
                    )
                await asyncio.sleep(3)
        await db.execute(
            "INSERT INTO health VALUES('dispatch',now()) ON CONFLICT(component) DO UPDATE SET at=now()"
        )
        await asyncio.sleep(0.5)


async def notification_tick(db, tg):
    allowed=await admin.admins(db)
    receipt=await db.fetchrow("SELECT p.*,c.title FROM products p LEFT JOIN categories c ON c.code=p.final_category WHERE p.status='published' AND p.decision_actor IS NOT NULL AND p.publish_notice_id IS NULL ORDER BY p.updated_at LIMIT 1")
    if receipt:
        actor=receipt['decision_actor']
        if actor in allowed:
            ids=receipt['destination_message_ids'] or []
            link=(f"https://t.me/c/{str(receipt['destination_channel_id']).removeprefix('-100')}/{ids[0]}" if ids else '')
            from .deliveries import rows
            ds=await rows(db,receipt['id'])
            if ds:
                details='\n'.join(f"→ {d['title'] or d['category_code']} · {len(d['message_ids'] or [])} сообщений\nhttps://t.me/c/{str(d['destination']).removeprefix('-100')}/{d['message_ids'][0]}" if d['message_ids'] else f"→ {d['title'] or d['category_code']} · сверен вручную" for d in ds)
                notice_text=f"✅ #{receipt['id']} опубликован во все каналы:\n"+details
            else:notice_text=f"✅ #{receipt['id']} опубликован → {receipt['title'] or 'канал'}\nСообщений: {len(ids)}\n{link}"
            msg=await tg.say(actor,notice_text,[[admin.button('Следующий товар','reviews:0')]])
            notice=msg['message_id'] if msg else -1
        else:notice=-1
        await db.execute('UPDATE products SET publish_notice_id=$2 WHERE id=$1',receipt['id'],notice)
    if not await setting(db,'paused',True) and await setting(db,'notify_enabled',True):
        actor=await setting(db,'review_admin_id',allowed[0])
        if actor not in allowed:return
        since=await setting(db,'review_notify_from',0)
        p=await db.fetchrow("SELECT p.id FROM products p JOIN sources s ON s.id=p.source_channel_id WHERE s.enabled AND (p.status IN ('review','low_confidence','uncertain') OR (p.status='collecting' AND p.closed AND p.ready_at<=now() AND $2)) AND p.review_message_id IS NULL AND p.created_at>=to_timestamp($1) ORDER BY p.id LIMIT 1",float(since),await setting(db,'ai_retry_until',0)>time.time())
        if p:
            msg=await admin.item(db,tg,actor,p['id'])
            await db.execute('UPDATE products SET review_message_id=$2,review_chat_id=$3 WHERE id=$1',p['id'],msg['message_id'] if msg else -1,actor)


async def notifications(db, tg):
    while True:
        try:
            await notification_tick(db,tg)
            await asyncio.sleep(12)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print('notifications:',type(e).__name__,str(e)[:150])
            await asyncio.sleep(30)


async def main():
    db = await connect()
    tg = Telegram()
    me = await tg.call("get_me")
    print("Sorting bot started:", me["username"])
    await tg.call(
        "set_my_commands",
        commands=[
            {"command": "admin", "description": "Управление сортировкой E-MODA"},
            {"command": "rules", "description": "Все заданные правила AI"},
            {"command": "cancel", "description": "Отменить ввод"},
        ],
    )
    await asyncio.gather(
        ingress(db, tg), dispatch(db, tg), worker(db, tg), notifications(db, tg)
    )


if __name__ == "__main__":
    asyncio.run(main())
