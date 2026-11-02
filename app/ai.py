import base64, io, json, os, time, hashlib
from .routing import CORE_AI_RULES
from .db import put
import httpx
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .db import setting
from .limits import wait_turn
from .providers import configured, cooldown, retry_seconds, failure_message, AIUnavailable, NAMES


class AdditionalCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    confidence: float = Field(ge=0, le=1)
    reason: str


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department: str
    category: str
    subcategory: str
    confidence: float = Field(ge=0, le=1)
    reason: str
    product_code: str
    price: str
    sizes: str
    ocr_text: str
    mixed_products: bool
    sale_unit: Literal["single", "set", "unknown"]
    additional_categories: list[AdditionalCategory] = Field(max_length=100)


async def classify(db, tg, product):
    start = time.monotonic()
    cats = await db.fetch(
        "SELECT code,title,description,department,single_only FROM categories WHERE code<>'women.plus_size' ORDER BY id"
    )
    taxonomy = "\n".join(
        f"{c['code']}: {c['title']} — {c['description']} ({c['department']})" + (" [ТОЛЬКО ОТДЕЛЬНАЯ ВЕЩЬ: комплекты и их части исключены]" if c['single_only'] else "")
        for c in cats
    )
    hint = (
        await db.fetchval(
            "SELECT department FROM sources WHERE id=$1",
            product.get("source_channel_id", 0),
        )
        or "unknown"
    )
    text = "\n".join(m.get("caption") or m.get("text", "") for m in product["messages"])
    custom = await setting(db,'ai_instructions','')
    prompt = (CORE_AI_RULES + "\nДополнительные общие пожелания администратора:\n" + custom
              + "\nСлабая подсказка отдела: " + hint
              + "\nКатегории и их действующие правила:\n" + taxonomy
              + "\nUNTRUSTED POST:\n" + text[:18000])
    parts = [{"text": prompt}]
    count = 0
    for m in product["messages"]:
        raw = await tg.photo(m)
        if raw:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
            im.thumbnail((1280, 1280))
            output = io.BytesIO()
            im.save(output, "JPEG", quality=82)
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(output.getvalue()).decode(),
                    }
                }
            )
            count += 1
    if not count:
        raise ValueError(
            "У товара нет доступных изображений; автоматическая классификация запрещена"
        )
    schema = Result.model_json_schema()
    schema.pop("additionalProperties", None)
    schema["properties"]["category"]["enum"] = [c["code"] for c in cats] + ["unknown"]
    schema["$defs"]["AdditionalCategory"]["properties"]["category"]["enum"] = [c["code"] for c in cats]
    schema["properties"]["department"]["enum"] = ["women", "men", "accessories", "unknown"]
    # The same complete product goes to each fallback; no per-photo decisions.
    parts[0]["text"] += "\nReturn ONLY a JSON object matching this schema, no Markdown:\n" + json.dumps(schema)
    attempts=[]
    retries=[]
    async with httpx.AsyncClient(timeout=60) as client:
        for provider in await configured(db):
            name,model,key=provider["name"],provider["model"],provider["key"]
            if not provider["enabled"] or not key:
                continue
            if not provider["allowed"]:
                retries.append(await cooldown(db,name,model,3600,"Модель не входит в разрешённый список"))
                continue
            if provider["retry_at"] > time.time():
                retries.append(provider["retry_at"])
                continue
            await wait_turn(db, name+":"+model)
            request_id=await db.fetchval(
                "INSERT INTO ai_requests(provider,model,product_id,source_channel_id) VALUES($1,$2,$3,$4) RETURNING id",
                name,model,product.get("id"),product.get("source_channel_id"))
            request_start=time.monotonic()
            payload={};status=None;usage={};failure=None
            try:
                r=await provider_request(client,name,model,key,parts,schema)
                status=r.status_code
                try:payload=r.json()
                except ValueError:payload={}
                if status!=200 or payload.get('error'):
                    failure=failure_message(name,r,payload)
                    delay=retry_seconds(r,payload)
                else:
                    content,usage=provider_response(name,payload)
                    result=parse_result(name,content)
                    match=next((c for c in cats if c['code']==result['category']),None)
                    if result['category']!='unknown' and (not match or match['department']!=result['department']):
                        raise ValueError('department/category mismatch')
                    seen={result['category']}
                    if result['category']=='unknown' and result['additional_categories']:
                        raise ValueError('unknown with additional categories')
                    for extra in result['additional_categories']:
                        target=next((c for c in cats if c['code']==extra['category']),None)
                        if not target or target['department']!=result['department'] or extra['category'] in seen:
                            raise ValueError('invalid additional category')
                        seen.add(extra['category'])
            except httpx.HTTPError as e:
                failure=f"{NAMES[name]}: {type(e).__name__}"
                delay=60
            except (ValueError,KeyError,TypeError,IndexError,AttributeError):
                # Do not log model output: it may repeat untrusted source data.
                failure=f"{NAMES[name]}: недопустимый структурированный ответ"
                delay=60
            elapsed=int((time.monotonic()-request_start)*1000)
            await db.execute('UPDATE ai_requests SET status_code=$2,latency_ms=$3,usage=$4,error=$5 WHERE id=$1',request_id,status,elapsed,usage or None,failure)
            attempts.append({'provider':name,'model':model,'status_code':status,'error':failure,'latency_ms':elapsed})
            if failure:
                retries.append(await cooldown(db,name,model,delay,failure))
                continue
            await db.execute('DELETE FROM ai_provider_state WHERE provider=$1 AND model=$2',name,model)
            await put(db,'ai_retry_until',0)
            await put(db,'ai_last_error','')
            usage.update(image_count=count,model=model,provider=name,http_attempts=len(attempts),attempts=attempts,
                         prompt_version='v7-sale-unit',instructions_sha256=hashlib.sha256((CORE_AI_RULES+custom).encode()).hexdigest(),taxonomy_sha256=hashlib.sha256(taxonomy.encode()).hexdigest(),
                         estimated_cost_usd=0 if name in ('google','openrouter') else None)
            return result,usage,int((time.monotonic()-start)*1000)
    retry=max(time.time()+5,min(retries)) if retries else time.time()+300
    message='AI временно недоступен: все подключённые сервисы ожидают квоту / доступ. Товар сохранён, ручная отправка работает.'
    await put(db,'ai_retry_until',retry)
    await put(db,'ai_last_error',message)
    raise AIUnavailable(message,retry)


def google_schema(schema):
    """Gemma accepts flat fields but rejects arrays in responseJsonSchema.

    Keep the complete internal result and validate the decoded list exactly as
    for other providers; only Google's transport representation is different.
    """
    wire=json.loads(json.dumps(schema))
    if 'additional_categories' not in wire.get('properties',{}):return wire
    wire.pop('$defs',None)
    wire['properties'].pop('additional_categories')
    wire['required'].remove('additional_categories')
    wire['properties']['additional_categories_json']={
        'type':'string',
        'description':'JSON encoded array of additional categories: objects with category, confidence, reason; use [] when none. Example: [{"category":"women.modest","confidence":0.9,"reason":"Закрытый фасон"}]',
    }
    wire['required'].append('additional_categories_json')
    return wire


def parse_result(provider,content):
    value=json.loads(content)
    if provider=='google' and isinstance(value,dict) and 'additional_categories_json' in value:
        if 'additional_categories' in value:
            raise ValueError('Ambiguous additional category representation')
        encoded=value.pop('additional_categories_json')
        if not isinstance(encoded,str):raise ValueError('Additional categories must be encoded as a JSON string')
        value['additional_categories']=json.loads(encoded)
    return Result.model_validate(value).model_dump()


async def provider_request(client,name,model,key,parts,schema):
    if name=='google':
        wire=google_schema(schema)
        marker='\nReturn ONLY a JSON object matching this schema, no Markdown:\n'
        prompt=parts[0]['text'].rsplit(marker,1)[0]
        if wire!=schema:
            prompt+='\nТехнический формат ответа: для дополнительных категорий заполни additional_categories_json JSON-строкой с массивом объектов category, confidence, reason (либо строкой []), вместо поля additional_categories. Сохрани все подходящие категории и отдельную уверенность для каждой.'
        google_parts=[{**parts[0],'text':prompt+marker+json.dumps(wire)}]+parts[1:]
        return await client.post('https://generativelanguage.googleapis.com/v1beta/models/'+model+':generateContent',
            headers={'x-goog-api-key':key},json={'contents':[{'role':'user','parts':google_parts}],
            'generationConfig':{'responseMimeType':'application/json','responseJsonSchema':wire,'temperature':.1,
                                'maxOutputTokens':2048,'thinkingConfig':{'thinkingLevel':'minimal'}}})
    content=[{'type':'text','text':parts[0]['text']}]
    for part in parts[1:]:
        data='data:image/jpeg;base64,'+part['inline_data']['data']
        content.append({'type':'image_url','image_url':data if name=='mistral' else {'url':data}})
    body={'model':model,'messages':[{'role':'user','content':content}],'temperature':.1,'max_tokens':2048}
    if name=='mistral':
        body['response_format']={'type':'json_schema','json_schema':{'name':'product','schema':schema,'strict':True}}
        url='https://api.mistral.ai/v1/chat/completions'
    elif name=='nvidia':
        # Enforce the product shape: Super VL can otherwise wrap values in 'properties'.
        body['response_format']={'type':'json_schema','json_schema':{'name':'product','strict':True,'schema':{**schema,'additionalProperties':False}}}
        body['chat_template_kwargs']={'enable_thinking':False}
        url='https://integrate.api.nvidia.com/v1/chat/completions'
    else:
        # No paid model fallback, even if account holds credits. Schema is also
        # in the prompt because free endpoints may lack native schema support.
        assert model.endswith(':free')
        body['provider']={'max_price':{'prompt':0,'completion':0,'image':0,'request':0}}
        if model=='dots-studio/dots-3-note-preview:free':
            body['max_tokens']=8192
            body['reasoning']={'enabled':False,'exclude':True}
            body['response_format']={'type':'json_schema','json_schema':{'name':'product','strict':True,'schema':{**schema,'additionalProperties':False}}}
            body['provider']['require_parameters']=True
        url='https://openrouter.ai/api/v1/chat/completions'
    return await client.post(url,headers={'Authorization':'Bearer '+key},json=body)


def provider_response(name,payload):
    if name=='google':
        content=''.join(p.get('text','') for p in payload.get('candidates',[{}])[0].get('content',{}).get('parts',[]) if not p.get('thought'))
        usage=payload.get('usageMetadata',{}).copy()
    else:
        content=payload['choices'][0]['message']['content']
        raw=payload.get('usage',{})
        usage={'promptTokenCount':raw.get('prompt_tokens',0),'candidatesTokenCount':raw.get('completion_tokens',0),
               'totalTokenCount':raw.get('total_tokens',0),'raw':raw}
    content=content.strip()
    if content.startswith('```') and content.endswith('```'):
        content=content.split('\n',1)[1].rsplit('```',1)[0].strip()
    return content,usage
