"""One durable delivery per product/channel; a completed channel is never recopied."""
import hashlib,json
from datetime import datetime,timezone
from aiogram.exceptions import TelegramRetryAfter,TelegramBadRequest,TelegramForbiddenError
from .routing import extra_categories, selected_categories, automatic_selection
from .db import setting


def source_hash(messages):
    return hashlib.sha256(json.dumps(messages,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


async def rows(db,pid):
    return await db.fetch('SELECT d.*,c.title FROM product_deliveries d LEFT JOIN categories c ON c.code=d.category_code WHERE product_id=$1 ORDER BY d.id',pid)


async def refresh(db,pid):
    async with db.acquire() as c,c.transaction():
        p=await c.fetchrow('SELECT * FROM products WHERE id=$1 FOR UPDATE',pid)
        ds=await c.fetch('SELECT * FROM product_deliveries WHERE product_id=$1 ORDER BY id',pid)
        if not ds:return
        if any(d['source_hash']!=source_hash(p['messages']) for d in ds):
            await c.execute("UPDATE products SET status='uncertain',error='Источник изменился после начала отправки: требуется сверка всех каналов',updated_at=now() WHERE id=$1",pid)
            return
        states={d['status'] for d in ds}
        status=next((s for s in ('sending','uncertain','review','finishing','queued') if s in states),'skipped' if 'skipped' in states else 'published')
        pending=next((d for d in ds if d['status']!='published'),None)
        errors='; '.join(f"{d['category_code']}: {d['error']}" for d in ds if d['error']) or None
        await c.execute("UPDATE products SET review_message_id=CASE WHEN status<>$2 AND $2 IN ('review','uncertain') THEN NULL ELSE review_message_id END,status=$2,error=$3,ready_at=$4,updated_at=now() WHERE id=$1",pid,status,errors,pending['ready_at'] if pending else datetime.now(timezone.utc))


async def ensure_plan(db,pid):
    async with db.acquire() as c,c.transaction():
        p=await c.fetchrow('SELECT * FROM products WHERE id=$1 FOR UPDATE',pid)
        if not p or p['status']!='queued':return False
        if await c.fetchval('SELECT 1 FROM product_deliveries WHERE product_id=$1',pid):return True
        try:
            primary=await c.fetchrow('SELECT * FROM categories WHERE code=$1 AND enabled AND destination IS NOT NULL',p['final_category'])
            if not primary:raise ValueError('Категория не привязана к доступному каналу')
            codes=selected_categories(p)
            if not codes or codes[0]!=p['final_category']:
                raise ValueError('Список категорий товара некорректен: выберите категории заново')
            ai_result=p['ai_result'] or {}
            if not p['decision_actor'] and ai_result.get('category')==p['final_category'] and 'confidence' in ai_result:
                threshold=((p['usage'] or {}).get('routing') or {}).get('auto_threshold',await setting(c,'auto_threshold',.85))
                routing=await automatic_selection(c,ai_result,threshold)
                if routing.get('primary_blocked'):
                    raise ValueError('Эта категория принимает только отдельные вещи. AI определил комплект или не установил состав продажи; выберите категорию вручную.')
                # A secondary channel disabled before planning cannot stop the primary.
                codes=[code for code in codes if code in routing['selected']]
                routing['selected']=codes
                await c.execute("UPDATE products SET final_categories=$2,usage=jsonb_set(COALESCE(usage,'{}'::jsonb),'{routing}',$3::jsonb) WHERE id=$1",pid,codes,routing)
            targets=[]
            ai_extras={r['category']:r for r in (p['ai_result'] or {}).get('additional_categories',[])}
            for code in codes:
                target=await c.fetchrow('SELECT * FROM categories WHERE code=$1 AND enabled AND destination IS NOT NULL',code)
                if not target:
                    raise ValueError('Выбранная категория выключена или не привязана: '+code+'. Проверьте каналы в /admin.')
                evidence={'rule':'manual_category' if p['decision_actor'] else 'ai_category'}
                if code in ai_extras and not p['decision_actor']:evidence.update(ai_extras[code])
                targets.append((dict(target),evidence))
            targets+=await extra_categories(c,p,primary)
            ids=sorted(m['message_id'] for m in p['messages'])
            if not ids or len(ids)>100:raise ValueError('Нужно от 1 до 100 сообщений для отправки всего товара')
            for cat,evidence in targets:
                if cat['destination']==p['source_channel_id'] or await c.fetchval('SELECT 1 FROM sources WHERE id=$1',cat['destination']):
                    raise ValueError('Назначение совпадает с основным источником')
            for i,(cat,evidence) in enumerate(targets):
                await c.execute("INSERT INTO product_deliveries(product_id,category_code,destination,kind,rule_evidence,source_hash,source_message_ids) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(product_id,destination) DO NOTHING",pid,cat['code'],cat['destination'],'primary' if i==0 else 'extra',evidence,source_hash(p['messages']),ids)
            await c.execute('UPDATE products SET destination_channel_id=$2 WHERE id=$1',pid,primary['destination'])
            return True
        except ValueError as e:
            await c.execute("UPDATE products SET status='review',error=$2,updated_at=now() WHERE id=$1",pid,str(e))
            return False


async def finish_one(db,tg,d):
    p=await db.fetchrow('SELECT * FROM products WHERE id=$1',d['product_id'])
    if not p or d['status']!='finishing':return
    if source_hash(p['messages'])!=d['source_hash']:
        await db.execute("UPDATE product_deliveries SET status='uncertain',error='Источник изменился во время отправки',updated_at=now() WHERE id=$1",d['id'])
        await refresh(db,p['id']);return
    messages=sorted(p['messages'],key=lambda x:x['message_id']);ids=d['message_ids'] or []
    if len(ids)!=len(messages):
        await db.execute("UPDATE product_deliveries SET status='uncertain',error='Число сообщений не совпало: ручная сверка',updated_at=now() WHERE id=$1",d['id'])
        await refresh(db,p['id']);return
    try:
        for original,mid in zip(messages,ids):
            keyboard=[[b for b in row if b.get('url')] for row in original.get('reply_markup',{}).get('inline_keyboard',[])]
            keyboard=[row for row in keyboard if row]
            if keyboard:
                try:await tg.call('edit_message_reply_markup',chat_id=d['destination'],message_id=mid,reply_markup={'inline_keyboard':keyboard})
                except TelegramBadRequest as e:
                    if 'message is not modified' not in str(e).lower():raise
        await db.execute("UPDATE product_deliveries SET status='published',error=NULL,updated_at=now() WHERE id=$1 AND status='finishing'",d['id'])
    except Exception as e:
        delay=float(e.retry_after) if isinstance(e,TelegramRetryAfter) else 60.
        await db.execute("UPDATE product_deliveries SET error=$2,ready_at=now()+$3*interval '1 second',updated_at=now() WHERE id=$1 AND status='finishing'",d['id'],'Товар скопирован; восстанавливаются URL-кнопки: '+str(e)[:500],delay)
    await refresh(db,p['id'])


async def send_one(db,tg,d):
    async with db.acquire() as c,c.transaction():
        p=await c.fetchrow('SELECT * FROM products WHERE id=$1 FOR UPDATE',d['product_id'])
        if not p or p['status'] not in ('queued','finishing'):return
        if source_hash(p['messages'])!=d['source_hash']:
            await c.execute("UPDATE products SET status='uncertain',error='Источник изменился после начала отправки: требуется сверка',updated_at=now() WHERE id=$1",p['id']);return
        # Resolve no new channels on retries: the committed plan is the delivery target.
        if await c.fetchval('SELECT 1 FROM sources WHERE id=$1',d['destination']):
            await c.execute("UPDATE product_deliveries SET status='review',error='Канал стал основным источником',updated_at=now() WHERE id=$1",d['id'])
            await c.execute("UPDATE products SET status='review',error='Канал стал основным источником',updated_at=now() WHERE id=$1",p['id'])
            return
        claimed=await c.fetchval("UPDATE product_deliveries SET status='sending',updated_at=now() WHERE id=$1 AND status='queued' AND ready_at<=now() RETURNING id",d['id'])
        if not claimed:return
        await c.execute("UPDATE products SET status='sending',updated_at=now() WHERE id=$1",p['id'])
    try:
        sent=await tg.call('copy_messages',chat_id=d['destination'],from_chat_id=p['source_channel_id'],message_ids=d['source_message_ids'])
        ids=[m['message_id'] for m in sent]
        await db.execute('UPDATE product_deliveries SET message_ids=$2,updated_at=now() WHERE id=$1',d['id'],ids)
        if d['kind']=='primary':await db.execute('UPDATE products SET destination_message_ids=$2 WHERE id=$1',p['id'],ids)
        if len(ids)!=len(d['source_message_ids']):raise RuntimeError(f"Telegram скопировал {len(ids)} из {len(d['source_message_ids'])} сообщений; сверить вручную")
        await db.execute("UPDATE product_deliveries SET status='finishing',error=NULL,updated_at=now() WHERE id=$1 AND status='sending'",d['id'])
        await refresh(db,p['id'])
        await finish_one(db,tg,await db.fetchrow('SELECT * FROM product_deliveries WHERE id=$1',d['id']))
    except TelegramRetryAfter as e:
        await db.execute("UPDATE product_deliveries SET status='queued',ready_at=now()+$2*interval '1 second',error='Telegram rate limit',updated_at=now() WHERE id=$1",d['id'],float(e.retry_after))
    except (TelegramBadRequest,TelegramForbiddenError) as e:
        await db.execute("UPDATE product_deliveries SET status='review',error=$2,updated_at=now() WHERE id=$1",d['id'],str(e)[:700])
    except Exception as e:
        await db.execute("UPDATE product_deliveries SET status='uncertain',error=$2,updated_at=now() WHERE id=$1",d['id'],str(e)[:700])
    await refresh(db,p['id'])


async def drive(db,tg,pid):
    while True:
        p=await db.fetchrow('SELECT * FROM products WHERE id=$1',pid)
        if not p or p['status'] not in ('queued','finishing'):return
        d=await db.fetchrow("SELECT * FROM product_deliveries WHERE product_id=$1 AND status<>'published' ORDER BY id LIMIT 1",pid)
        if not d:await refresh(db,pid);return
        if d['ready_at']>datetime.now(timezone.utc):return
        if d['status']=='queued':await send_one(db,tg,d)
        elif d['status']=='finishing':await finish_one(db,tg,d)
        else:return
        current=await db.fetchval('SELECT status FROM product_deliveries WHERE id=$1',d['id'])
        if current!='published':return


async def publish(db,tg,p):
    if await ensure_plan(db,p['id']):await drive(db,tg,p['id'])


async def recover(db,startup=False):
    changed=await db.fetch("UPDATE product_deliveries SET status='uncertain',error='Отправка прервана: сверить этот канал перед повтором',updated_at=now() WHERE status='sending' AND ($1 OR updated_at<now()-interval '10 minutes') RETURNING product_id",startup)
    for pid in {r['product_id'] for r in changed}:await refresh(db,pid)


async def decide(db,pid,did,action,actor):
    async with db.acquire() as c,c.transaction():
        p=await c.fetchrow('SELECT * FROM products WHERE id=$1 FOR UPDATE',pid)
        d=await c.fetchrow('SELECT * FROM product_deliveries WHERE id=$1 AND product_id=$2 FOR UPDATE',did,pid)
        if not d or source_hash(p['messages'])!=d['source_hash']:raise ValueError('Источник изменился: сначала сверьте товар во всех каналах. Автоповтор запрещён.')
        if action=='retrydelivery':
            if d['status']!='review':raise ValueError('Эта отправка уже обработана. Повтор запрещён.')
            target='queued'
        else:
            if d['status']!='uncertain':raise ValueError('Эта отправка уже сверена. Повтор запрещён.')
            target='published' if action=='deliveryyes' else 'queued'
        await c.execute('UPDATE product_deliveries SET status=$2,error=NULL,ready_at=now(),updated_at=now() WHERE id=$1',did,target)
        await c.execute('UPDATE products SET decision_actor=$2,publish_notice_id=NULL WHERE id=$1',pid,actor)
        await c.execute("INSERT INTO corrections(product_id,ai_category,final_category,actor,action) VALUES($1,$2,$3,$4,$5)",pid,p['ai_category'],d['category_code'],actor,action+':'+str(did))
    await refresh(db,pid)


async def skip_unstarted(db,pid,actor):
    async with db.acquire() as c,c.transaction():
        p=await c.fetchrow('SELECT * FROM products WHERE id=$1 FOR UPDATE',pid)
        ds=await c.fetch('SELECT * FROM product_deliveries WHERE product_id=$1 FOR UPDATE',pid)
        if not p or p['status']!='review' or not ds or any(d['status'] not in ('queued','review') or d['message_ids'] for d in ds):
            raise ValueError('Товар уже отправлен хотя бы в один канал или исход отправки неизвестен. Используйте сверку отдельного канала.')
        await c.execute("UPDATE product_deliveries SET status='skipped',updated_at=now() WHERE product_id=$1",pid)
        await c.execute("UPDATE products SET status='skipped',updated_at=now() WHERE id=$1",pid)
        await c.execute("INSERT INTO corrections(product_id,ai_category,final_category,actor,action) VALUES($1,$2,$3,$4,'skip')",pid,p['ai_category'],p['final_category'],actor)
