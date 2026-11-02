"""A sold set must not enter a standalone-garment channel through AUTO."""
import json
import pytest
from test_pipeline import db,Gateway,callback
from test_providers import RESULT,MediaGateway,transport,success
from test_multi_categories import prepared,run_until_decided,copies
from app.ai import Result,google_schema,parse_result
from app.routing import automatic_selection,omitted_text,CORE_AI_RULES
from app.admin import handle
from app.routing_admin import rule_pages
from app.db import put
from app.deliveries import ensure_plan
from app.pipeline import publish
from app.operations import cached_count,resume_cached,queue_manual_many

async def setup(db,department='women'):
 p,ids=await prepared(db)
 primary='women.suit_casual' if department=='women' else 'men.sport'
 for code,title,destination,single in [(primary,'Комплекты',-2101,False),(department+'.pants','Брюки',-2102,True)]:
  await db.execute('INSERT INTO categories(code,title,description,department,destination,enabled,single_only) VALUES($1,$2,$2,$3,$4,true,$5)',code,title,department,destination,single)
 return p,dict(RESULT,department=department,category=primary,sale_unit='set',additional_categories=[{'category':department+'.pants','confidence':.99,'reason':'В комплект входят брюки'}])

@pytest.mark.asyncio
@pytest.mark.parametrize('department',['women','men'])
async def test_confident_sets_publish_without_pants_and_without_extra_review(db,monkeypatch,department):
 p,value=await setup(db,department);tg=MediaGateway()
 transport(monkeypatch,lambda request:success('google',value))
 row=await run_until_decided(db,tg)
 assert row['status']=='published' and row['final_categories']==[value['category']]
 assert row['confidence']==.95 and row['ai_result']==value
 assert [r['chat_id'] for r in copies(tg)]==[-2101]
 assert row['usage']['routing']['omitted']==[{'category':department+'.pants','confidence':.99,'cause':'sale_unit'}]
 assert 'комплект или его часть сюда не отправляется' in await omitted_text(db,row)

@pytest.mark.asyncio
async def test_denim_set_keeps_denim_and_modest_plus_size_but_not_pants(db,monkeypatch):
 p,value=await setup(db)
 await db.execute("UPDATE categories SET code='women.denim' WHERE code='women.suit_casual'")
 await db.execute("INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.plus_size','Plus','Plus','women',-2103,true)")
 await db.execute("UPDATE products SET messages=jsonb_set(messages,'{2,text}',to_jsonb('Размеры: 44–50'::text))")
 value.update(category='women.denim',additional_categories=value['additional_categories']+[{'category':'women.modest','confidence':.95,'reason':'Закрытый комплект'}])
 tg=MediaGateway();transport(monkeypatch,lambda request:success('google',value))
 row=await run_until_decided(db,tg)
 assert row['status']=='published' and row['final_categories']==['women.denim','women.modest']
 assert sorted(r['chat_id'] for r in copies(tg))==[-2103,-2101,-2002]

@pytest.mark.asyncio
async def test_standalone_sport_pants_still_route_to_pants_and_sport(db,monkeypatch):
 p,value=await setup(db,'men')
 value.update(category='men.pants',sale_unit='single',additional_categories=[{'category':'men.sport','confidence':.95,'reason':'Отдельные спортивные брюки'}])
 tg=MediaGateway();transport(monkeypatch,lambda request:success('google',value))
 row=await run_until_decided(db,tg)
 assert row['status']=='published' and row['final_categories']==['men.pants','men.sport']
 assert sorted(r['chat_id'] for r in copies(tg))==[-2102,-2101]

@pytest.mark.asyncio
@pytest.mark.parametrize('unit',['set','unknown'])
async def test_primary_pants_conflicting_with_sale_unit_cannot_publish_or_bulk_resume(db,monkeypatch,unit):
 p,value=await setup(db);value.update(category='women.pants',sale_unit=unit,additional_categories=[])
 tg=MediaGateway();transport(monkeypatch,lambda request:success('google',value))
 row=await run_until_decided(db,tg)
 assert row['status']=='review' and not copies(tg)
 assert await cached_count(db)==0 and await resume_cached(db)==[]
 assert 'AUTO остановлен' in await omitted_text(db,row)
 await db.execute("UPDATE products SET status='queued'")
 assert not await ensure_plan(db,p['id'])
 assert await db.fetchval('SELECT status FROM products')=='review'
 assert await db.fetchval('SELECT count(*) FROM product_deliveries')==0

@pytest.mark.asyncio
async def test_plan_rechecks_single_only_setting_changed_after_analysis(db):
 p,value=await setup(db)
 await db.execute("UPDATE products SET status='queued',ai_category=$1,final_category=$1,final_categories=$2,ai_result=$3",value['category'],[value['category'],'women.pants'],value)
 tg=Gateway();await publish(db,tg,p)
 assert [r['chat_id'] for r in copies(tg)]==[-2101]
 assert await db.fetchval('SELECT final_categories FROM products')==['women.suit_casual']

@pytest.mark.asyncio
async def test_manual_selection_is_explicit_and_not_overridden_by_auto_filter(db):
 p,value=await setup(db)
 await db.execute("UPDATE products SET status='review',ai_category=$1,final_category=$1,ai_result=$2",value['category'],value)
 ids=[await db.fetchval('SELECT id FROM categories WHERE code=$1',code) for code in [value['category'],'women.pants']]
 await queue_manual_many(db,p['id'],ids,111)
 tg=Gateway();await publish(db,tg,p)
 assert sorted(r['chat_id'] for r in copies(tg))==[-2102,-2101]

@pytest.mark.asyncio
async def test_admin_can_see_change_and_persist_filter_without_losing_rule_or_draft(db):
 p,value=await setup(db);cid=await db.fetchval("SELECT id FROM categories WHERE code='women.pants'")
 await db.execute("INSERT INTO admin_state VALUES(111,'namecat',jsonb_build_object('id',$1::int))",cid)
 tg=Gateway();await handle(db,tg,callback(f'category:{cid}'))
 assert 'да — комплекты исключены' in tg.calls[-1][1][1]
 assert any(b['callback_data']==f'singlecat:{cid}' for row in tg.calls[-1][1][2] for b in row)
 before=await db.fetchrow('SELECT * FROM categories WHERE id=$1',cid)
 await handle(db,tg,callback(f'singlecat:{cid}'))
 assert 'НЕТ' in tg.calls[-1][1][1] and not await db.fetchval('SELECT single_only FROM categories WHERE id=$1',cid)
 assert 'women.pants' in (await automatic_selection(db,value,.85))['selected']
 await handle(db,tg,callback(f'singlecat:{cid}'))
 assert await db.fetchval('SELECT single_only FROM categories WHERE id=$1',cid)
 assert await db.fetchval('SELECT description FROM categories WHERE id=$1',cid)==before['description']
 assert await db.fetchval('SELECT action FROM admin_state WHERE actor=111')=='namecat'
 assert 'Только отдельные вещи (AUTO): ДА' in '\n'.join(await rule_pages(db))

@pytest.mark.asyncio
async def test_single_only_setting_survives_schema_reconnect(db):
 from app.db import connect
 p,value=await setup(db)
 await db.execute("UPDATE categories SET single_only=false WHERE code='women.pants'")
 another=await connect()
 try:assert not await another.fetchval("SELECT single_only FROM categories WHERE code='women.pants'")
 finally:await another.close()

@pytest.mark.parametrize('unit',[None,'bundle',True,1])
def test_invalid_sale_unit_cannot_pass_provider_validation(unit):
 with pytest.raises(ValueError):parse_result('google',json.dumps(dict(RESULT,sale_unit=unit)))


def test_google_flat_schema_keeps_required_sale_unit_and_telegram_core_fits():
 wire=google_schema(Result.model_json_schema())
 assert 'sale_unit' in wire['required'] and set(wire['properties']['sale_unit']['enum'])=={'single','set','unknown'}
 assert all(p.get('type')!='array' for p in wire['properties'].values())
 assert len('Действующие общие инструкции AI (всегда применяются):\n\n'+CORE_AI_RULES)<4000
