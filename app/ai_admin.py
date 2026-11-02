"""Private Telegram controls for AI providers. Keys are never rendered."""
import time
from datetime import datetime, timezone, timedelta
from .db import setting, put
from .providers import PROVIDERS,NAMES,MODELS,KEYS,configured,reset,normalize_order


def b(text,data):return {'text':text,'callback_data':data}

def until(value):
    return datetime.fromtimestamp(value,timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')

async def overview(db,tg,chat):
    rows=await configured(db)
    lines=['AI: Gemma, NVIDIA и резервные сервисы','Порядок: '+' → '.join(NAMES[p['name']] for p in rows)]
    for p in rows:
        state='выключен' if not p['enabled'] else 'нужен ключ' if not p['key'] else 'модель недоступна' if not p['allowed'] else 'ожидание до '+until(p['retry_at']) if p['retry_at']>time.time() else 'подключён'
        lines.append(f"{NAMES[p['name']]}: {state}\n  {p['model']}")
        if p['error']:lines.append('  '+p['error'])
    last=await db.fetchrow('SELECT provider,model,status_code,error FROM ai_requests ORDER BY id DESC LIMIT 1')
    if last:lines.append(f"Последний запрос: {NAMES.get(last['provider'],last['provider'])}, HTTP {last['status_code'] or '—'}"+(' · '+last['error'] if last['error'] else ''))
    lines.append('\nGemma бесплатна в пределах квот Google. OpenRouter использует только :free с ценой токенов 0. Mistral: используйте Free mode; условия аккаунта и баланс бот не может узнать. Если бесплатный доступ закончится, включённый платный аккаунт Mistral может тарифицировать запросы.\n\nПри лимите/ошибке весь товар передаётся следующему доступному сервису. NVIDIA NIM распознаёт все фото товара; доступ и квоты зависят от аккаунта NVIDIA. Низкая уверенность → ручной выбор категории; повторного копирования товара из-за смены AI не будет.')
    return await tg.say(chat,'\n'.join(lines),[[b(NAMES[p],f'aiprovider:{p}')] for p in PROVIDERS]+[[b('Изменить порядок','aiorder')],[b('Повторить доступ ко всем','retryai')],[b('Где создать ключи','aihelp')],[b('📋 Все заданные правила AI','airulesall:0')],[b('Меню','home')]])

async def provider_menu(db,tg,chat,name):
    if name not in PROVIDERS:raise ValueError('Неизвестный AI сервис')
    p=next(p for p in await configured(db) if p['name']==name)
    state='ожидание до '+until(p['retry_at']) if p['retry_at']>time.time() else 'нет активной блокировки'
    text=f"{NAMES[name]}\nМодель: {p['model']}\nКлюч: {'сохранён' if p['key'] else 'не введён'}\nВключён: {'да' if p['enabled'] else 'нет'}\nСтатус: {state}\n{p['error'] or ''}\n\nВвод ключа автоматически включает сервис. Сообщение с ключом будет удалено; значение не показывается."
    if p['model']=='dots-studio/dots-3-note-preview:free':text+='\nВременный резерв: OpenRouter объявил отключение этой Preview-модели 31.12.2026. После этого выберите другую модель в этом меню.'
    if name=='nvidia':text+='\nNVIDIA NIM: все фото товара отправляются вместе. При лимите бот использует следующий доступный сервис. Квоты смотрите в NVIDIA Build.'
    if name=='mistral':text+='\nИспользуйте ключ аккаунта Free mode. Сам API не сообщает боту, бесплатный у вас аккаунт или платный.'
    keys=[[b('🔑 Ввести / заменить ключ','config:'+KEYS[name])],
          [b('Выключить' if p['enabled'] else 'Включить',f'aitoggle:{name}'),b('Убрать ключ',f'aiclearkey:{name}')]]
    keys += [[b(model,f'aimodel:{name}:{i}')] for i,model in enumerate(MODELS[name])]
    keys += [[b('Повторить доступ',f'airetry:{name}')],[b('Все AI сервисы','aiproviders')]]
    return await tg.say(chat,text,keys)


async def models_menu(db,tg,chat,name):
    return await tg.say(chat,'Выберите модель '+NAMES[name]+':',[[b(model,f'aimodel:{name}:{i}')] for i,model in enumerate(MODELS[name])]+[[b('Назад',f'aiprovider:{name}')]])

async def help_menu(db,tg,chat):
    return await tg.say(chat,
        'AI: бесплатная Google Gemma\nGemma имеет квоты: точные RPM/TPM/RPD смотрите в Google AI Studio. При ответе 429 (исчерпана квота) бот использует подключённый резерв, затем повторяет Google после ожидания.\n\n'
        '1. Google: существующий ключ уже подходит для Gemma. Подписка на чат не требуется.\n'
        '2. Mistral: зарегистрируйтесь в Mistral Studio, создайте API key в аккаунте Free mode. Бесплатный объём смотрите в Limits. Не включайте pay-as-you-go, если нужны только бесплатные запросы.\n'
        '3. OpenRouter: создайте API key. Без покупки кредитов :free обычно ограничены 50 запросами/сутки; после покупки от $10 кредитов — 1000/сутки, плюс возможные комиссия и налоги. В боте разрешены только бесплатные модели.\n'
        '4. NVIDIA: создайте API key в NVIDIA Build. Подключены модели с распознаванием изображений; действуют квоты вашего аккаунта.\n'
        '5. Откройте «AI сервисы» → нужный сервис → «Ввести / заменить ключ» и отправьте ключ только в личный чат боту. Сообщение будет удалено.\n\n'
        'Бот не может узнать владельца проекта, баланс или условия оплаты Mistral по ключу. Плата за VPS остаётся отдельно. Если все сервисы недоступны, товары сохраняются: их можно отправить кнопкой категории.',
        [[{'text':'Google AI Studio','url':'https://aistudio.google.com/'}],[{'text':'Mistral Studio','url':'https://console.mistral.ai/'}],[{'text':'Ключи OpenRouter','url':'https://openrouter.ai/settings/keys'}],[{'text':'NVIDIA Build','url':'https://build.nvidia.com/'}],[b('AI сервисы','aiproviders')],[b('Проверка товаров','reviews:0'),b('Меню','home')]])

async def callback(db,tg,chat,cmd,parts):
    name=parts[1] if len(parts)>1 else ''
    if cmd=='aiproviders':await overview(db,tg,chat)
    elif cmd=='aiorder':
        order=normalize_order(await setting(db,'ai_order',list(PROVIDERS)))
        if name=='up':
            target=parts[2] if len(parts)>2 else ''
            if target not in PROVIDERS:raise ValueError('Неизвестный AI сервис')
            i=order.index(target)
            if i:order[i-1],order[i]=order[i],order[i-1]
            await put(db,'ai_order',order)
        elif name:
            # Keep old Telegram buttons valid after this upgrade.
            if name not in ('mistral','openrouter'):raise ValueError('Неизвестный порядок')
            order=normalize_order(['google','mistral','openrouter'] if name=='mistral' else ['google','openrouter','mistral'])
            await put(db,'ai_order',order)
            await overview(db,tg,chat)
            return True
        await tg.say(chat,'Порядок резервных сервисов: '+ ' → '.join(NAMES[p] for p in order)+
                     '\nНажмите ↑, чтобы поднять сервис на одну позицию. При лимите бот пропускает его до окончания ожидания.',
                     [[b('↑ '+NAMES[p],f'aiorder:up:{p}')] for p in order[1:]]+[[b('AI сервисы','aiproviders')]])
    elif cmd in ('aiprovider','aitoggle','aiclearkey','airetry','aimodel'):
        if name not in PROVIDERS:raise ValueError('Неизвестный AI сервис')
        if cmd=='aitoggle':await put(db,name+'_enabled',not await setting(db,name+'_enabled',name=='google'));await reset(db,name)
        elif cmd=='aiclearkey':
            await put(db,KEYS[name],'');await put(db,name+'_enabled',False);await reset(db,name)
        elif cmd=='airetry':await reset(db,name)
        elif cmd=='aimodel':
            index=int(parts[2])
            if not 0<=index<len(MODELS[name]):raise ValueError('Недоступная модель')
            await put(db,'model' if name=='google' else name+'_model',MODELS[name][index]);await reset(db,name)
            if name=='google':
                await put(db,'input_price',0);await put(db,'output_price',0)
        await provider_menu(db,tg,chat,name)
    else:return False
    return True
