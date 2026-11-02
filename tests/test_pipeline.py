import os, asyncio
import pytest, pytest_asyncio
from app.db import connect, put
from app.pipeline import ingest, publish, route_decision
from app.admin import handle


@pytest_asyncio.fixture
async def db():
    assert os.environ["DATABASE_URL"].endswith("/sorting_test"), (
        "isolated test database required"
    )
    db = await connect()
    await db.execute(
        "TRUNCATE ai_provider_state,ai_requests,ai_limits,corrections,source_messages,products,sources,categories,updates,settings,admin_state RESTART IDENTITY CASCADE"
    )
    await db.execute("INSERT INTO sources VALUES(-1001,'QA',true,'autopost')")
    yield db
    await db.close()


def msg(mid, group="a", caption=None, text=None):
    m = {"message_id": mid, "chat": {"id": -1001, "type": "channel"}}
    if group:
        m.update(media_group_id=group, photo=[{"file_id": "photo_" + str(mid)}])
    if caption:
        m["caption"] = caption
    if text:
        m["text"] = text
    return m


@pytest.mark.asyncio
async def test_whole_product_chunks_caption_and_duplicate(db):
    for m in [
        msg(1),
        msg(2),
        msg(3, "b"),
        msg(4, None, text="Код E-MODA: A123\nЦена: 20\nРазмеры: S M"),
    ]:
        await ingest(db, m)
    await ingest(db, msg(2))
    assert await db.fetchval("SELECT count(*) FROM products") == 1
    p = await db.fetchrow("SELECT * FROM products")
    assert p["closed"]
    assert len(p["messages"]) == 4
    await ingest(db, msg(5, "c"))
    assert await db.fetchval("SELECT count(*) FROM products") == 2


@pytest.mark.asyncio
async def test_generic_album_separates_neighbor_products(db):
    await db.execute("UPDATE sources SET style='album'")
    await ingest(db, msg(1, "a", "Dress"))
    await ingest(db, msg(2, "a"))
    await ingest(db, msg(3, "b", "Pants"))
    assert await db.fetchval("SELECT count(*) FROM products") == 2


@pytest.mark.asyncio
async def test_late_photo_after_publish_never_requeues(db):
    await ingest(db, msg(1))
    await db.execute("UPDATE products SET status='published'")
    await ingest(db, msg(2))
    p = await db.fetchrow("SELECT * FROM products")
    assert p["status"] == "uncertain"
    assert len(p["messages"]) == 2


@pytest.mark.asyncio
async def test_unconfigured_channel_is_ignored(db):
    m = msg(1)
    m["chat"]["id"] = -999
    await ingest(db, m)
    assert await db.fetchval("SELECT count(*) FROM products") == 0


@pytest.mark.parametrize(
    "mode,confidence,mixed,available,expected",
    [
        ("AUTO", 0.85, False, True, "queued"),
        ("AUTO", 0.849, False, True, "review"),
        ("AUTO", 0.59, False, True, "low_confidence"),
        ("REVIEW", 0.99, False, True, "review"),
        ("AUTO", 0.99, True, True, "review"),
        ("AUTO", 0.99, False, False, "review"),
    ],
)
def test_route(mode, confidence, mixed, available, expected):
    assert route_decision(mode, confidence, 0.85, 0.6, mixed, available) == expected


class Gateway:
    def __init__(self, error=False, partial=False):
        self.calls = []
        self.error = error
        self.partial = partial

    async def call(self, method, **kw):
        self.calls.append((method, kw))
        if method != "copy_messages":
            return True
        if self.error:
            raise TimeoutError("unknown network outcome")
        return [
            {"message_id": n + 100}
            for n in kw["message_ids"][: 1 if self.partial else 100]
        ]

    async def say(self, *a, **kw):
        self.calls.append(("say", a))


async def queued(db):
    await ingest(db, msg(1))
    await ingest(db, msg(2))
    await db.execute(
        "INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.dress','Dress','Dress','women',-2001,true)"
    )
    await db.execute("UPDATE products SET status='queued',final_category='women.dress'")
    return await db.fetchrow("SELECT * FROM products")


@pytest.mark.asyncio
async def test_publish_repeated_attempt_does_not_duplicate(db):
    p = await queued(db)
    tg = Gateway()
    await publish(db, tg, p)
    await publish(db, tg, p)
    assert len(tg.calls) == 1
    assert await db.fetchval("SELECT status FROM products") == "published"


@pytest.mark.asyncio
async def test_timeout_is_uncertain_and_never_blind_retry(db):
    p = await queued(db)
    tg = Gateway(error=True)
    await publish(db, tg, p)
    await publish(db, tg, p)
    assert len(tg.calls) == 1
    assert await db.fetchval("SELECT status FROM products") == "uncertain"


@pytest.mark.asyncio
async def test_partial_copy_is_uncertain(db):
    p = await queued(db)
    await publish(db, Gateway(partial=True), p)
    assert await db.fetchval("SELECT status FROM products") == "uncertain"


@pytest.mark.asyncio
async def test_route_back_into_source_is_forbidden(db):
    p = await queued(db)
    await db.execute("UPDATE categories SET destination=-1001")
    tg = Gateway()
    await publish(db, tg, p)
    assert not tg.calls
    assert await db.fetchval("SELECT status FROM products") == "review"


@pytest.mark.asyncio
async def test_unauthorized_admin_cannot_change_mode(db):
    await put(db, "admins", [111])
    tg = Gateway()
    await handle(
        db,
        tg,
        {
            "callback_query": {
                "id": "x",
                "from": {"id": 222},
                "data": "mode:AUTO",
                "message": {"chat": {"id": 222, "type": "private"}},
            }
        },
    )
    assert await db.fetchval("SELECT value FROM settings WHERE key='mode'") is None


@pytest.mark.asyncio
async def test_concurrent_approve_once_audited(db):
    p = await queued(db)
    await db.execute("UPDATE products SET status='review'")
    await put(db, "admins", [111])
    tg = Gateway()
    u = {
        "callback_query": {
            "id": "x",
            "from": {"id": 111},
            "data": f"approve:{p['id']}",
            "message": {"chat": {"id": 111, "type": "private"}},
        }
    }
    # Approval commits before a repeated click is accepted.
    await handle(db, tg, u)
    with pytest.raises(ValueError):
        await handle(db, tg, u)
    assert await db.fetchval("SELECT count(*) FROM corrections") == 1


@pytest.mark.asyncio
async def test_recovery_does_not_retry_uncertain_send(db):
    from app.pipeline import worker

    await queued(db)
    await db.execute("UPDATE products SET status='sending'")
    await put(db, "paused", True)
    tg = Gateway()
    task = asyncio.create_task(worker(db, tg))
    for _ in range(20):
        if await db.fetchval("SELECT status FROM products") == "uncertain":
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db.fetchval("SELECT status FROM products") == "uncertain"
    assert not tg.calls


@pytest.mark.asyncio
async def test_concurrent_admin_clicks_queue_once(db):
    p = await queued(db)
    await db.execute("UPDATE products SET status='review'")
    await put(db, "admins", [111])
    tg = Gateway()
    u = {
        "callback_query": {
            "id": "x",
            "from": {"id": 111},
            "data": f"approve:{p['id']}",
            "message": {"chat": {"id": 111, "type": "private"}},
        }
    }
    await asyncio.gather(handle(db, tg, u), handle(db, tg, u), return_exceptions=True)
    assert await db.fetchval("SELECT count(*) FROM corrections") == 1
    assert await db.fetchval("SELECT status FROM products") == "queued"


@pytest.mark.asyncio
async def test_telegram_boundary_keeps_bot_api_field_names():
    from aiogram.types import Update
    from app.telegram import Telegram

    class BotReply:
        async def get_updates(self, **kw):
            return [
                Update.model_validate(
                    {
                        "update_id": 10,
                        "message": {
                            "message_id": 1,
                            "date": 1792216800,
                            "chat": {"id": 111, "type": "private"},
                            "from": {"id": 111, "is_bot": False, "first_name": "Admin"},
                            "text": "/admin",
                        },
                    }
                )
            ]

    gateway = object.__new__(Telegram)
    gateway.bot = BotReply()
    result = await gateway.call("get_updates")
    assert result[0]["message"]["from"]["id"] == 111


@pytest.mark.asyncio
async def test_telegram_empty_link_preview_has_no_default_sentinel():
    from aiogram.types import Message
    from app.telegram import Telegram

    class BotReply:
        async def send_message(self, **kw):
            return Message.model_validate(
                {
                    "message_id": 1,
                    "date": 1792216800,
                    "chat": {"id": 111, "type": "private"},
                    "text": "https://t.me/e_moda_jeans",
                    "link_preview_options": {},
                }
            )

    gateway = object.__new__(Telegram)
    gateway.bot = BotReply()
    result = await gateway.call("send_message")
    assert result["message_id"] == 1


@pytest.mark.asyncio
async def test_source_change_during_send_keeps_uncertain(db):
    p = await queued(db)

    class LateGateway(Gateway):
        async def call(self, method, **kw):
            if method == "copy_messages":
                await ingest(db, msg(3))
            return await super().call(method, **kw)

    await publish(db, LateGateway(), p)
    saved = await db.fetchrow("SELECT * FROM products")
    assert saved["status"] == "uncertain"
    assert len(saved["destination_message_ids"]) == 2
    assert len(saved["messages"]) == 3


@pytest.mark.asyncio
async def test_telegram_membership_discriminator_is_preserved():
    from aiogram.types import ChatMemberAdministrator
    from app.telegram import Telegram

    class BotReply:
        async def get_chat_member(self, **kw):
            return ChatMemberAdministrator.model_validate(
                {
                    "status": "administrator",
                    "user": {"id": 123, "is_bot": True, "first_name": "Sorting"},
                    "can_be_edited": True,
                    "is_anonymous": False,
                    "can_manage_chat": True,
                    "can_delete_messages": False,
                    "can_manage_video_chats": False,
                    "can_restrict_members": False,
                    "can_promote_members": False,
                    "can_change_info": False,
                    "can_invite_users": False,
                    "can_post_stories": False,
                    "can_edit_stories": False,
                    "can_delete_stories": False,
                    "can_post_messages": True,
                }
            )

    gateway = object.__new__(Telegram)
    gateway.bot = BotReply()
    result = await gateway.call("get_chat_member")
    assert result["status"] == "administrator" and result["can_post_messages"] is True


@pytest.mark.asyncio
async def test_slow_album_chunks_never_split_product(db):
    await ingest(db, msg(1, "first"))
    await db.execute("UPDATE products SET updated_at=now()-interval '60 seconds'")
    await ingest(db, msg(2, "second"))
    await ingest(db, msg(3, None, text="Код E-MODA SLOW"))
    assert await db.fetchval("SELECT count(*) FROM products") == 1
    p = await db.fetchrow("SELECT * FROM products")
    assert p["closed"] and len(p["messages"]) == 3


@pytest.mark.asyncio
async def test_unconfirmed_boundary_late_album_stays_one_review(db):
    await ingest(db, msg(1, "first"))
    await db.execute(
        "UPDATE products SET status='review',error='missing caption',updated_at=now()-interval '120 seconds'"
    )
    await ingest(db, msg(2, "second"))
    await ingest(db, msg(3, None, text="Код E-MODA LATE"))
    assert await db.fetchval("SELECT count(*) FROM products") == 1
    p = await db.fetchrow("SELECT * FROM products")
    assert p["closed"] and p["status"] == "review"


@pytest.mark.asyncio
async def test_quota_cooldown_retains_unanalyzed_product(db, monkeypatch):
    import time
    from app.pipeline import worker

    await ingest(db, msg(1))
    await ingest(db, msg(2, None, text="Код E-MODA QUOTA"))
    await db.execute("UPDATE products SET ready_at=now()-interval '1 second'")
    await put(db, "ai_retry_until", time.time() + 3600)
    calls = []

    async def forbidden(*args):
        calls.append("called")
        raise AssertionError("AI must not run during quota cooldown")

    monkeypatch.setattr("app.pipeline.classify", forbidden)
    task = asyncio.create_task(worker(db, Gateway()))
    await asyncio.sleep(1.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not calls
    assert await db.fetchval("SELECT status FROM products") == "collecting"


@pytest.mark.asyncio
async def test_gemini_key_is_hidden_and_removed_from_durable_update(db):
    await put(db, "admins", [111])
    await db.execute(
        "INSERT INTO admin_state VALUES(111,'config',$1)", {"key": "gemini_key"}
    )
    secret = "A" * 39
    u = {
        "update_id": 42,
        "message": {
            "message_id": 20,
            "from": {"id": 111},
            "chat": {"id": 111, "type": "private"},
            "text": secret,
        },
    }
    await db.execute("INSERT INTO updates(id,payload) VALUES(42,$1)", u)
    await handle(db, Gateway(), u)
    assert (
        await db.fetchval("SELECT value FROM settings WHERE key='gemini_key'") == secret
    )
    assert secret not in str(
        await db.fetchval("SELECT payload FROM updates WHERE id=42")
    )


@pytest.mark.asyncio
async def test_publication_is_complete_only_after_url_buttons(db):
    p = await queued(db)
    messages = p["messages"]
    messages[-1]["reply_markup"] = {
        "inline_keyboard": [[{"text": "Catalog", "url": "https://t.me/e_moda_dresses"}]]
    }
    await db.execute("UPDATE products SET messages=$2 WHERE id=$1", p["id"], messages)
    p = await db.fetchrow("SELECT * FROM products")

    class ButtonsGateway(Gateway):
        async def call(self, method, **kw):
            if method == "edit_message_reply_markup":
                assert await db.fetchval("SELECT status FROM products") == "finishing"
            return await super().call(method, **kw)

    tg = ButtonsGateway()
    await publish(db, tg, p)
    assert [m for m, k in tg.calls] == ["copy_messages", "edit_message_reply_markup"]
    assert await db.fetchval("SELECT status FROM products") == "published"


@pytest.mark.asyncio
async def test_button_retry_never_resends_product(db):
    from app.pipeline import finish_publication

    p = await queued(db)
    messages = p["messages"]
    messages[-1]["reply_markup"] = {
        "inline_keyboard": [[{"text": "Catalog", "url": "https://t.me/e_moda_dresses"}]]
    }
    await db.execute("UPDATE products SET messages=$2 WHERE id=$1", p["id"], messages)
    p = await db.fetchrow("SELECT * FROM products")

    class ButtonFailure(Gateway):
        async def call(self, method, **kw):
            if method == "edit_message_reply_markup":
                raise TimeoutError("uncertain button edit")
            return await super().call(method, **kw)

    await publish(db, ButtonFailure(), p)
    p = await db.fetchrow("SELECT * FROM products")
    assert p["status"] == "finishing"
    tg = Gateway()
    await finish_publication(db, tg, p)
    assert [m for m, k in tg.calls] == ["edit_message_reply_markup"]
    assert await db.fetchval("SELECT status FROM products") == "published"


@pytest.mark.asyncio
async def test_stale_database_interrupted_send_becomes_uncertain(db):
    from app.pipeline import recover_stale

    await queued(db)
    await db.execute(
        "UPDATE products SET status='sending',updated_at=now()-interval '11 minutes'"
    )
    await recover_stale(db)
    assert await db.fetchval("SELECT status FROM products") == "uncertain"


@pytest.mark.asyncio
async def test_stats_counts_category_changes_not_approvals(db):
    p = await queued(db)
    await put(db, "admins", [111])
    for action in ("change_category", "approve"):
        await db.execute(
            "INSERT INTO corrections(product_id,ai_category,final_category,actor,action) VALUES($1,$2,$3,111,$4)",
            p["id"],
            "women.dress",
            "women.dress",
            action,
        )
    tg = Gateway()
    await handle(
        db,
        tg,
        {
            "callback_query": {
                "id": "x",
                "from": {"id": 111},
                "data": "stats",
                "message": {"chat": {"id": 111, "type": "private"}},
            }
        },
    )
    assert any(
        method == "say" and "Исправлений: 1" in args[1] for method, args in tg.calls
    )


@pytest.mark.asyncio
async def test_changing_to_free_gemma_replaces_old_paid_tariffs(db):
    from app.db import setting

    await put(db, "admins", [111])
    await put(db, "input_price", 0.25)
    await put(db, "output_price", 1.50)
    await db.execute(
        "INSERT INTO admin_state VALUES(111,'config',$1)", {"key": "model"}
    )
    await handle(
        db,
        Gateway(),
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 111},
                "chat": {"id": 111, "type": "private"},
                "text": "gemma-4-26b-a4b-it",
            },
        },
    )
    assert await setting(db, "model") == "gemma-4-26b-a4b-it"
    assert (
        await setting(db, "input_price") == 0
        and await setting(db, "output_price") == 0
    )
    assert await db.fetchval("SELECT count(*) FROM admin_state") == 0


@pytest.mark.asyncio
async def test_standalone_captioned_media_closes_without_trailing_text(db):
    for mid in (1, 2):
        m = msg(mid, None, caption=f"Код / Code: AA{mid}")
        m["video"] = {"file_id": f"video_{mid}"}
        await ingest(db, m)
    rows = await db.fetch("SELECT * FROM products ORDER BY id")
    assert len(rows) == 2 and all(p["closed"] for p in rows)
    assert all(len(p["messages"]) == 1 for p in rows)


@pytest.mark.asyncio
async def test_caption_added_by_edit_closes_standalone_item(db):
    m = msg(1, None)
    m["video"] = {"file_id": "video"}
    await ingest(db, m)
    assert not await db.fetchval("SELECT closed FROM products")
    edited = {**m, "caption": "Код / Code: AA123"}
    await ingest(db, edited)
    assert await db.fetchval("SELECT closed FROM products")
    assert await db.fetchval("SELECT count(*) FROM products") == 1


@pytest.mark.asyncio
async def test_late_media_during_button_finalization_requires_reconciliation(db):
    p = await queued(db)
    await db.execute(
        "UPDATE products SET status='finishing',destination_message_ids='[101,102]'::jsonb"
    )
    await ingest(db, msg(3))
    result = await db.fetchrow("SELECT * FROM products")
    assert result["status"] == "uncertain"
    assert result["destination_message_ids"] == [101, 102]


@pytest.mark.asyncio
async def test_concurrent_ai_calls_share_database_rate_limit(db):
    from app.limits import wait_turn

    await put(db, "ai_rpm", 200)
    times = []

    async def request():
        await wait_turn(db, "model-one")
        times.append(asyncio.get_running_loop().time())

    await asyncio.gather(request(), request(), request())
    times.sort()
    assert all(b - a > 0.20 for a, b in zip(times, times[1:])), times
    assert await db.fetchval("SELECT count(*) FROM ai_limits") == 1


@pytest.mark.asyncio
async def test_admin_ai_rate_limit_is_configurable(db):
    await put(db, "admins", [111])
    await db.execute(
        "INSERT INTO admin_state VALUES(111,'config',$1)", {"key": "ai_rpm"}
    )
    await handle(
        db,
        Gateway(),
        {
            "message": {
                "message_id": 20,
                "from": {"id": 111},
                "chat": {"id": 111, "type": "private"},
                "text": "12",
            }
        },
    )
    assert await db.fetchval("SELECT value FROM settings WHERE key='ai_rpm'") == 12


@pytest.mark.asyncio
async def test_review_shows_russian_category_and_full_video(db):
    from app.admin import item

    p = await queued(db)
    message = {
        "message_id": 1,
        "chat": {"id": -1001, "type": "channel"},
        "video": {"file_id": "real-video-id"},
        "caption": "Код / Code: AA123",
    }
    await db.execute(
        "UPDATE products SET messages=$2,status='review',ai_category='women.dress',confidence=.94 WHERE id=$1",
        p["id"],
        [message],
    )
    tg = Gateway()
    await item(db, tg, 111, p["id"])
    assert any(
        method == "send_video" and args["video"] == "real-video-id"
        for method, args in tg.calls
    )
    assert any(
        method == "say" and "Итог: Женское → Dress" in args[1] and "94%" in args[1]
        for method, args in tg.calls
    )

@pytest.mark.asyncio
async def test_binding_channel_updates_visible_link_and_title(db):
 p=await queued(db);cat_id=await db.fetchval("SELECT id FROM categories WHERE code='women.dress'")
 await put(db,'admins',[111]);await db.execute("UPDATE categories SET link='https://t.me/old_target'")
 await db.execute("INSERT INTO admin_state VALUES(111,'destcat',$1)",{'id':cat_id})
 class Resolver(Gateway):
  async def call(self,method,**kw):
   if method=='get_chat':return {'id':-2001,'type':'channel','title':'New Target','username':'new_target'}
   if method=='get_me':return {'id':123}
   if method=='get_chat_member':return {'status':'administrator','can_post_messages':True}
   return await super().call(method,**kw)
 await handle(db,Resolver(),{'message':{'message_id':1,'from':{'id':111},'chat':{'id':111,'type':'private'},'text':'@new_target'}})
 assert await db.fetchval('SELECT link FROM categories')=='https://t.me/new_target'
 assert await db.fetchval('SELECT title FROM available_channels WHERE id=-2001')=='New Target'


@pytest.mark.asyncio
async def test_sources_navigation_supports_hundreds_without_large_keyboard(db):
    await put(db,'admins',[111])
    await db.executemany("INSERT INTO sources(id,title) VALUES($1,$2)",[(i,f'Source {i}') for i in range(-1300,-1200)])
    tg=Gateway()
    async def page(offset):
        await handle(db,tg,{'callback_query':{'id':'callback','from':{'id':111},'data':f'sources:{offset}','message':{'chat':{'id':111,'type':'private'}}}})
        return next(args[2] for method,args in reversed(tg.calls) if method=='say')
    first=await page(0)
    assert sum(b['callback_data'].startswith('source:') for row in first for b in row)==10
    assert any(b['callback_data']=='sources:10' for row in first for b in row)
    last=await page(100)
    assert sum(b['callback_data'].startswith('source:') for row in last for b in row)==1
    assert not any(b['callback_data'].startswith('sources:') for row in last for b in row)


def callback(data,actor=111):
    return {'callback_query':{'id':'callback','from':{'id':actor},'data':data,'message':{'chat':{'id':actor,'type':'private'}}}}


@pytest.mark.asyncio
async def test_unknown_item_routes_with_one_click_during_ai_quota_and_keeps_whole_product(db,monkeypatch):
    import time
    from app.pipeline import worker
    from app.main import notification_tick
    p=await queued(db)
    await db.execute("UPDATE products SET status='review',ai_category=NULL,final_category=NULL,error='Gemini HTTP429 quota'")
    await put(db,'admins',[111]);await put(db,'paused',False);await put(db,'notify_enabled',False)
    await put(db,'ai_retry_until',time.time()+3600)
    cid=await db.fetchval('SELECT id FROM categories')
    async def forbidden(*args):raise AssertionError('Manual routing must not call AI')
    monkeypatch.setattr('app.pipeline.classify',forbidden)
    tg=Gateway()
    await handle(db,tg,callback(f"route:{p['id']}:{cid}"))
    assert await db.fetchval('SELECT status FROM products')=='queued'
    task=asyncio.create_task(worker(db,tg))
    for _ in range(30):
        if await db.fetchval('SELECT status FROM products')=='published':break
        await asyncio.sleep(.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    result=await db.fetchrow('SELECT * FROM products')
    assert result['status']=='published' and result['destination_message_ids']==[101,102]
    assert result['decision_actor']==111 and result['final_category']=='women.dress'
    assert await db.fetchval("SELECT count(*) FROM corrections WHERE action='change_category'")==1
    await notification_tick(db,tg);await notification_tick(db,tg)
    assert sum(method=='say' and '✅ #'+str(p['id'])+' опубликован' in args[1] for method,args in tg.calls)==1
    assert await db.fetchval('SELECT publish_notice_id FROM products')==-1


@pytest.mark.asyncio
async def test_concurrent_direct_route_cannot_send_twice_and_logs_actor(db):
    from app.operations import queue_manual
    p=await queued(db);await db.execute("UPDATE products SET status='review'")
    cid=await db.fetchval('SELECT id FROM categories')
    results=await asyncio.gather(queue_manual(db,p['id'],cid,111),queue_manual(db,p['id'],cid,111),return_exceptions=True)
    assert sum(isinstance(r,dict) for r in results)==1
    assert sum(isinstance(r,ValueError) for r in results)==1
    assert await db.fetchval("SELECT count(*) FROM corrections WHERE action='approve'")==1
    assert await db.fetchval('SELECT decision_actor FROM products')==111


@pytest.mark.asyncio
async def test_direct_route_rejects_uncertain_disabled_source_loop_and_unauthorized(db):
    from app.operations import queue_manual
    p=await queued(db);cid=await db.fetchval('SELECT id FROM categories')
    await put(db,'admins',[111]);await db.execute("UPDATE products SET status='review'")
    await handle(db,Gateway(),callback(f"route:{p['id']}:{cid}",222))
    assert await db.fetchval('SELECT status FROM products')=='review'
    await db.execute('UPDATE categories SET enabled=false')
    with pytest.raises(ValueError):await queue_manual(db,p['id'],cid,111)
    await db.execute('UPDATE categories SET enabled=true,destination=-1001')
    with pytest.raises(ValueError):await queue_manual(db,p['id'],cid,111)
    await db.execute("UPDATE categories SET destination=-2001");await db.execute("UPDATE products SET status='uncertain'")
    with pytest.raises(ValueError):await queue_manual(db,p['id'],cid,111)
    assert await db.fetchval('SELECT count(*) FROM corrections')==0


@pytest.mark.asyncio
async def test_review_buttons_route_immediately_and_only_show_connected_categories(db):
    from app.admin import item
    p=await queued(db);await db.execute("UPDATE products SET status='review'")
    await db.execute("INSERT INTO categories(code,title,description,department) VALUES('women.hidden','Hidden','hidden','women')")
    tg=Gateway();await item(db,tg,111,p['id'])
    keys=next(args[2] for method,args in reversed(tg.calls) if method=='say')
    assert any(b['callback_data'].startswith(f"route:{p['id']}:") for row in keys for b in row)
    assert not any('Hidden' in b['text'] for row in keys for b in row)
    assert not any(b['callback_data'].startswith('setcat:') for row in keys for b in row)


@pytest.mark.asyncio
async def test_review_recipient_is_explicit_and_does_not_flood_old_backlog(db):
    from app.main import notification_tick
    from app.db import setting
    p=await queued(db);await db.execute("UPDATE products SET status='review',created_at=now()-interval '1 day'")
    await put(db,'admins',[222,111]);await put(db,'paused',False)
    tg=Gateway();await handle(db,tg,callback('reviewme'))
    assert await setting(db,'review_admin_id')==111
    await notification_tick(db,tg)
    assert await db.fetchval('SELECT review_chat_id FROM products') is None
    await db.execute('UPDATE products SET created_at=now()')
    await notification_tick(db,tg)
    assert await db.fetchval('SELECT review_chat_id FROM products')==111


@pytest.mark.asyncio
async def test_resume_cached_respects_mixed_manual_decisions_and_duplicate_deliveries(db):
    from app.operations import cached_count,resume_cached
    p=await queued(db)
    await db.execute("UPDATE products SET closed=true,status='review',ai_category=final_category,confidence=.95,ai_result=$1",{'mixed_products':True})
    await put(db,'mode','AUTO')
    assert await cached_count(db)==0
    await db.execute("UPDATE products SET ai_result=$1",{'mixed_products':False})
    assert await cached_count(db)==1
    await db.execute("INSERT INTO corrections(product_id,actor,action) VALUES($1,111,'change_category')",p['id'])
    assert await cached_count(db)==0
    await db.execute('DELETE FROM corrections');await db.execute("UPDATE products SET destination_message_ids='[101]'::jsonb")
    assert await cached_count(db)==0
    await db.execute('UPDATE products SET destination_message_ids=NULL')
    assert await resume_cached(db)==[p['id']]
    assert await resume_cached(db)==[]


@pytest.mark.asyncio
async def test_home_and_payment_help_report_quota_without_claiming_balance(db):
    import time
    await put(db,'admins',[111]);await put(db,'mode','AUTO');await put(db,'paused',False)
    await put(db,'ai_retry_until',time.time()+1000);await put(db,'ai_last_error','Gemini HTTP429 quota')
    tg=Gateway();await handle(db,tg,callback('home'));await handle(db,tg,callback('aihelp'))
    texts=[args[1] for method,args in tg.calls if method=='say']
    assert 'Режим: AUTO' in texts[0] and 'Ручная отправка работает' in texts[0]
    assert 'исчерпана квота' in texts[1] and 'не может узнать' in texts[1] and 'Gemma' in texts[1] and 'Free mode' in texts[1]


@pytest.mark.asyncio
async def test_binding_enables_category_and_keeps_unrelated_custom_route(db):
    from app.admin import bind_category
    p=await queued(db);cid=await db.fetchval('SELECT id FROM categories')
    await db.execute('UPDATE categories SET enabled=false')
    await db.execute("INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.custom','Test','Test','women',-3001,false)")
    await bind_category(db,cid,{'id':-2001,'username':'dress','title':'Dress'})
    assert await db.fetchval('SELECT enabled FROM categories WHERE id=$1',cid)
    assert not await db.fetchval("SELECT enabled FROM categories WHERE code='women.custom'")


@pytest.mark.asyncio
async def test_finished_waiting_for_ai_can_be_routed_but_incomplete_item_cannot(db):
    from app.operations import queue_manual
    p=await queued(db);cid=await db.fetchval('SELECT id FROM categories')
    await db.execute("UPDATE products SET status='collecting',closed=false,ready_at=now()-interval '1 second'")
    with pytest.raises(ValueError):await queue_manual(db,p['id'],cid,111)
    await db.execute('UPDATE products SET closed=true')
    await queue_manual(db,p['id'],cid,111)
    assert await db.fetchval('SELECT status FROM products')=='queued'


@pytest.mark.asyncio
async def test_missing_original_messages_explained_and_not_requeued_by_stale_button(db):
    from app.admin import item
    from app.operations import queue_manual
    p=await queued(db);cid=await db.fetchval('SELECT id FROM categories')
    await db.execute("UPDATE products SET status='review',error='Telegram server says - Bad Request: there are no messages to forward'")
    p=await db.fetchrow('SELECT * FROM products')
    tg=Gateway();await item(db,tg,111,p['id'])
    card=next(args for method,args in reversed(tg.calls) if method=='say')
    assert 'Telegram не нашёл исходные сообщения' in card[1]
    assert 'Выберите канал вручную' not in card[1]
    assert not any(b['callback_data'].startswith(('route:','reanalyze:')) for row in card[2] for b in row)
    assert any(b['callback_data']==f"skip:{p['id']}" for row in card[2] for b in row)
    with pytest.raises(ValueError,match='Смена категории не восстановит'):
        await queue_manual(db,p['id'],cid,111)
    assert await db.fetchval('SELECT status FROM products')=='review'
    assert await db.fetchval('SELECT count(*) FROM corrections')==0
