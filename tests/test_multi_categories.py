import asyncio,json
import pytest
from test_pipeline import db,Gateway,callback
from test_providers import setup,transport,success,MediaGateway,RESULT
from app.db import put,setting
from app.ai import classify
from app.pipeline import worker,publish
from app.admin import handle,item
from app.operations import queue_manual_many,cached_count,resume_cached
from app.deliveries import recover

async def prepared(db):
 p=await setup(db)
 await put(db,'admins',[111,222]);await put(db,'mode','AUTO');await put(db,'paused',False)
 second=await db.fetchval("INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.modest','Исламская одежда','Закрытая одежда по правилам','women',-2002,true) RETURNING id")
 third=await db.fetchval("INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.maternity','Для беременных','Явные признаки','women',-2003,true) RETURNING id")
 first=await db.fetchval("SELECT id FROM categories WHERE code='women.dress'")
 return p,[first,second,third]

def result(extra_conf=.94):return dict(RESULT,additional_categories=[{'category':'women.modest','confidence':extra_conf,'reason':'Закрытый фасон'}])
def copies(tg):return [kw for method,kw in tg.calls if method=='copy_messages']
def has_button(tg,data):return any(b['callback_data']==data for row in tg.calls[-1][1][2] for b in row)

async def run_until_decided(db,tg):
 task=asyncio.create_task(worker(db,tg))
 try:
  for _ in range(160):
   await asyncio.sleep(.05)
   p=await db.fetchrow('SELECT * FROM products')
   if p['status'] in ('published','review','low_confidence','uncertain'):return p
  raise AssertionError('Worker did not finish')
 finally:
  task.cancel()
  with pytest.raises(asyncio.CancelledError):await task

@pytest.mark.asyncio
async def test_ai_two_categories_copies_whole_product_and_restart_cannot_duplicate(db,monkeypatch):
 p,ids=await prepared(db);tg=MediaGateway()
 def response(r):
  schema=json.loads(r.content)['generationConfig']['responseJsonSchema']
  assert 'additional_categories_json' in schema['required']
  assert schema['properties']['additional_categories_json']['type']=='string' and '$defs' not in schema
  assert 'women.modest' in json.loads(r.content)['contents'][0]['parts'][0]['text']
  assert len(json.loads(r.content)['contents'][0]['parts'])==3
  return success('google',result())
 transport(monkeypatch,response)
 p=await run_until_decided(db,tg)
 assert p['status']=='published' and p['final_categories']==['women.dress','women.modest']
 assert p['confidence']==.95
 assert sorted(kw['chat_id'] for kw in copies(tg))==[-2002,-2001]
 assert all(kw['message_ids']==[1,2,3] for kw in copies(tg))
 ds=await db.fetch('SELECT * FROM product_deliveries ORDER BY id')
 assert ds[1]['rule_evidence']['confidence']==.94 and ds[1]['rule_evidence']['reason']=='Закрытый фасон'
 await recover(db,True);await publish(db,tg,p)
 assert len(copies(tg))==2

@pytest.mark.asyncio
async def test_uncertain_additional_category_does_not_block_primary(db,monkeypatch):
 p,ids=await prepared(db);tg=MediaGateway()
 transport(monkeypatch,lambda r:success('google',result(.78)))
 p=await run_until_decided(db,tg)
 assert p['status']=='published' and p['confidence']==.95 and p['final_categories']==['women.dress']
 assert [kw['chat_id'] for kw in copies(tg)]==[-2001]
 assert p['ai_result']['additional_categories']==result(.78)['additional_categories']
 await item(db,tg,111,p['id'])
 assert '78%' in tg.calls[-1][1][1] and 'не блокируют' in tg.calls[-1][1][1]

@pytest.mark.asyncio
async def test_unavailable_optional_category_does_not_block_primary(db,monkeypatch):
 p,ids=await prepared(db);await db.execute('UPDATE categories SET enabled=false WHERE id=$1',ids[1])
 tg=MediaGateway();transport(monkeypatch,lambda r:success('google',result()))
 p=await run_until_decided(db,tg)
 assert p['status']=='published' and p['final_categories']==['women.dress']
 assert [kw['chat_id'] for kw in copies(tg)]==[-2001]
 assert p['usage']['routing']['omitted'][0]['cause']=='channel_unavailable'

@pytest.mark.asyncio
async def test_multiselect_ui_saves_draft_and_sends_all_selected_only_on_confirm(db):
 p,ids=await prepared(db);pid=p['id'];await put(db,'paused',True)
 await db.execute("UPDATE products SET status='review',final_category='women.dress'")
 tg=Gateway();await item(db,tg,111,pid)
 assert has_button(tg,f'multiroute:{pid}')
 await db.execute("INSERT INTO admin_state VALUES(111,'desccat','{\"id\":1}')")
 await handle(db,tg,callback(f'multiroute:{pid}'))
 for cid in ids[1:]:await handle(db,tg,callback(f'multitoggle:{pid}:{cid}:all:0'))
 assert not copies(tg) and await db.fetchval('SELECT status FROM products')=='review'
 assert await db.fetchval('SELECT category_ids FROM route_drafts WHERE actor=111')==ids
 assert await db.fetchval('SELECT action FROM admin_state WHERE actor=111')=='desccat'
 # Reopening reads a durable draft, including after a fresh DB acquisition.
 await handle(db,tg,callback(f'multiroute:{pid}'));assert has_button(tg,f'multisend:{pid}')
 for title in ('Dress','Исламская одежда','Для беременных'):assert '✓ '+title in tg.calls[-1][1][1]
 await handle(db,tg,callback(f'multisend:{pid}'))
 p=await db.fetchrow('SELECT * FROM products');assert p['final_categories']==['women.dress','women.modest','women.maternity']
 await publish(db,tg,p);assert sorted(kw['chat_id'] for kw in copies(tg))==[-2003,-2002,-2001]
 with pytest.raises(ValueError):await handle(db,tg,callback(f'multisend:{pid}'))
 await publish(db,tg,p);assert len(copies(tg))==3
 assert await db.fetchval('SELECT count(*) FROM route_drafts')==0

@pytest.mark.asyncio
async def test_empty_draft_and_disabled_target_never_partially_queue(db):
 p,ids=await prepared(db);pid=p['id'];await db.execute("UPDATE products SET status='review',final_category='women.dress'")
 tg=Gateway();await handle(db,tg,callback(f'multiroute:{pid}'))
 await handle(db,tg,callback(f'multitoggle:{pid}:{ids[0]}:all:0'))
 assert not has_button(tg,f'multisend:{pid}')
 with pytest.raises(ValueError):await handle(db,tg,callback(f'multisend:{pid}'))
 assert await db.fetchval('SELECT status FROM products')=='review'
 for cid in ids[:2]:await handle(db,tg,callback(f'multitoggle:{pid}:{cid}:all:0'))
 await db.execute('UPDATE categories SET enabled=false WHERE id=$1',ids[1])
 with pytest.raises(ValueError):await handle(db,tg,callback(f'multisend:{pid}'))
 assert await db.fetchval('SELECT status FROM products')=='review' and not copies(tg)

@pytest.mark.asyncio
async def test_drafts_are_separate_per_admin_and_pages_keep_choices(db):
 p,ids=await prepared(db);pid=p['id'];await db.execute("UPDATE products SET status='review',final_category='women.dress'")
 tg=Gateway();await handle(db,tg,callback(f'multitoggle:{pid}:{ids[1]}:all:0'))
 other=callback(f'multiroute:{pid}');other['callback_query']['from']['id']=222;other['callback_query']['message']['chat']['id']=222
 await handle(db,tg,other)
 assert await db.fetchval('SELECT category_ids FROM route_drafts WHERE actor=111')==ids[:2]
 assert await db.fetchval('SELECT category_ids FROM route_drafts WHERE actor=222')==ids[:1]
 for n in range(24):await db.execute('INSERT INTO categories(code,title,description,department,destination,enabled) VALUES($1,$2,$2,$3,$4,true)',f'men.test{n}',f'Extra {n}','men',-3000-n)
 await handle(db,tg,callback(f'multiroute:{pid}:all:20'))
 assert '✓ Исламская одежда' in tg.calls[-1][1][1]
 assert await db.fetchval('SELECT category_ids FROM route_drafts WHERE actor=111')==ids[:2]

@pytest.mark.asyncio
async def test_multiple_categories_same_channel_and_plus_size_do_not_duplicate(db):
 p,ids=await prepared(db);pid=p['id']
 await db.execute("UPDATE products SET status='review',final_category='women.dress',messages=jsonb_set(messages,'{2,text}',to_jsonb('Размеры: 44–50'::text))")
 await db.execute('UPDATE categories SET destination=-2001 WHERE id=$1',ids[1])
 await db.execute("INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.plus_size','Plus size','Plus','women',-2004,true)")
 await queue_manual_many(db,pid,ids,111);tg=Gateway();await publish(db,tg,await db.fetchrow('SELECT * FROM products'))
 assert sorted(kw['chat_id'] for kw in copies(tg))==[-2004,-2003,-2001]
 assert await db.fetchval('SELECT count(*) FROM product_deliveries')==3

@pytest.mark.asyncio
async def test_single_manual_choice_replaces_ai_multiple_categories(db):
 p,ids=await prepared(db)
 await db.execute("UPDATE products SET status='review',final_category='women.dress',final_categories='[\"women.dress\",\"women.modest\"]'")
 tg=Gateway();await handle(db,tg,callback(f"route:{p['id']}:{ids[2]}"));await publish(db,tg,p)
 assert await db.fetchval('SELECT final_categories FROM products')==['women.maternity']
 assert [kw['chat_id'] for kw in copies(tg)]==[-2003]

@pytest.mark.asyncio
@pytest.mark.parametrize('extras',[
 [{'category':'women.dress','confidence':.95,'reason':'duplicate primary'}],
 [{'category':'women.modest','confidence':.95,'reason':'a'},{'category':'women.modest','confidence':.95,'reason':'b'}],
 [{'category':'men.shirt','confidence':.95,'reason':'wrong department'}],
 [{'category':'women.plus_size','confidence':.95,'reason':'must be deterministic'}],
])
async def test_invalid_ai_routes_fallback_instead_of_sending(db,monkeypatch,extras):
 p,ids=await prepared(db)
 await db.execute("INSERT INTO categories(code,title,description,department) VALUES('men.shirt','Мужские рубашки','Рубашки','men')")
 calls=[]
 def response(r):
  calls.append(r.url.host)
  return success('google',dict(RESULT,additional_categories=extras)) if len(calls)==1 else success('mistral',result())
 transport(monkeypatch,response)
 value,usage,_=await classify(db,MediaGateway(),p)
 assert usage['provider']=='mistral' and value['additional_categories']==result()['additional_categories']
 assert len(calls)==2

@pytest.mark.asyncio
async def test_google_flat_transport_preserves_multi_category_confidence_and_whole_album(db,monkeypatch):
 p,ids=await prepared(db);tg=MediaGateway()
 def response(request):
  body=json.loads(request.content);schema=body['generationConfig']['responseJsonSchema']
  assert 'additional_categories_json' in schema['required'] and 'additional_categories' not in schema['properties']
  assert all(prop.get('type')!='array' for prop in schema['properties'].values())
  assert len(body['contents'][0]['parts'])==3
  prompt=body['contents'][0]['parts'][0]['text']
  marker='\nReturn ONLY a JSON object matching this schema, no Markdown:\n'
  assert json.loads(prompt.rsplit(marker,1)[1])==schema
  value=result();value['additional_categories_json']=json.dumps(value.pop('additional_categories'),ensure_ascii=False)
  return success('google',value)
 transport(monkeypatch,response)
 p=await run_until_decided(db,tg)
 assert p['status']=='published' and p['final_categories']==['women.dress','women.modest']
 assert p['ai_result']['additional_categories']==result()['additional_categories']
 assert 'additional_categories_json' not in p['ai_result'] and p['confidence']==.95
 assert sorted(kw['chat_id'] for kw in copies(tg))==[-2002,-2001]

@pytest.mark.asyncio
@pytest.mark.parametrize('encoded',[
 'not json', '{}', '[{"category":"women.modest","confidence":1.5,"reason":"bad"}]',
 '[{"category":"unknown","confidence":0.95,"reason":"bad"}]',
 [{'category':'women.modest','confidence':.95,'reason':'not encoded'}],
])
async def test_google_encoded_transport_does_not_weaken_validation(db,monkeypatch,encoded):
 p,ids=await prepared(db);calls=[]
 def response(request):
  calls.append(request.url.host)
  if len(calls)==1:
   value=dict(RESULT);value.pop('additional_categories');value['additional_categories_json']=encoded
   return success('google',value)
  return success('mistral',result())
 transport(monkeypatch,response)
 value,usage,_=await classify(db,MediaGateway(),p)
 assert usage['provider']=='mistral' and value['additional_categories']==result()['additional_categories']
 assert len(calls)==2


def test_google_schema_does_not_mutate_native_provider_contract():
 from app.ai import Result,google_schema,parse_result
 schema=Result.model_json_schema();before=json.loads(json.dumps(schema))
 wire=google_schema(schema)
 assert schema==before and 'additional_categories' in schema['required']
 assert 'additional_categories_json' in wire['required']
 value=dict(RESULT);value['additional_categories_json']='[]'
 with pytest.raises(ValueError):parse_result('google',json.dumps(value))
