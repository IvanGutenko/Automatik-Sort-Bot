import pytest
from test_pipeline import db
from test_multi_categories import prepared,result,run_until_decided,copies
from test_providers import transport,success,MediaGateway

@pytest.mark.asyncio
async def test_weak_extra_must_not_block_confident_primary(db,monkeypatch):
 await prepared(db);tg=MediaGateway()
 transport(monkeypatch,lambda request:success('google',result(.78)))
 p=await run_until_decided(db,tg)
 assert p['status']=='published'
 assert p['confidence']==.95 and p['final_categories']==['women.dress']
 assert [call['chat_id'] for call in copies(tg)]==[-2001]
 assert p['ai_result']['additional_categories'][0]['confidence']==.78

from app.db import put,setting
from app.operations import cached_count,resume_cached,queue_manual_many,route_draft
from app.pipeline import publish
from app.deliveries import ensure_plan
from app.admin import item,handle
from test_pipeline import Gateway,callback

@pytest.mark.asyncio
@pytest.mark.parametrize('main_conf,mixed,status',[(.8,False,'review'),(.5,False,'low_confidence'),(.95,True,'review')])
async def test_real_primary_uncertainty_and_mixed_products_remain_reviewed(db,monkeypatch,main_conf,mixed,status):
 await prepared(db);tg=MediaGateway();value=dict(result(.3),confidence=main_conf,mixed_products=mixed)
 transport(monkeypatch,lambda request:success('google',value))
 p=await run_until_decided(db,tg)
 assert p['status']==status and p['confidence']==main_conf and not copies(tg)
 assert await cached_count(db)==0

@pytest.mark.asyncio
async def test_primary_channel_unavailable_never_routes_to_secondary_instead(db,monkeypatch):
 p,ids=await prepared(db);await db.execute('UPDATE categories SET enabled=false WHERE id=$1',ids[0])
 tg=MediaGateway();transport(monkeypatch,lambda request:success('google',result(.98)))
 p=await run_until_decided(db,tg)
 assert p['status']=='review' and not copies(tg)
 assert await cached_count(db)==0

@pytest.mark.asyncio
async def test_recover_original_low_confidence_record_without_reanalysis(db):
 p,ids=await prepared(db);original=result(.2)
 await db.execute("UPDATE products SET closed=true,status='low_confidence',ai_category='women.dress',final_category='women.dress',final_categories='[\"women.dress\",\"women.modest\"]',ai_result=$1,confidence=.2",original)
 assert await cached_count(db)==1
 assert await resume_cached(db)==[p['id']]
 row=await db.fetchrow('SELECT * FROM products')
 assert row['confidence']==.95 and row['ai_result']==original and row['final_categories']==['women.dress']
 tg=Gateway();await publish(db,tg,row)
 assert [c['chat_id'] for c in copies(tg)]==[-2001]
 assert await db.fetchval('SELECT count(*) FROM ai_requests')==0
 assert await cached_count(db)==0 and await resume_cached(db)==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('guard',['mixed','correction','draft','error','already_planned','decision_actor'])
async def test_bulk_recovery_preserves_manual_work_and_other_blockers(db,guard):
 p,ids=await prepared(db);value=dict(result(.2),mixed_products=guard=='mixed')
 await db.execute("UPDATE products SET status='review',closed=true,ai_category='women.dress',final_category='women.dress',confidence=.2,ai_result=$1",value)
 if guard=='correction':await db.execute("INSERT INTO corrections(product_id,actor,action) VALUES($1,111,'choose')",p['id'])
 elif guard=='draft':await route_draft(db,p['id'],111)
 elif guard=='decision_actor':await db.execute('UPDATE products SET decision_actor=111')
 elif guard=='error':await db.execute("UPDATE products SET error='late edit'")
 elif guard=='already_planned':
  await db.execute("UPDATE products SET status='queued',final_categories='[\"women.dress\"]'")
  assert await ensure_plan(db,p['id'])
  await db.execute("UPDATE products SET status='review'")
 assert await cached_count(db)==0 and await resume_cached(db)==[]
 assert await db.fetchval('SELECT status FROM products')=='review'

@pytest.mark.asyncio
async def test_mode_review_still_requires_manual_decision_and_allows_explicit_weak_category(db,monkeypatch):
 p,ids=await prepared(db);await put(db,'mode','REVIEW');tg=MediaGateway()
 transport(monkeypatch,lambda request:success('google',result(.3)))
 p=await run_until_decided(db,tg)
 assert p['status']=='review' and not copies(tg)
 with pytest.raises(ValueError):await resume_cached(db)
 await handle(db,tg,callback(f"multitoggle:{p['id']}:{ids[1]}:all:0"))
 await handle(db,tg,callback(f"multisend:{p['id']}"));await publish(db,tg,p)
 assert sorted(c['chat_id'] for c in copies(tg))==[-2002,-2001]

@pytest.mark.asyncio
async def test_optional_channel_disabled_after_analysis_does_not_block_primary(db):
 p,ids=await prepared(db);value=result()
 await db.execute("UPDATE products SET status='queued',final_category='women.dress',ai_category='women.dress',final_categories='[\"women.dress\",\"women.modest\"]',ai_result=$1",value)
 await db.execute('UPDATE categories SET enabled=false WHERE id=$1',ids[1])
 tg=Gateway();await publish(db,tg,await db.fetchrow('SELECT * FROM products'))
 assert [c['chat_id'] for c in copies(tg)]==[-2001]
 assert await db.fetchval('SELECT status FROM products')=='published'

@pytest.mark.asyncio
async def test_mandatory_plus_size_still_applies_after_weak_extra_is_omitted(db,monkeypatch):
 p,ids=await prepared(db)
 await db.execute("UPDATE products SET messages=jsonb_set(messages,'{2,text}',to_jsonb('Размеры: 44–50'::text))")
 await db.execute("INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.plus_size','Plus','Plus','women',-2004,true)")
 tg=MediaGateway();transport(monkeypatch,lambda request:success('google',result(.2)))
 p=await run_until_decided(db,tg)
 assert p['status']=='published' and sorted(c['chat_id'] for c in copies(tg))==[-2004,-2001]
