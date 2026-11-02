from .db import setting,put
from .routing import CORE_AI_RULES,plus_config,plus_evidence,omitted_text
from .deliveries import rows,decide
from .operations import source_unavailable,source_unavailable_text


def b(text,data):return {'text':text,'callback_data':data}


async def overview(db,tg,chat):
    custom=await setting(db,'ai_instructions','')
    count=await db.fetchval("SELECT count(*) FROM categories WHERE code<>'women.plus_size'")
    await tg.say(chat,f'Правила AI и отправки\nУже заданы общие инструкции и {count} правил категорий. Кнопка «Все заданные правила» или /rules показывает их полный текст.\n\n1. Общие инструкции применяются ко всем товарам.\n2. У каждой категории есть своё правило AI. AI может выбрать несколько совместимых категорий одного товара; дополнительная категория отправляется только при достаточной уверенности и подключённом канале. Сомнения о дополнительной категории не задерживают уверенную основную. Настройка «Только отдельные вещи» исключает комплекты из выбранного канала, даже если AI предложил их отправить.\n3. В ручном разборе кнопка «Несколько категорий» позволяет отметить каналы и отправить весь товар одной кнопкой. Правило больших размеров продолжает добавлять свой канал.\n\nДополнительные общие пожелания:\n'+(custom or 'Отдельные пожелания не добавлены. Общие инструкции и правила категорий уже действуют.'),[
        [b('📋 Все заданные правила','airulesall:0')],
        [b('📖 Общие инструкции AI','airulescore')],
        [b('✏️ Общие пожелания','airulesedit')],
        [b('🗂 Правила по категориям','categories:0')],
        [b('➕ Большие размеры: доп. отправка','plusrule')],
        [b('🏠 Меню','home')]])


async def plus_menu(db,tg,chat):
    cfg=await plus_config(db)
    cat=await db.fetchrow('SELECT * FROM categories WHERE code=$1',cfg['category'])
    channel=(cat['link'] or str(cat['destination'])) if cat and cat['destination'] else 'не подключён'
    await tg.say(chat,f"Дополнительная отправка → Большие размеры\n\nПравило: {'включено' if cfg['enabled'] else 'выключено'}\nРоссийский размер: от {cfg['threshold']} (минимальный размер всего ряда, а не любое число внутри него).\nРяд начинается с XXL / 2XL и больше: {'учитывать' if cfg['letters'] else 'не учитывать'}.\nЯвные слова «большие размеры», plus size, батал: учитывать, если нет противоречащего размерного ряда.\nТолько женская одежда.\nКанал: {channel}\nКатегория канала: {'включена' if cat and cat['enabled'] else 'выключена'}\n\nЧитаем строки «Размеры / Sizes» в тексте публикации. Числа без обозначения системы в этих строках считаем российскими. Явные EU/TR/CN/US/UK и мерки в см не переводим в RU. Цену, код и количество не считаем размерами. Oversize без размеров сам по себе не означает plus size.\n\nПримеры при пороге 42: ряд 42–50 → два канала; ряд 40–50 → только основной. Ряд XXL–4XL → два канала; ряд M–XXL → только основной. Если указаны и числа, и буквы, оба ряда должны подходить. В каждом канале — весь товар. Если каналы совпадают, копия будет одна. Изменения действуют до начала отправки товара.",[
      [b('Включить/выключить правило','plustoggle')],
      [b('Порог российского размера','plusthreshold')],
      [b('XXL+ вкл/выкл','plusletters')],
      *([[b('Канал больших размеров',f"category:{cat['id']}")]] if cat else []),
      [b('🧪 Проверить описание','plustest')],
      [b('Назад: Правила AI','airules')]])


async def callback(db,tg,actor,chat,cmd,parts,ask):
    if cmd=='airulesall':await all_rules(db,tg,chat,int(parts[1]) if len(parts)>1 else 0)
    elif cmd=='airules':await overview(db,tg,chat)
    elif cmd=='airulescore':
        await tg.say(chat,'Действующие общие инструкции AI (всегда применяются):\n\n'+CORE_AI_RULES,[[b('Назад','airules')]])
    elif cmd=='airulesedit':
        current=await setting(db,'ai_instructions','')
        await ask(db,tg,actor,chat,'airulesedit','Текущие общие пожелания:\n'+(current or 'Не заданы.')+'\n\nНовый текст ЗАМЕНИТ только общие пожелания (до 1500 символов). Для дополнения включите в новый текст и прежние пожелания. Слово «очистить» удаляет только это поле; обязательные инструкции и правила категорий сохраняются.')
    elif cmd=='plusrule':await plus_menu(db,tg,chat)
    elif cmd=='plustoggle':
        await put(db,'plus_size_enabled',not (await plus_config(db))['enabled']);await plus_menu(db,tg,chat)
    elif cmd=='plusletters':
        await put(db,'plus_size_letters',not (await plus_config(db))['letters']);await plus_menu(db,tg,chat)
    elif cmd=='plusthreshold':
        await ask(db,tg,actor,chat,'plusthreshold',f"Сейчас: от {(await plus_config(db))['threshold']}-го российского размера. Пришлите чётное число от 40 до 80.")
    elif cmd=='plustest':
        await ask(db,tg,actor,chat,'plustest','Пришлите описание товара целиком. Бот покажет, сработает ли правило больших размеров для женской одежды. Публикации и запросов к AI не будет.')
    elif cmd in ('retrydelivery','deliveryyes','deliveryno'):
        pid,did=int(parts[1]),int(parts[2]);await decide(db,pid,did,cmd,actor)
        await show_delivery(db,tg,chat,await db.fetchrow('SELECT * FROM products WHERE id=$1',pid))
    else:return False
    return True


async def text_input(db,tg,actor,chat,action,text):
    if action=='airulesedit':
        if len(text)>1500:raise ValueError('Правило должно быть не длиннее 1500 символов')
        await put(db,'ai_instructions','' if text.lower()=='очистить' else text)
    elif action=='plusthreshold':
        try:value=int(text)
        except ValueError:raise ValueError('Нужно чётное число от 40 до 80')
        if value<40 or value>80 or value%2:raise ValueError('Нужно чётное число от 40 до 80')
        await put(db,'plus_size_threshold',value)
    elif action=='plustest':
        cfg=await plus_config(db);e=plus_evidence(text,cfg)
        await db.execute('DELETE FROM admin_state WHERE actor=$1',actor)
        await tg.say(chat,('✅ Дополнительная отправка сработает.' if e['matched'] and cfg['enabled'] else 'Дополнительной отправки не будет.')+'\n'+e['reason']+(' Правило выключено.' if not cfg['enabled'] else '')+'\nПроверка текста завершена; товар никуда не отправлялся.',[[b('Правило больших размеров','plusrule')]])
        return True
    else:return False
    await db.execute('DELETE FROM admin_state WHERE actor=$1',actor)
    if action=='airulesedit':await overview(db,tg,chat)
    else:await plus_menu(db,tg,chat)
    return True


async def show_delivery(db,tg,chat,p):
    ds=await rows(db,p['id'])
    text=f"Товар #{p['id']} · отправка по каналам\nОсновная категория: {p['final_category']}\n"
    keys=[]
    text+=await omitted_text(db,p)
    for d in ds:
        label={'published':'✅ Опубликован','skipped':'Пропущен','queued':'⏳ Ожидает','sending':'Отправляется','finishing':'Копия есть, восстанавливаются кнопки','review':'⚠️ Ошибка отправки','uncertain':'⚠️ Требуется сверка'}.get(d['status'],d['status'])
        text+=f"\n{'Дополнительно' if d['kind']=='extra' else 'Основной'} → {d['title'] or d['category_code']}\n{label} · {len(d['message_ids'] or [])}/{len(d['source_message_ids'])} сообщений\n"
        if d['rule_evidence'].get('reason'):text+=d['rule_evidence']['reason']+'\n'
        if d['message_ids']:text+=f"https://t.me/c/{str(d['destination']).removeprefix('-100')}/{d['message_ids'][0]}\n"
        if d['error']:text+=(source_unavailable_text() if source_unavailable(d) else d['error'][:400])+'\n'
        if d['status']=='finishing':keys.append([b('Повторить восстановление кнопок',f"finish:{p['id']}")])
        if d['status']=='review' and not source_unavailable(d):keys.append([b('Повторить: '+(d['title'] or d['category_code'])[:30],f"retrydelivery:{p['id']}:{d['id']}")])
        elif d['status']=='uncertain':
            keys.append([b('Весь товар уже есть: '+(d['title'] or '')[:20],f"deliveryyes:{p['id']}:{d['id']}")])
            keys.append([b('Товара точно нет: '+(d['title'] or '')[:20],f"deliveryno:{p['id']}:{d['id']}")])
    if p['error']:text+='\n'+p['error'][:500]+'\n'
    text+='\nУже опубликованные каналы повторно не отправляются. При неизвестном исходе сначала сверьте весь товар в указанном канале.'
    text+=f"\nОригинал: https://t.me/c/{str(p['source_channel_id']).removeprefix('-100')}/{p['source_message_id']}"
    if ds and p['status']=='review' and all(d['status'] in ('queued','review') and not d['message_ids'] for d in ds):keys.append([b('❌ Пропустить товар',f"skip:{p['id']}")])
    keys.extend([[b('Обновить',f"item:{p['id']}")],[b('Очередь','reviews:0'),b('Меню','home')]])
    return await tg.say(chat,text,keys)


async def rule_pages(db):
    custom=await setting(db,'ai_instructions','')
    cats=await db.fetch("SELECT code,title,description,single_only FROM categories WHERE code<>'women.plus_size' ORDER BY id")
    cfg=await plus_config(db)
    sections=["ОБЯЗАТЕЛЬНЫЕ ОБЩИЕ ИНСТРУКЦИИ\n"+CORE_AI_RULES,
              "ОБЩИЕ ПОЖЕЛАНИЯ АДМИНИСТРАТОРА\n"+(custom or 'Не добавлены. Обязательные инструкции выше и правила категорий ниже действуют.'),
              f"ДОПОЛНИТЕЛЬНОЕ ПРАВИЛО: БОЛЬШИЕ РАЗМЕРЫ\nСостояние: {'включено' if cfg['enabled'] else 'выключено'}. Только женская одежда. Минимальный размер всего ряда от {cfg['threshold']}. Буквенный ряд от XXL / 2XL: {'учитывать' if cfg['letters'] else 'не учитывать'}. Одного размера внутри более широкого ряда недостаточно. Явная маркировка plus size / большие размеры / батал учитывается без противоречащего ряда. Подробности и настройка — «Большие размеры: доп. отправка»."]
    sections += [f"КАТЕГОРИЯ: {c['title']} ({c['code']})\n{c['description']}"+("\nТолько отдельные вещи (AUTO): ДА. Комплекты и их части исключены." if c['single_only'] else "\nТолько отдельные вещи (AUTO): НЕТ.") for c in cats]
    pages=[];page=''
    for section in sections:
        pieces=[section[i:i+3800] for i in range(0,len(section),3800)] or ['']
        for piece in pieces:
            if page and len(page)+len(piece)+2>3800:
                pages.append(page);page=''
            page+=('\n\n' if page else '')+piece
    if page:pages.append(page)
    return pages


async def all_rules(db,tg,chat,page=0):
    pages=await rule_pages(db);page=max(0,min(page,len(pages)-1))
    nav=[]
    if page:nav.append(b('← Назад',f'airulesall:{page-1}'))
    if page+1<len(pages):nav.append(b('Далее →',f'airulesall:{page+1}'))
    keys=([nav] if nav else [])+[[b('Изменить общие пожелания','airulesedit')],[b('Открыть правило категории','categories:0')],[b('Большие размеры: доп. отправка','plusrule')],[b('Меню правил','airules')]]
    return await tg.say(chat,f"Все заданные правила AI · {page+1}/{len(pages)}\nЭто сохранённые тексты, которые сейчас используются.\n\n"+pages[page],keys)
