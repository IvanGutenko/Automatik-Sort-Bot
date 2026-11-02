"""Contract tests for provider failures and complete-product delivery, not accuracy."""
import asyncio,io,json,time
import httpx,pytest
from PIL import Image
from app.db import put,setting
from app.ai import classify
from app.providers import AIUnavailable,configured
from app.pipeline import worker
from app.admin import handle
from test_pipeline import db,Gateway,callback,msg

RESULT={'department':'women','category':'women.dress','subcategory':'dress','confidence':.95,'reason':'Платье',
        'product_code':'AA123','price':'20','sizes':'S M','ocr_text':'','mixed_products':False,'sale_unit':'single','additional_categories':[]}
CLIENT=httpx.AsyncClient

class MediaGateway(Gateway):
    def __init__(self):
        super().__init__();out=io.BytesIO();Image.new('RGB',(10,10),'red').save(out,'JPEG');self.image=out.getvalue()
    async def photo(self,m):return self.image if m.get('photo') else None

async def setup(db):
    await db.execute("INSERT INTO categories(code,title,description,department,destination,enabled) VALUES('women.dress','Dress','Dress','women',-2001,true)")
    for m in (msg(1),msg(2),msg(3,None,text='Код AA123, цена 20, S M')):await __import__('app.pipeline',fromlist=['ingest']).ingest(db,m)
    await db.execute("UPDATE products SET ready_at=now()-interval '1 second'")
    for name,key in (('model','gemma-4-26b-a4b-it'),('gemini_key','test-google-key'),('mistral_key','test-mistral-key'),('openrouter_key','test-openrouter-key'),('mistral_enabled',True),('openrouter_enabled',True),('ai_rpm',10000)):
        await put(db,name,key)
    return await db.fetchrow('SELECT * FROM products')

def transport(monkeypatch,handler):
    monkeypatch.setattr('app.ai.httpx.AsyncClient',lambda **kw:CLIENT(transport=httpx.MockTransport(handler),**kw))

def success(provider, result=None):
    text=json.dumps(result or RESULT)
    if provider=='google':return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':text}]}}],'usageMetadata':{'promptTokenCount':40,'candidatesTokenCount':50}})
    return httpx.Response(200,json={'choices':[{'message':{'content':text}}],'usage':{'prompt_tokens':40,'completion_tokens':50,'total_tokens':90}})

@pytest.mark.asyncio
async def test_worker_429_503_fallback_keeps_album_and_copies_only_once(db,monkeypatch):
    p=await setup(db);await put(db,'mode','AUTO');await put(db,'paused',False)
    calls=[]
    def handler(request):
        body=json.loads(request.content);host=request.url.host;calls.append(host)
        if host=='generativelanguage.googleapis.com':
            assert len(body['contents'][0]['parts'])==3
            return httpx.Response(429,json={'error':{'message':'RequestsPerDay quota'}})
        content=body['messages'][0]['content']
        assert sum(x['type']=='image_url' for x in content)==2
        assert 'AA123' in content[0]['text']
        if host=='api.mistral.ai':return httpx.Response(503,json={'error':'unavailable'})
        assert body['model'].endswith(':free') and all(v==0 for v in body['provider']['max_price'].values())
        return success('openrouter')
    transport(monkeypatch,handler);tg=MediaGateway();task=asyncio.create_task(worker(db,tg))
    try:
        for _ in range(100):
            await asyncio.sleep(.05)
            if await db.fetchval('SELECT status FROM products')=='published':break
        row=await db.fetchrow('SELECT * FROM products')
        assert row['status']=='published'
        assert row['usage']['provider']=='openrouter' and row['usage']['image_count']==2
        assert row['estimated_cost_usd']==0
        await asyncio.sleep(1.1)
        assert len([x for x in tg.calls if x[0]=='copy_messages'])==1
        assert await db.fetchval('SELECT count(*) FROM ai_requests')==3
        assert await setting(db,'ai_retry_until')==0
        # A subsequent item skips only the failed providers, keeping fallback available.
        result,usage,_=await classify(db,tg,p)
        assert usage['http_attempts']==1 and calls[-1]=='openrouter.ai'
        assert calls.count('generativelanguage.googleapis.com')==1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task

@pytest.mark.asyncio
async def test_all_providers_429_leave_real_worker_item_for_retry_and_manual_review(db,monkeypatch):
    await setup(db);await put(db,'paused',False)
    await put(db,'nvidia_key','test-nvidia-key');await put(db,'nvidia_enabled',True)
    transport(monkeypatch,lambda r:httpx.Response(429,headers={'Retry-After':'120'},json={'error':'rate limit'}))
    task=asyncio.create_task(worker(db,MediaGateway()))
    try:
        for _ in range(100):
            await asyncio.sleep(.05)
            if await db.fetchval('SELECT count(*) FROM ai_requests')==4 and await db.fetchval('SELECT status FROM products')=='collecting':break
        p=await db.fetchrow('SELECT * FROM products')
        assert p['status']=='collecting' and p['closed'] and p['ai_result'] is None
        assert p['ready_at'].timestamp()<=time.time()
        assert await setting(db,'ai_retry_until')>=time.time()+110
        # Survives a reconnect: cooldown is durable, not a process-local flag.
        assert all(r['retry_at']>time.time() for r in await configured(db))
        assert p['destination_message_ids'] is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task

@pytest.mark.asyncio
async def test_invalid_category_falls_back_but_low_confidence_is_not_reclassified(db,monkeypatch):
    p=await setup(db);calls=[]
    def handler(r):
        calls.append(r.url.host)
        result=dict(RESULT,category='dress') if len(calls)==1 else dict(RESULT,confidence=.2)
        return success('google' if len(calls)==1 else 'mistral',result)
    transport(monkeypatch,handler)
    result,usage,_=await classify(db,MediaGateway(),p)
    assert result['confidence']==.2 and usage['provider']=='mistral' and len(calls)==2
    assert await db.fetchval("SELECT error FROM ai_requests WHERE provider='google'")=='Google Gemma: недопустимый структурированный ответ'
    assert usage['estimated_cost_usd'] is None

@pytest.mark.asyncio
async def test_timeout_falls_back_without_persisting_credentials(db,monkeypatch):
    p=await setup(db)
    def handler(r):
        if r.url.host=='generativelanguage.googleapis.com':raise httpx.ReadTimeout('test-google-key test-mistral-key',request=r)
        return success('mistral')
    transport(monkeypatch,handler)
    _,usage,_=await classify(db,MediaGateway(),p)
    rows=await db.fetch('SELECT error FROM ai_requests')
    assert 'test-google-key' not in str(rows)+str(usage)
    assert usage['provider']=='mistral'

@pytest.mark.asyncio
async def test_paid_or_unknown_models_are_never_requested(db,monkeypatch):
    p=await setup(db)
    for k,v in (('model','gemini-3.1-flash-lite'),('mistral_enabled',False),('openrouter_model','google/paid-model')):await put(db,k,v)
    def handler(r):raise AssertionError('Must not send a paid/unknown model request')
    transport(monkeypatch,handler)
    with pytest.raises(AIUnavailable):await classify(db,MediaGateway(),p)
    assert await db.fetchval('SELECT count(*) FROM ai_requests')==0

@pytest.mark.asyncio
async def test_provider_keys_ui_scrubs_valid_and_invalid_inputs_and_enables_backup(db):
    await put(db,'admins',[111]);tg=Gateway()
    for key,text,valid in [('mistral_key','1234567890abcdef1234567890abcdef',True),('openrouter_key','bad.secret',False)]:
        await handle(db,tg,callback('config:'+key))
        u={'update_id':900 if valid else 901,'message':{'message_id':500,'chat':{'id':111,'type':'private'},'from':{'id':111},'text':text}}
        await db.execute('INSERT INTO updates(id,payload) VALUES($1,$2)',u['update_id'],u)
        if valid:await handle(db,tg,u)
        else:
            with pytest.raises(ValueError):await handle(db,tg,u)
        stored=await db.fetchval('SELECT payload FROM updates WHERE id=$1',u['update_id'])
        assert stored['message']['text']=='[API key removed]'
    assert await setting(db,'mistral_enabled')
    assert all('1234567890abcdef' not in str(args) for method,args in tg.calls if method=='say')
    assert len([x for x in tg.calls if x[0]=='delete_message'])==2

@pytest.mark.asyncio
async def test_ai_controls_show_real_state_reset_only_selected_and_restrict_model_buttons(db):
    await setup(db);await put(db,'admins',[111]);tg=Gateway()
    await db.execute("INSERT INTO ai_provider_state VALUES('google','gemma-4-26b-a4b-it',$1,'429 quota',now()),('mistral','mistral-small-2603',$1,'503',now())",time.time()+100)
    await handle(db,tg,callback('aiproviders'))
    assert 'ожидание до' in tg.calls[-1][1][1]
    await handle(db,tg,callback('airetry:google'))
    assert next(p for p in await configured(db) if p['name']=='google')['retry_at']==0
    assert next(p for p in await configured(db) if p['name']=='mistral')['retry_at']>time.time()
    await handle(db,tg,callback('aiorder:openrouter'))
    assert await setting(db,'ai_order')==['google','openrouter','mistral','nvidia']


def test_real_zero_account_limit_and_shared_upstream_limit_have_different_retries():
    from app.providers import retry_seconds,failure_message
    zero=httpx.Response(429,headers={'x-ratelimit-limit-req-minute':'0'},request=httpx.Request('POST','https://api.mistral.ai/v1/chat/completions'))
    assert retry_seconds(zero,{'message':'Rate limit exceeded'})>=3600
    assert '0 запросов/мин' in failure_message('mistral',zero,{'message':'Rate limit exceeded'})
    busy=httpx.Response(429,request=httpx.Request('POST','https://openrouter.ai/api/v1/chat/completions'))
    assert 'провайдер временно ограничен' in failure_message('openrouter',busy,{'error':{'metadata':{'limit_source':'upstream_provider_shared_pool'}}})


@pytest.mark.asyncio
async def test_dots_free_response_fences_and_expiry_visible_in_admin(db,monkeypatch):
    p=await setup(db)
    for k,v in {'google_enabled':False,'mistral_enabled':False,'openrouter_model':'dots-studio/dots-3-note-preview:free','admins':[111]}.items():await put(db,k,v)
    def handler(r):
        body=json.loads(r.content)
        assert body['model']=='dots-studio/dots-3-note-preview:free'
        assert body['reasoning']['enabled'] is False
        assert body['response_format']['type']=='json_schema'
        assert body['provider']['require_parameters']
        assert sum(p['type']=='image_url' for p in body['messages'][0]['content'])==2
        return httpx.Response(200,json={'choices':[{'message':{'content':'\n\n```json\n'+json.dumps(RESULT)+'\n```'}}],'usage':{'prompt_tokens':100,'completion_tokens':200,'total_tokens':300,'cost':0}})
    transport(monkeypatch,handler)
    result,usage,_=await classify(db,MediaGateway(),p)
    assert result['category']=='women.dress' and usage['raw']['cost']==0
    tg=Gateway();await handle(db,tg,callback('aiprovider:openrouter'))
    assert '31.12.2026' in tg.calls[-1][1][1]


@pytest.mark.asyncio
async def test_nvidia_receives_whole_product_after_google_limit_and_uses_independent_cooldown(db,monkeypatch):
    p=await setup(db)
    for k,v in {'nvidia_key':'test-nvidia-key','nvidia_enabled':True,'ai_order':['google','nvidia','mistral','openrouter']}.items():
        await put(db,k,v)
    calls=[]
    def handler(r):
        calls.append(r.url.host)
        if r.url.host=='generativelanguage.googleapis.com':
            return httpx.Response(429,headers={'Retry-After':'120'},json={'error':'daily quota'})
        assert r.url.host=='integrate.api.nvidia.com'
        assert r.headers['Authorization']=='Bearer test-nvidia-key'
        body=json.loads(r.content)
        assert body['model']=='nvidia/nemotron-3.5-super-vl-120b-a12b'
        assert body['chat_template_kwargs']['enable_thinking'] is False
        native=body['response_format']['json_schema']
        assert body['response_format']['type']=='json_schema' and native['strict'] is True
        assert native['schema']['additionalProperties'] is False
        assert 'women.dress' in native['schema']['properties']['category']['enum']
        assert 'provider' not in body
        content=body['messages'][0]['content']
        assert 'AA123' in content[0]['text']
        assert len([x for x in content if x['type']=='image_url' and x['image_url']['url'].startswith('data:image/jpeg;base64,')])==2
        return success('nvidia')
    transport(monkeypatch,handler)
    _,usage,_=await classify(db,MediaGateway(),p)
    assert usage['provider']=='nvidia' and usage['image_count']==2 and usage['http_attempts']==2
    _,usage,_=await classify(db,MediaGateway(),p)
    assert usage['provider']=='nvidia' and usage['http_attempts']==1
    assert calls==['generativelanguage.googleapis.com','integrate.api.nvidia.com','integrate.api.nvidia.com']


@pytest.mark.asyncio
async def test_worker_nvidia_limit_falls_back_and_persists_result_without_duplicate_copy(db,monkeypatch):
    await setup(db)
    for k,v in {'mode':'AUTO','paused':False,'nvidia_key':'test-nvidia-key','nvidia_enabled':True,
                'ai_order':['nvidia','mistral','google','openrouter']}.items():await put(db,k,v)
    def handler(r):
        if r.url.host=='integrate.api.nvidia.com':
            return httpx.Response(429,headers={'Retry-After':'180'},json={'error':'rate limit'})
        assert r.url.host=='api.mistral.ai'
        return success('mistral')
    transport(monkeypatch,handler);tg=MediaGateway();task=asyncio.create_task(worker(db,tg))
    try:
        for _ in range(120):
            await asyncio.sleep(.05)
            if await db.fetchval('SELECT status FROM products')=='published':break
        p=await db.fetchrow('SELECT * FROM products')
        assert p['status']=='published' and p['ai_result']['category']=='women.dress'
        assert p['usage']['provider']=='mistral' and p['usage']['image_count']==2
        assert p['usage']['attempts'][0]['provider']=='nvidia' and p['usage']['attempts'][0]['status_code']==429
        assert await db.fetchval("SELECT retry_at FROM ai_provider_state WHERE provider='nvidia'")>time.time()+170
        await asyncio.sleep(1.1)
        assert len([x for x in tg.calls if x[0]=='copy_messages'])==1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task


def test_provider_order_upgrade_preserves_saved_order_and_removes_duplicates():
    from app.providers import normalize_order
    assert normalize_order(['openrouter','google','mistral'])==['openrouter','google','mistral','nvidia']
    assert normalize_order(['nvidia','google','nvidia','retired'])==['nvidia','google','mistral','openrouter']
    assert normalize_order('invalid')==['google','mistral','openrouter','nvidia']


@pytest.mark.asyncio
async def test_nvidia_admin_key_models_and_reordering(db):
    await put(db,'admins',[111]);tg=Gateway()
    await handle(db,tg,callback('config:nvidia_key'))
    key='nvapi-'+('a'*64)
    u={'update_id':991,'message':{'message_id':501,'chat':{'id':111,'type':'private'},'from':{'id':111},'text':key}}
    await db.execute('INSERT INTO updates(id,payload) VALUES($1,$2)',u['update_id'],u)
    await handle(db,tg,u)
    assert await setting(db,'nvidia_enabled') and await setting(db,'nvidia_key')==key
    assert (await db.fetchval('SELECT payload FROM updates WHERE id=991'))['message']['text']=='[API key removed]'
    assert key not in str(tg.calls)
    await handle(db,tg,callback('aimodel:nvidia:1'))
    from app.providers import MODELS
    assert await setting(db,'nvidia_model')==MODELS['nvidia'][1]
    await handle(db,tg,callback('aiorder:up:nvidia'))
    assert await setting(db,'ai_order')==['google','mistral','nvidia','openrouter']
    await handle(db,tg,callback('aiorder:up:nvidia'))
    assert await setting(db,'ai_order')==['google','nvidia','mistral','openrouter']
    await handle(db,tg,callback('aitoggle:nvidia'))
    assert not await setting(db,'nvidia_enabled')
    await handle(db,tg,callback('aiclearkey:nvidia'))
    assert await setting(db,'nvidia_key')==''
