import asyncio, json, time
from datetime import datetime, timedelta, timezone
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from .db import setting
from .ai import classify
from .routing import automatic_selection, classification_confidence
from .providers import AIUnavailable

TERMINAL = {"published", "skipped", "uncertain"}


def buttons(p):
    return [
        [
            {"text": "✅ Опубликовать", "callback_data": f"approve:{p['id']}"},
            {"text": "✏️ Категория", "callback_data": f"choose:{p['id']}"},
        ],
        [{"text": "❌ Пропустить", "callback_data": f"skip:{p['id']}"}],
    ]


def route_decision(
    mode, confidence, auto_threshold, review_threshold, mixed, available
):
    if mixed or not available:
        return "review"
    if mode == "AUTO" and confidence >= auto_threshold:
        return "queued"
    return "review" if confidence >= review_threshold else "low_confidence"


async def ingest(db, m):
    source = m["chat"]["id"]
    s = await db.fetchrow("SELECT * FROM sources WHERE id=$1 AND enabled", source)
    if not s:
        return
    mid = m["message_id"]
    now = datetime.now(timezone.utc)
    media = bool(m.get("photo") or m.get("video") or m.get("document"))
    standalone_complete = (
        media and not m.get("media_group_id") and bool(m.get("caption"))
    )
    async with db.acquire() as c, c.transaction():
        await c.execute("SELECT pg_advisory_xact_lock($1)", source)
        old = await c.fetchval(
            "SELECT product_id FROM source_messages WHERE source=$1 AND message=$2",
            source,
            mid,
        )
        if old:
            p = await c.fetchrow("SELECT * FROM products WHERE id=$1 FOR UPDATE", old)
            previous = next((x for x in p["messages"] if x["message_id"] == mid), None)
            if previous == m:
                return
            messages = [m if x["message_id"] == mid else x for x in p["messages"]]
            status = (
                p["status"]
                if p["status"] == "collecting"
                else (
                    "uncertain"
                    if p["status"] in ("sending", "finishing", "published", "uncertain")
                    else "review"
                )
            )
            await c.execute(
                "UPDATE products SET messages=$2,status=$3,error=$4,closed=closed OR $5,ready_at=now()+interval '12 seconds',updated_at=now() WHERE id=$1",
                old,
                messages,
                status,
                None
                if status == "collecting"
                else "Позднее изменение товара: требуется сверка",
                standalone_complete,
            )
            return
        p = None
        group = str(m.get("media_group_id") or "")
        if group:
            p = await c.fetchrow(
                "SELECT * FROM products WHERE source_channel_id=$1 AND EXISTS(SELECT 1 FROM jsonb_array_elements(messages) x WHERE x->>'media_group_id'=$2) ORDER BY id DESC LIMIT 1 FOR UPDATE",
                source,
                group,
            )
        if not p and s["style"] == "autopost":
            p = await c.fetchrow(
                "SELECT * FROM products WHERE source_channel_id=$1 AND status IN ('collecting','review','low_confidence') AND NOT closed ORDER BY id DESC LIMIT 1 FOR UPDATE",
                source,
            )
        if not p and not media:
            return
        if p:
            messages = sorted(p["messages"] + [m], key=lambda x: x["message_id"])
            status = p["status"]
            if status != "collecting":
                status = (
                    "uncertain"
                    if status in ("sending", "finishing", "published", "uncertain")
                    else "review"
                )
            await c.execute(
                "UPDATE products SET messages=$2,closed=closed OR $3,status=$4,ready_at=now()+interval '12 seconds',updated_at=now(),error=CASE WHEN $4='collecting' THEN NULL ELSE 'Поздняя часть товара: требуется сверка' END WHERE id=$1",
                p["id"],
                messages,
                not media or standalone_complete,
                status,
            )
            pid = p["id"]
        else:
            pid = await c.fetchval(
                "INSERT INTO products(source_channel_id,source_message_id,media_group_id,messages,closed,ready_at) VALUES($1,$2,$3,$4,$5,now()+interval '12 seconds') RETURNING id",
                source,
                mid,
                group or None,
                [m],
                standalone_complete
                or (s["style"] != "autopost" and bool(m.get("caption"))),
            )
        await c.execute(
            "INSERT INTO source_messages VALUES($1,$2,$3)", source, mid, pid
        )


async def publish(db, tg, p):
    from .deliveries import publish as publish_channels
    await publish_channels(db,tg,p)


async def finish_publication(db, tg, p):
    if p and await db.fetchval('SELECT 1 FROM product_deliveries WHERE product_id=$1',p['id']):
        from .deliveries import drive,finish_one
        d=await db.fetchrow("SELECT * FROM product_deliveries WHERE product_id=$1 AND status<>'published' ORDER BY id LIMIT 1",p['id'])
        if d and d['status']=='finishing':
            await finish_one(db,tg,d)
            await drive(db,tg,p['id'])
        return
    if not p or p["status"] != "finishing":
        return
    ids = p["destination_message_ids"] or []
    messages = sorted(p["messages"], key=lambda x: x["message_id"])
    if len(ids) != len(messages):
        await db.execute(
            "UPDATE products SET status='uncertain',error='Число отправленных сообщений не совпадает: ручная сверка' WHERE id=$1",
            p["id"],
        )
        return
    try:
        for original, did in zip(messages, ids):
            rows = [
                [b for b in row if b.get("url")]
                for row in original.get("reply_markup", {}).get("inline_keyboard", [])
            ]
            rows = [row for row in rows if row]
            if rows:
                try:
                    await tg.call(
                        "edit_message_reply_markup",
                        chat_id=p["destination_channel_id"],
                        message_id=did,
                        reply_markup={"inline_keyboard": rows},
                    )
                except TelegramBadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        raise
        await db.execute(
            "UPDATE products SET status='published',error=NULL,updated_at=now() WHERE id=$1 AND status='finishing'",
            p["id"],
        )
    except Exception as e:
        delay = float(e.retry_after) if isinstance(e, TelegramRetryAfter) else 60.0
        await db.execute(
            "UPDATE products SET error=$2,ready_at=now()+$3*interval '1 second',updated_at=now() WHERE id=$1 AND status='finishing'",
            p["id"],
            "Товар скопирован; восстанавливаются URL-кнопки: " + str(e)[:500],
            delay,
        )


async def recover_stale(db):
    from .deliveries import recover
    await recover(db)
    await db.execute(
        "UPDATE products SET status='uncertain',error='Отправка прервана или потеряна связь с БД: требуется сверка' WHERE status='sending' AND updated_at<now()-interval '10 minutes'"
    )
    await db.execute(
        "UPDATE products SET status='review',error='Анализ прерван или потеряна связь с БД: можно повторить' WHERE status='analyzing' AND updated_at<now()-interval '10 minutes'"
    )


async def worker(db, tg):
    from .deliveries import recover
    await recover(db,startup=True)
    await db.execute(
        "UPDATE products SET status='review',error='Прерван анализ: можно повторить' WHERE status='analyzing'"
    )
    await db.execute(
        "UPDATE products SET status='uncertain',error='Перезапуск во время отправки: требуется сверка канала' WHERE status='sending'"
    )
    while True:
        try:
            await recover_stale(db)
            await db.execute(
                "INSERT INTO health VALUES('worker',now()) ON CONFLICT(component) DO UPDATE SET at=now()"
            )
            if not await setting(db, "paused", False):
                p = (
                    await db.fetchrow(
                        "SELECT * FROM products WHERE status='collecting' AND ready_at<now() ORDER BY id LIMIT 1"
                    )
                    if await setting(db, "ai_retry_until", 0) < time.time()
                    else None
                )
                if p:
                    if (
                        not p["closed"]
                        and await db.fetchval(
                            "SELECT style FROM sources WHERE id=$1",
                            p["source_channel_id"],
                        )
                        == "autopost"
                    ):
                        if (
                            datetime.now(timezone.utc) - p["updated_at"]
                        ).total_seconds() < 90:
                            await asyncio.sleep(1)
                            continue
                        await db.execute(
                            "UPDATE products SET status='review',error='Нет завершающей подписи autopost; граница товара не подтверждена' WHERE id=$1",
                            p["id"],
                        )
                        continue
                    claimed = await db.fetchval(
                        "UPDATE products SET status='analyzing',attempts=attempts+1 WHERE id=$1 AND status='collecting' AND messages=$2 RETURNING id",
                        p["id"],
                        p["messages"],
                    )
                    if not claimed:
                        continue
                    try:
                        result, usage, latency = await classify(db, tg, p)
                        threshold = await setting(db, "auto_threshold", 0.85)
                        routing = await automatic_selection(db, result, threshold)
                        selected = routing['selected']
                        confidence = classification_confidence(result)
                        available = routing['primary_available']
                        usage['routing'] = routing
                        status = route_decision(
                            await setting(db, "mode", "REVIEW"),
                            confidence,
                            threshold,
                            await setting(db, "review_threshold", 0.60),
                            result["mixed_products"],
                            available,
                        )
                        ip = await setting(db, "input_price")
                        op = await setting(db, "output_price")
                        cost = (
                            (
                                usage.get("promptTokenCount", 0) * ip
                                + (
                                    usage.get("candidatesTokenCount", 0)
                                    + usage.get("thoughtsTokenCount", 0)
                                )
                                * op
                            )
                            / 1e6
                            if ip is not None and op is not None
                            else None
                        )
                        if 'estimated_cost_usd' in usage:
                            cost=usage['estimated_cost_usd']
                        await db.execute(
                            "UPDATE products SET ai_result=$2,confidence=$3,ai_category=$4,final_category=$4,usage=$5,latency_ms=$6,status=$7,estimated_cost_usd=$9,final_categories=$10,error=NULL,updated_at=now() WHERE id=$1 AND status='analyzing' AND messages=$8",
                            p["id"],
                            result,
                            confidence,
                            result["category"],
                            usage,
                            latency,
                            status,
                            p["messages"],
                            cost,
                            selected,
                        )
                    except AIUnavailable as e:
                        await db.execute("UPDATE products SET status='collecting',ready_at=now(),error=$2,updated_at=now() WHERE id=$1 AND status='analyzing' AND messages=$3",
                                         p['id'],str(e),p['messages'])
                    except Exception as e:
                        await db.execute(
                            "UPDATE products SET status='review',error=$2 WHERE id=$1 AND status='analyzing'",
                            p["id"],
                            str(e)[:700],
                        )
                q = await db.fetchrow(
                    "SELECT * FROM products WHERE status='queued' AND ready_at<=now() ORDER BY id LIMIT 1"
                )
                if q:
                    await publish(db, tg, q)
                pending = await db.fetchrow(
                    "SELECT * FROM products WHERE status='finishing' AND ready_at<=now() ORDER BY id LIMIT 1"
                )
                if pending:
                    await finish_publication(db, tg, pending)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("worker:", type(e).__name__, str(e)[:250])
            await asyncio.sleep(3)
