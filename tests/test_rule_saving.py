"""Regression for real admin rich-message inputs; isolated database only."""
import json
from pathlib import Path
import pytest
from test_pipeline import db, Gateway, callback
from app.admin import handle
from app.db import put, setting

INPUTS = json.loads(Path(__file__).with_name('rich_rule_inputs.json').read_text())

def message(**content):
    return {'message': {'message_id':321,'from':{'id':111},'chat':{'id':111,'type':'private'},**content}}

def expected(rich):
    # Independent fixture oracle: all actual messages contain paragraphs and lists.
    def blocks(xs):
        return '\n'.join(b['text'] if b['type']=='paragraph' else '\n'.join(i['label']+' '+blocks(i['blocks']) for i in b['items']) for b in xs)
    return blocks(rich['blocks']).strip()

@pytest.mark.asyncio
@pytest.mark.parametrize('update_id', list(INPUTS))
async def test_real_formatted_rule_survives_save_and_reopen(db, update_id):
    await put(db,'admins',[111]);tg=Gateway()
    cid=await db.fetchval("INSERT INTO categories(code,title,department,description) VALUES('men.shirt','Мужские рубашки','men','Старое правило') RETURNING id")
    await handle(db,tg,callback(f'desccat:{cid}'))
    await handle(db,tg,message(rich_message=INPUTS[update_id]))
    value=await db.fetchval('SELECT description FROM categories WHERE id=$1',cid)
    assert value == expected(INPUTS[update_id])
    assert not await db.fetchval('SELECT count(*) FROM admin_state')
    assert 'Мужские рубашки' in tg.calls[-1][1][1] and value in tg.calls[-1][1][1]
    await handle(db,tg,callback(f'desccat:{cid}'))
    assert value in tg.calls[-1][1][1] and 'Не задано' not in tg.calls[-1][1][1]

@pytest.mark.asyncio
@pytest.mark.parametrize('content',[
    {}, {'voice':{'file_id':'x'}}, {'text':'  \n  '},
    {'rich_message':{'blocks':[{'type':'paragraph','text':'Начало'},{'type':'unknown','text':'Конец'}]}},
    {'rich_message':{'blocks':[{'type':'list','items':None}]}},
    {'rich_message':{'blocks':[{'type':'paragraph','text':''}]}},
    {'text':'я'*1501},
])
async def test_invalid_or_empty_input_cannot_erase_rule_or_report_saved(db, content):
    await put(db,'admins',[111]);tg=Gateway()
    cid=await db.fetchval("INSERT INTO categories(code,title,department,description) VALUES('men.shirt','Рубашки','men','Прежнее правило') RETURNING id")
    await handle(db,tg,callback(f'desccat:{cid}'));tg.calls.clear()
    with pytest.raises(ValueError):await handle(db,tg,message(**content))
    assert await db.fetchval('SELECT description FROM categories WHERE id=$1',cid)=='Прежнее правило'
    assert await db.fetchval('SELECT action FROM admin_state WHERE actor=111')=='desccat'
    assert not any(method=='say' and 'Сохранено' in args[1] for method,args in tg.calls)

@pytest.mark.asyncio
async def test_missing_category_never_claims_save(db):
    await put(db,'admins',[111]);tg=Gateway()
    with pytest.raises(ValueError):await handle(db,tg,callback('desccat:99999'))
    assert not await db.fetchval('SELECT count(*) FROM admin_state')
    await db.execute("INSERT INTO admin_state VALUES(111,'desccat','{\"id\":99999}')")
    with pytest.raises(ValueError):await handle(db,tg,message(text='Правило'))
    assert not any(method=='say' and 'Сохранено' in args[1] for method,args in tg.calls)

@pytest.mark.asyncio
async def test_formatted_global_rule_does_not_erase_category_rules(db):
    await put(db,'admins',[111]);tg=Gateway()
    await db.execute("INSERT INTO categories(code,title,department,description) VALUES('men.shirt','Рубашки','men','Прежнее правило')")
    await handle(db,tg,callback('airulesedit'))
    await handle(db,tg,message(rich_message=INPUTS['948219147']))
    assert await setting(db,'ai_instructions')==expected(INPUTS['948219147'])
    await handle(db,tg,callback('airulesedit'))
    with pytest.raises(ValueError):await handle(db,tg,message(voice={'file_id':'x'}))
    assert await setting(db,'ai_instructions')==expected(INPUTS['948219147'])
    assert await db.fetchval('SELECT description FROM categories')=='Прежнее правило'

@pytest.mark.asyncio
@pytest.mark.parametrize('secret,valid', [('1234567890abcdef1234567890abcdef',True),('bad.secret',False)])
async def test_formatted_key_is_scrubbed_including_invalid_inputs(db,secret,valid):
    await put(db,'admins',[111]);tg=Gateway();await handle(db,tg,callback('config:mistral_key'))
    u=message(rich_message={'blocks':[{'type':'paragraph','text':secret}]},caption=secret)
    u['update_id']=1000
    await db.execute('INSERT INTO updates(id,payload) VALUES($1,$2)',1000,u)
    if valid:await handle(db,tg,u)
    else:
        with pytest.raises(ValueError):await handle(db,tg,u)
    stored=await db.fetchval('SELECT payload FROM updates WHERE id=1000')
    assert secret not in json.dumps(stored)
    assert stored['message']['text']=='[API key removed]'
    assert 'rich_message' not in stored['message'] and 'caption' not in stored['message']
    if valid:assert await setting(db,'mistral_key')==secret

@pytest.mark.asyncio
async def test_saved_formatted_rule_is_in_the_actual_ai_request(db,monkeypatch):
    from test_providers import setup, transport, success, MediaGateway
    from app.ai import classify
    p=await setup(db);await put(db,'admins',[111]);tg=Gateway()
    cid=await db.fetchval("SELECT id FROM categories WHERE code='women.dress'")
    await handle(db,tg,callback(f'desccat:{cid}'))
    rule=expected(INPUTS['948219147'])
    await handle(db,tg,message(rich_message=INPUTS['948219147']))
    requests=[]
    def response(request):
        body=json.loads(request.content)
        prompt=body['contents'][0]['parts'][0]['text']
        assert rule in prompt and rule.splitlines()[-1] in prompt
        requests.append(request.url.host)
        return success('google')
    transport(monkeypatch,response)
    await classify(db,MediaGateway(),p)
    assert requests==['generativelanguage.googleapis.com']


def test_plain_caption_and_nested_formatted_text():
    from app.admin_text import message_text
    assert message_text({'text':'  Текст\nправила  '})=='Текст\nправила'
    assert message_text({'caption':'  Правило из подписи  '})=='Правило из подписи'
    assert message_text({'rich_message':{'blocks':[
        {'type':'heading','text':[{'type':'bold','text':'Заголовок'}]},
        {'type':'list','items':[{'label':'•','blocks':[{'type':'paragraph','text':['Текст ',{'type':'italic','text':'целиком'}]}]}]},
        {'type':'blockquote','blocks':[{'type':'paragraph','text':'Исключение'}],'credit':'Автор'},
    ]}})=='Заголовок\n• Текст целиком\nИсключение\nАвтор'
