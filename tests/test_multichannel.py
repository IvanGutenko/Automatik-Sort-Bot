import asyncio
import pytest
from test_pipeline import db,Gateway,queued,msg
from app.db import put
from app.routing import plus_evidence
from app.pipeline import publish,finish_publication,worker
from app.deliveries import decide,recover
from app.admin import handle
from aiogram.exceptions import TelegramForbiddenError,TelegramRetryAfter
from aiogram.methods import CopyMessages


@pytest.mark.parametrize('text,matched',[
 ('Размеры / Sizes: 46, 48, 50, 52',True),
 ('Размеры: 44–56',True),
 ('размеры: 40',False),
 ('Размеры: XXL, 3XL',True),
 ('Размеры: XL',False),
 ('PLUS SIZE платье',True),
 ('Платье большие размеры',True),
 ('Оверсайз футболка',False),
 ('Код: AA505254\nЦена: 54\nШт: 50',False),
 ('Размеры: EU 50',False),
 ('Размеры: TR 50',False),
 ('Размеры: EU 44 / RU 50',True),
 ('Размеры: 40\nРост: 54 см\nЦена 56',False),
 ('Размеры: 54 см',False),
 ('Размеры / Sizes: Уточнить / Check',False),
 ('Размеры: 40,42,44,46,48,50',False),
 ('Размеры: 38–44',False),
 ('Размеры: 50, 48, 40',False),
 ('Размеры: S M L XL XXL',False),
 ('Размеры: XXL–4XL',True),
 ('Размеры: XS–2XL',False),
 ('Размеры: 42–50',True),
 ('Размеры: 42 44 / S M',False),
 ('Не plus size. Размеры: S–XXL',False),
 ('Размеры: 40; цена 54',False),
 ('Размеры: 40\\nЦена: 52',False),
])
def test_size_evidence(text,matched):
 assert plus_evidence(text,{'threshold':42,'letters':True})['matched'] is matched


def cb(data):
 return {'callback_query':{'id':'x','from':{'id':111},'data':data,'message':{'chat':{'id':111,'type':'private'}}}}

def message(text):
 return {'message':{'message_id':123,'from':{'id':111},'chat':{'id':111,'type':'private'},'text':text}}

async def multi(db):
 p=await queued(db)
 text=msg(3,None,text='Код: Q123\nРазмеры / Sizes: 50, 52, 54\nЦена: $25')
 messages=p['messages']+[text]
 await db.execute('UPDATE products SET messages=$1,closed=true',messages)
 await db.execute("INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.plus_size','Большие размеры','Plus size','women',-2002,true)")
 await put(db,'admins',[111])
 return await db.fetchrow('SELECT * FROM products')


def copies(tg):return [kw['chat_id'] for method,kw in tg.calls if method=='copy_messages']

@pytest.mark.asyncio
async def test_entire_product_to_two_channels_no_duplicates(db):
 p=await multi(db);tg=Gateway()
 await asyncio.gather(publish(db,tg,p),publish(db,tg,p))
 await publish(db,tg,p)
 assert sorted(copies(tg))==[-2002,-2001]
 ds=await db.fetch('SELECT * FROM product_deliveries ORDER BY id')
 assert len(ds)==2 and all(d['status']=='published' for d in ds)
 assert all(d['source_message_ids']==[1,2,3] for d in ds)
 assert ds[1]['rule_evidence']['sizes']==[50,52,54]
 assert await db.fetchval('SELECT status FROM products')=='published'

@pytest.mark.asyncio
async def test_same_channel_is_copied_once(db):
 p=await multi(db);await db.execute("UPDATE categories SET destination=-2001 WHERE code='women.plus_size'")
 tg=Gateway();await publish(db,tg,p)
 assert copies(tg)==[-2001]
 assert await db.fetchval('SELECT count(*) FROM product_deliveries')==1

@pytest.mark.asyncio
async def test_failed_extra_retry_does_not_repeat_primary(db):
 p=await multi(db)
 class FailExtra(Gateway):
  fail=True
  async def call(self,method,**kw):
   if method=='copy_messages' and kw['chat_id']==-2002 and self.fail:
    self.fail=False;self.calls.append((method,kw));raise TelegramForbiddenError(method=CopyMessages(**kw),message='bot removed')
   return await super().call(method,**kw)
 await db.execute('UPDATE products SET review_message_id=999')
 tg=FailExtra();await publish(db,tg,p)
 assert await db.fetchval('SELECT status FROM products')=='review'
 assert await db.fetchval("SELECT status FROM product_deliveries WHERE kind='primary'")=='published'
 assert await db.fetchval('SELECT review_message_id FROM products') is None
 did=await db.fetchval("SELECT id FROM product_deliveries WHERE kind='extra'")
 await handle(db,tg,cb(f'retrydelivery:{p["id"]}:{did}'))
 await publish(db,tg,p)
 assert copies(tg)==[-2001,-2002,-2002]
 assert await db.fetchval('SELECT decision_actor FROM products')==111
 assert await db.fetchval('SELECT status FROM products')=='published'
 with pytest.raises(ValueError):await handle(db,tg,cb(f'retrydelivery:{p["id"]}:{did}'))

@pytest.mark.asyncio
async def test_uncertain_extra_requires_explicit_absence_before_retry(db):
 p=await multi(db)
 class TimeoutExtra(Gateway):
  fail=True
  async def call(self,method,**kw):
   if method=='copy_messages' and kw['chat_id']==-2002 and self.fail:
    self.fail=False;self.calls.append((method,kw));raise TimeoutError('unknown outcome')
   return await super().call(method,**kw)
 tg=TimeoutExtra();await publish(db,tg,p);await publish(db,tg,p);await recover(db,True)
 assert copies(tg)==[-2001,-2002]
 did=await db.fetchval("SELECT id FROM product_deliveries WHERE kind='extra'")
 with pytest.raises(ValueError):await decide(db,p['id'],did,'retrydelivery',111)
 await handle(db,tg,cb(f'deliveryno:{p["id"]}:{did}'));await publish(db,tg,p)
 assert copies(tg)==[-2001,-2002,-2002]
 assert await db.fetchval('SELECT status FROM products')=='published'

@pytest.mark.asyncio
async def test_extra_button_restore_never_recopies_media(db):
 p=await multi(db);messages=list(p['messages']);messages[-1]['reply_markup']={'inline_keyboard':[[{'text':'Order','url':'https://example.com/order'}]]}
 await db.execute('UPDATE products SET messages=$1',messages)
 class Buttons(Gateway):
  fail=True
  async def call(self,method,**kw):
   if method=='edit_message_reply_markup' and kw['chat_id']==-2002 and self.fail:
    self.fail=False;raise TimeoutError('buttons unavailable')
   return await super().call(method,**kw)
 tg=Buttons();await publish(db,tg,p)
 assert await db.fetchval('SELECT status FROM products')=='finishing'
 await db.execute('UPDATE product_deliveries SET ready_at=now()')
 await finish_publication(db,tg,await db.fetchrow('SELECT * FROM products'))
 assert copies(tg)==[-2001,-2002]
 assert await db.fetchval('SELECT status FROM products')=='published'

@pytest.mark.asyncio
async def test_missing_mandatory_extra_stops_before_any_send(db):
 p=await multi(db);await db.execute("UPDATE categories SET enabled=false WHERE code='women.plus_size'")
 tg=Gateway();await publish(db,tg,p)
 assert not copies(tg)
 assert await db.fetchval('SELECT status FROM products')=='review'
 assert 'Большие размеры' in await db.fetchval('SELECT error FROM products')

@pytest.mark.asyncio
async def test_disabled_rule_and_menswear_do_not_add_womens_channel(db):
 p=await multi(db);await put(db,'plus_size_enabled',False)
 tg=Gateway();await publish(db,tg,p);assert copies(tg)==[-2001]
 await db.execute('DELETE FROM product_deliveries');await db.execute("UPDATE products SET status='queued',destination_message_ids=NULL")
 await put(db,'plus_size_enabled',True);await db.execute("UPDATE categories SET department='men' WHERE code='women.dress'")
 tg=Gateway();await publish(db,tg,p);assert copies(tg)==[-2001]

@pytest.mark.asyncio
async def test_current_category_rule_visible_in_edit_prompt(db):
 await queued(db);await put(db,'admins',[111]);tg=Gateway()
 cid=await db.fetchval('SELECT id FROM categories')
 await db.execute("UPDATE categories SET description='Текущее правило заказчика' WHERE id=$1",cid)
 await handle(db,tg,cb('desccat:'+str(cid)))
 assert any('Текущее правило заказчика' in str(args) for name,args in tg.calls if name=='say')

@pytest.mark.asyncio
async def test_rules_admin_can_view_edit_and_test_without_publication(db):
 await put(db,'admins',[111]);tg=Gateway()
 await handle(db,tg,cb('airulescore'))
 assert any('ВСЕМ' in str(a) for n,a in tg.calls if n=='say')
 await handle(db,tg,cb('airulesedit'));await handle(db,tg,message('Вещь продаётся комплектом только при явных признаках.'))
 from app.db import setting
 assert await setting(db,'ai_instructions')=='Вещь продаётся комплектом только при явных признаках.'
 await handle(db,tg,cb('plusthreshold'));await handle(db,tg,message('52'))
 assert await setting(db,'plus_size_threshold')==52
 await handle(db,tg,cb('plustest'));await handle(db,tg,message('Платье. Размеры: 52–56'))
 assert any('✅ Дополнительная отправка сработает' in str(a) for n,a in tg.calls if n=='say')
 assert not copies(tg)

@pytest.mark.asyncio
async def test_actual_ai_payload_uses_visible_instructions_and_primary_taxonomy(db,monkeypatch):
 from test_providers import setup,transport,success,MediaGateway
 from app.ai import classify
 from app.routing import CORE_AI_RULES
 import json
 p=await setup(db)
 await put(db,'ai_instructions','Не считать пояс самостоятельной сумкой.')
 await db.execute("INSERT INTO categories(code,title,description,department) VALUES('women.plus_size','Plus size','Old exclusive rule','women')")
 def handler(request):
  body=json.loads(request.content);prompt=body['contents'][0]['parts'][0]['text']
  assert CORE_AI_RULES in prompt and 'Не считать пояс самостоятельной сумкой.' in prompt
  assert 'Old exclusive rule' not in prompt
  assert 'women.plus_size' not in body['generationConfig']['responseJsonSchema']['properties']['category']['enum']
  assert len(body['contents'][0]['parts'])==3
  return success('google')
 transport(monkeypatch,handler)
 _,usage,_=await classify(db,MediaGateway(),p)
 assert usage['prompt_version']=='v7-sale-unit' and usage['instructions_sha256']

@pytest.mark.asyncio
async def test_deleted_source_keeps_russian_explanation_and_safe_skip(db):
 from aiogram.exceptions import TelegramBadRequest
 from app.routing_admin import show_delivery
 p=await multi(db)
 class Gone(Gateway):
  async def call(self,method,**kw):
   if method=='copy_messages':
    self.calls.append((method,kw));raise TelegramBadRequest(method=CopyMessages(**kw),message='there are no messages to forward')
   return await super().call(method,**kw)
 tg=Gone();await publish(db,tg,p)
 await show_delivery(db,tg,111,await db.fetchrow('SELECT * FROM products'))
 last=next(a for method,a in reversed(tg.calls) if method=='say')
 assert 'Telegram не нашёл исходные сообщения' in last[1]
 assert not any(b['callback_data'].startswith('retrydelivery') for row in last[2] for b in row)
 await handle(db,tg,cb(f'skip:{p["id"]}'))
 assert await db.fetchval('SELECT status FROM products')=='skipped'
 assert all(r['status']=='skipped' for r in await db.fetch('SELECT status FROM product_deliveries'))
 await publish(db,tg,p);assert copies(tg)==[-2001]
