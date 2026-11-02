"""Configured providers, independent durable cooldowns and free model restrictions."""
import os, time, json
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from .db import setting, put

PROVIDERS = ('google', 'mistral', 'openrouter', 'nvidia')
NAMES = {'google':'Google Gemma', 'mistral':'Mistral', 'openrouter':'OpenRouter', 'nvidia':'NVIDIA'}
MODELS = {
 'google': ('gemma-4-26b-a4b-it','gemma-4-31b-it'),
 'mistral': ('mistral-small-2603','ministral-14b-2512','ministral-8b-2512','ministral-3b-2512'),
 'nvidia': ('nvidia/nemotron-3.5-super-vl-120b-a12b','nvidia/nemotron-3-nano-omni-30b-a3b-reasoning'),
 'openrouter': ('google/gemma-4-26b-a4b-it:free','google/gemma-4-31b-it:free','dots-studio/dots-3-note-preview:free'),
}
KEYS={'google':'gemini_key','mistral':'mistral_key','openrouter':'openrouter_key','nvidia':'nvidia_key'}

class AIUnavailable(RuntimeError):
    def __init__(self, message, retry_at):
        super().__init__(message)
        self.retry_at=retry_at

def normalize_order(order):
    if not isinstance(order,list):
        return list(PROVIDERS)
    names=[]
    for name in order:
        if name in PROVIDERS and name not in names:
            names.append(name)
    return names+[name for name in PROVIDERS if name not in names]


async def configured(db):
    order=await setting(db,'ai_order',list(PROVIDERS))
    # Extend saved three-provider orders without discarding admin preferences.
    order=normalize_order(order)
    rows=[]
    for name in order:
        model=await setting(db,'model' if name=='google' else name+'_model',MODELS[name][0])
        key=await setting(db,KEYS[name],os.environ.get('GEMINI_API_KEY','') if name=='google' else '')
        state=await db.fetchrow('SELECT * FROM ai_provider_state WHERE provider=$1 AND model=$2',name,model)
        rows.append({'name':name,'model':model,'key':key,'enabled':await setting(db,name+'_enabled',name=='google'),
                     'retry_at':state['retry_at'] if state else 0,'error':state['last_error'] if state else None,
                     'allowed':model in MODELS[name]})
    return rows

async def reset(db, name=None):
    if name is None:
        await db.execute('UPDATE ai_provider_state SET retry_at=0,last_error=NULL,updated_at=now()')
    else:
        await db.execute('UPDATE ai_provider_state SET retry_at=0,last_error=NULL,updated_at=now() WHERE provider=$1',name)
    await put(db,'ai_retry_until',0)
    await put(db,'ai_last_error','')

async def cooldown(db, name, model, seconds, error):
    retry=time.time()+seconds
    await db.execute('INSERT INTO ai_provider_state(provider,model,retry_at,last_error) VALUES($1,$2,$3,$4) ON CONFLICT(provider,model) DO UPDATE SET retry_at=$3,last_error=$4,updated_at=now()',name,model,retry,error)
    return retry

def retry_seconds(response, payload):
    # Respect provider Retry-After. Daily limits must not cause repeated requests.
    details=json.dumps(payload).lower()
    daily=any(x in details for x in ('perday','per_day','daily','requests per day','per-day'))
    seconds=60
    if response.status_code==429 and daily:
        now=datetime.now(timezone.utc)
        if response.request.url.host=='generativelanguage.googleapis.com':
            from zoneinfo import ZoneInfo
            local=now.astimezone(ZoneInfo('America/Los_Angeles'))
            seconds=(datetime.combine(local.date()+timedelta(days=1),datetime.min.time(),local.tzinfo)-now).total_seconds()+5
        else:seconds=3600
    elif response.status_code in (401,402,403,404):seconds=3600
    elif response.status_code not in (429,500,502,503,504):seconds=300
    if response.status_code==429 and response.headers.get('x-ratelimit-limit-req-minute')=='0':
        seconds=max(seconds,3600)
    header=response.headers.get('retry-after','')
    try:seconds=max(seconds,float(header))
    except ValueError:
        try:seconds=max(seconds,(parsedate_to_datetime(header)-datetime.now(timezone.utc)).total_seconds())
        except (ValueError,TypeError,OverflowError):pass
    return min(max(seconds,1),86400)


def failure_message(name,response,payload):
    prefix=f"{NAMES[name]} HTTP {response.status_code}: "
    if response.status_code==429:
        if response.headers.get('x-ratelimit-limit-req-minute')=='0':
            return prefix+'для этой модели лимит 0 запросов/мин; выберите доступную модель из Limits'
        error=payload.get('error',{}) if isinstance(payload,dict) else {}
        metadata=error.get('metadata',{}) if isinstance(error,dict) else {}
        if metadata.get('limit_source')=='upstream_provider_shared_pool':
            return prefix+'бесплатный провайдер временно ограничен'
        return prefix+'исчерпана квота / лимит запросов или токенов'
    return prefix+'ошибка доступа / сервиса'
