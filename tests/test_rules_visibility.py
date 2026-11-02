import pytest
from test_pipeline import db,Gateway,callback
from app.admin import handle
from app.db import put,setting
from app.routing_admin import rule_pages,all_rules
from app.routing import CORE_AI_RULES


def msg(text):return {'message':{'message_id':321,'from':{'id':111},'chat':{'id':111,'type':'private'},'text':text}}

@pytest.mark.asyncio
async def test_every_existing_rule_is_visible_without_telegram_truncation(db):
 await put(db,'ai_instructions','Дополнительное общее правило '+('я'*1400))
 await put(db,'gemini_key','secret-must-not-be-shown')
 descriptions=[]
 for n in range(30):
  text=f'Исходное правило {n}: '+str(n)*1300;descriptions.append(text)
  await db.execute('INSERT INTO categories(code,title,department,description) VALUES($1,$2,$3,$4)',f'women.test{n}',f'Категория {n}','women',text)
 pages=await rule_pages(db);joined='\n\n'.join(pages)
 assert CORE_AI_RULES in joined and all(text in joined for text in descriptions)
 assert await setting(db,'ai_instructions') in joined
 assert 'secret-must-not-be-shown' not in joined
 tg=Gateway()
 for i in range(len(pages)):await all_rules(db,tg,111,i)
 rendered=[args[1] for method,args in tg.calls if method=='say']
 assert len(rendered)==len(pages) and all(len(text)<4000 for text in rendered)
 assert await db.fetchval('SELECT count(*) FROM categories')==30

@pytest.mark.asyncio
async def test_rules_command_does_not_overwrite_a_pending_admin_edit(db):
 await put(db,'admins',[111]);await db.execute("INSERT INTO admin_state VALUES(111,'config','{\"key\":\"gemini_key\"}')")
 tg=Gateway();await handle(db,tg,msg('/rules'))
 state=await db.fetchrow('SELECT * FROM admin_state WHERE actor=111')
 assert state['action']=='config' and state['data']=={'key':'gemini_key'}
 assert 'Все заданные правила AI' in tg.calls[-1][1][1]
 assert not any(name=='delete_message' for name,args in tg.calls)
 await handle(db,tg,callback('airulesall:0'))
 assert await db.fetchval('SELECT action FROM admin_state WHERE actor=111')=='config'

@pytest.mark.asyncio
async def test_replacing_a_rule_keeps_other_rules_and_does_not_accumulate_duplicates(db):
 await put(db,'admins',[111]);await put(db,'ai_instructions','Старое общее правило')
 cid=await db.fetchval("INSERT INTO categories(code,title,department,description) VALUES('women.dress','Платья','women','Старое правило платья') RETURNING id")
 await db.execute("INSERT INTO categories(code,title,department,description) VALUES('women.pants','Брюки','women','Правило брюк')")
 tg=Gateway()
 for _ in range(2):
  await handle(db,tg,callback(f'desccat:{cid}'))
  prompt=tg.calls[-1][1][1];assert 'ЗАМЕНИТ только правило этой категории' in prompt
  await handle(db,tg,msg('Новое правило платья'))
 assert await db.fetchval("SELECT description FROM categories WHERE code='women.dress'")=='Новое правило платья'
 assert await db.fetchval("SELECT description FROM categories WHERE code='women.pants'")=='Правило брюк'
 assert await setting(db,'ai_instructions')=='Старое общее правило'
 await handle(db,tg,callback('airulesedit'));assert 'ЗАМЕНИТ только общие пожелания' in tg.calls[-1][1][1]
 await handle(db,tg,msg('Новое общее правило'))
 assert await setting(db,'ai_instructions')=='Новое общее правило'
 assert await db.fetchval("SELECT description FROM categories WHERE code='women.dress'")=='Новое правило платья'

@pytest.mark.asyncio
async def test_rules_are_reachable_from_ai_services_and_empty_custom_field_is_explained(db):
 await put(db,'admins',[111]);tg=Gateway()
 await handle(db,tg,callback('aiproviders'))
 assert any(b['callback_data']=='airulesall:0' for row in tg.calls[-1][1][2] for b in row)
 await handle(db,tg,callback('airules'))
 text=tg.calls[-1][1][1]
 assert 'Общие инструкции и правила категорий уже действуют' in text
 assert any(b['callback_data']=='airulesall:0' for row in tg.calls[-1][1][2] for b in row)
