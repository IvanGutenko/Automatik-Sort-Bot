"""Atomic operator decisions; no AI request is needed for manual routing."""
from datetime import datetime, timezone
from .db import setting
from .routing import selected_categories, automatic_selection


def source_unavailable(p):
    return 'there are no messages to forward' in (p.get('error') or '').lower()


def source_unavailable_text():
    return ('Telegram не нашёл исходные сообщения этого товара. Смена категории не восстановит их. '
            'Опубликуйте товар заново в основном канале: новая публикация поступит в сортировщик. '
            'Эту карточку можно пропустить.')


async def editable_product(c, pid):
    p = await c.fetchrow('SELECT * FROM products WHERE id=$1 FOR UPDATE', pid)
    if not p or not (p['status'] in ('review','low_confidence') or (p['status']=='collecting' and p['closed'] and p['ready_at'] <= datetime.now(timezone.utc))) or p['destination_message_ids']:
        raise ValueError('Товар уже обработан или отправляется; повтор запрещён')
    if await c.fetchval('SELECT 1 FROM product_deliveries WHERE product_id=$1',pid):
        raise ValueError('Отправки уже запланированы. Используйте повтор или сверку отдельного канала в карточке товара.')
    if source_unavailable(p):
        raise ValueError(source_unavailable_text())
    return p


async def queue_manual(db, pid, category_id, actor):
    return (await queue_manual_many(db,pid,[category_id],actor))[0]


async def queue_manual_many(db, pid, category_ids, actor, use_draft=False):
    async with db.acquire() as c, c.transaction():
        p = await editable_product(c,pid)
        if use_draft:
            category_ids=await c.fetchval('SELECT category_ids FROM route_drafts WHERE actor=$1 AND product_id=$2 FOR UPDATE',actor,pid)
            if category_ids is None:raise ValueError('Сначала выберите категории товара')
        if category_ids is None:
            category_ids=[]
            for code in selected_categories(p):
                cid=await c.fetchval('SELECT id FROM categories WHERE code=$1',code)
                if cid is None:raise ValueError('Категория больше не существует: выберите категории заново')
                category_ids.append(cid)
        category_ids=list(dict.fromkeys(category_ids))
        if not category_ids:raise ValueError('Выберите хотя бы одну категорию')
        cats=[]
        for cid in category_ids:
            cat=await c.fetchrow('SELECT * FROM categories WHERE id=$1 AND enabled AND destination IS NOT NULL',cid)
            if not cat:raise ValueError('Канал выбранной категории не подключён или категория выключена. Проверьте «Категории / каналы».')
            if await c.fetchval('SELECT 1 FROM sources WHERE id=$1',cat['destination']):
                raise ValueError('Нельзя отправлять товар обратно в основной источник')
            cats.append(dict(cat))
        codes=[cat['code'] for cat in cats]
        await c.execute("UPDATE products SET final_category=$2,final_categories=$4,status='queued',error=NULL,ready_at=now(),updated_at=now(),decision_actor=$3 WHERE id=$1",pid,codes[0],actor,codes)
        if p['final_category'] != codes[0]:
            await c.execute("INSERT INTO corrections(product_id,ai_category,final_category,actor,action) VALUES($1,$2,$3,$4,'change_category')",pid,p['ai_category'],codes[0],actor)
        await c.execute("INSERT INTO corrections(product_id,ai_category,final_category,actor,action) VALUES($1,$2,$3,$4,$5)",pid,p['ai_category'],codes[0],actor,'approve' if len(codes)==1 else 'approve_categories:'+','.join(codes))
        await c.execute('DELETE FROM route_drafts WHERE product_id=$1',pid)
        return cats


async def route_draft(db, pid, actor, toggle=None):
    async with db.acquire() as c,c.transaction():
        p=await editable_product(c,pid)
        current=await c.fetchval('SELECT category_ids FROM route_drafts WHERE actor=$1 AND product_id=$2 FOR UPDATE',actor,pid)
        if current is None:
            current=[]
            for code in selected_categories(p):
                cid=await c.fetchval('SELECT id FROM categories WHERE code=$1 AND enabled AND destination IS NOT NULL',code)
                if cid is not None:current.append(cid)
        if toggle is not None:
            if toggle in current:current.remove(toggle)
            else:
                if not await c.fetchval('SELECT 1 FROM categories WHERE id=$1 AND enabled AND destination IS NOT NULL',toggle):
                    raise ValueError('Категория выключена или канал не подключён')
                current.append(toggle)
        await c.execute('INSERT INTO route_drafts(actor,product_id,category_ids) VALUES($1,$2,$3) ON CONFLICT(actor,product_id) DO UPDATE SET category_ids=$3,updated_at=now()',actor,pid,current)
        return dict(p),current


# Reuse only untouched, complete, confidently recognized products. Manual decisions
# and uncertain deliveries must never be overridden by the backlog action.
ELIGIBLE = """
FROM products p JOIN sources s ON s.id=p.source_channel_id
JOIN categories c ON c.code=p.final_category
WHERE p.status IN ('review','low_confidence') AND s.enabled AND p.closed
AND c.enabled AND c.destination IS NOT NULL
AND (NOT c.single_only OR p.ai_result->>'sale_unit'='single')
AND NOT EXISTS(SELECT 1 FROM sources loop WHERE loop.id=c.destination)
AND p.ai_result IS NOT NULL AND p.ai_category=p.final_category
AND COALESCE((p.ai_result->>'confidence')::double precision,p.confidence) >= $1 AND COALESCE((p.ai_result->>'mixed_products')::boolean,false)=false
AND p.error IS NULL AND p.destination_message_ids IS NULL AND p.decision_actor IS NULL
AND NOT EXISTS(SELECT 1 FROM route_drafts draft WHERE draft.product_id=p.id)
AND NOT EXISTS(SELECT 1 FROM corrections a WHERE a.product_id=p.id)
AND NOT EXISTS(SELECT 1 FROM product_deliveries d WHERE d.product_id=p.id)
"""


async def cached_count(db):
    return await db.fetchval('SELECT count(*) '+ELIGIBLE, await setting(db,'auto_threshold',.85))


async def resume_cached(db):
    if await setting(db,'mode','REVIEW') != 'AUTO':
        raise ValueError('Сначала включите AUTO в настройках')
    threshold = await setting(db,'auto_threshold',.85)
    async with db.acquire() as c, c.transaction():
        rows=await c.fetch('SELECT p.* '+ELIGIBLE+' ORDER BY p.id FOR UPDATE OF p',threshold)
        ids=[r['id'] for r in rows]
        for p in rows:
            result={**p['ai_result'],'category':p['final_category'],
                    'confidence':p['ai_result'].get('confidence',p['confidence'])}
            routing=await automatic_selection(c,result,threshold)
            await c.execute("UPDATE products SET status='queued',confidence=$2,final_categories=$3,usage=jsonb_set(COALESCE(usage,'{}'::jsonb),'{routing}',$4::jsonb),ready_at=now(),updated_at=now() WHERE id=$1",p['id'],result['confidence'],routing['selected'],routing)
        return ids
