import os, re, json, time
from datetime import datetime, timezone, timedelta
from .db import setting, put
from .pipeline import buttons
from . import ai_admin, routing_admin, multi_admin
from .admin_text import message_text
from .routing import selected_categories, omitted_text
from .providers import KEYS, MODELS, reset as reset_ai
from .operations import queue_manual, queue_manual_many, cached_count, resume_cached, source_unavailable, source_unavailable_text


STATUS_LABELS = {
    "collecting": "Ожидает распознавания",
    "analyzing": "Распознаётся",
    "review": "На проверке",
    "low_confidence": "Низкая уверенность",
    "queued": "Ожидает публикации",
    "sending": "Отправляется",
    "finishing": "Восстанавливаются кнопки",
    "published": "Опубликован",
    "skipped": "Пропущен",
    "uncertain": "Нужна сверка",
}


def button(text, data):
    return {"text": text, "callback_data": data}


def home():
    return [
        [button("📊 Статистика", "stats"), button("🔎 Проверка товаров", "reviews:0")],
        [
            button("📥 Источники", "sources"),
            button("🗂 Категории / каналы", "categories:0"),
        ],
        [button("⚙️ Настройки", "settings"), button("🤖 AI сервисы", "aiproviders")],
        [button("📖 Правила AI / доп. каналы", "airules")],
        [button("✅ Последние публикации", "published"), button("Готовые к AUTO", "cached")],
    ]


async def admins(db):
    return await setting(
        db, "admins", [int(x) for x in os.environ["ADMIN_IDS"].split(",")]
    )


async def ask(db, tg, actor, chat, action, text, data=None):
    await db.execute(
        "INSERT INTO admin_state VALUES($1,$2,$3) ON CONFLICT(actor) DO UPDATE SET action=$2,data=$3",
        actor,
        action,
        data or {},
    )
    await tg.say(chat, text + "\n\n/cancel — отмена")


async def resolve(db, tg, message, text, source=False):
    origin = message.get("forward_origin", {}).get("chat", {})
    if origin:
        target = origin["id"]
    elif re.fullmatch(r"-?\d+", text):
        target = int(text)
    elif text.startswith("https://t.me/+"):
        raise ValueError(
            "Приватную ссылку Bot API не раскрывает. Перешлите публикацию из канала или пришлите ID -100… после добавления бота администратором."
        )
    else:
        target = "@" + text.replace("https://t.me/", "").lstrip("@").split("/")[0]
    chat = await tg.call("get_chat", chat_id=target)
    if chat["type"] != "channel":
        raise ValueError("Нужен Telegram-канал")
    me = await tg.call("get_me")
    member = await tg.call("get_chat_member", chat_id=chat["id"], user_id=me["id"])
    if member["status"] not in ("administrator", "creator"):
        raise ValueError("Сначала добавьте @Auto_Sort_bot администратором канала")
    if not source and not member.get(
        "can_post_messages", member["status"] == "creator"
    ):
        raise ValueError("Нужно право публикации в канале")
    if not source and await db.fetchval(
        "SELECT 1 FROM sources WHERE id=$1", chat["id"]
    ):
        raise ValueError("Нельзя публиковать обратно в основной источник")
    if source and await db.fetchval(
        "SELECT 1 FROM categories WHERE destination=$1", chat["id"]
    ):
        raise ValueError(
            "Канал уже является назначением: сначала уберите привязку категории"
        )
    await db.execute('INSERT INTO available_channels(id,title,status,can_post) VALUES($1,$2,$3,$4) ON CONFLICT(id) DO UPDATE SET title=$2,status=$3,can_post=$4,updated_at=now()',chat['id'],chat['title'],member['status'],member.get('can_post_messages',False))
    return chat


async def bind_category(db,category_id,chat):
    link='https://t.me/'+chat['username'] if chat.get('username') else chat.get('invite_link')
    await db.execute('UPDATE categories SET destination=$2,link=$3,enabled=true WHERE id=$1',category_id,chat['id'],link)


async def channel_picker(db, tg, chat, kind, category_id=0, offset=0):
    rows = await db.fetch(
        "SELECT * FROM available_channels a WHERE status IN ('administrator','creator') AND NOT EXISTS(SELECT 1 FROM sources s WHERE s.id=a.id) AND ($1 OR can_post) ORDER BY title LIMIT 21 OFFSET $2",
        kind == "source",
        offset,
    )
    keys = [
        [button(r["title"][:45], f"pickchannel:{kind}:{category_id}:{r['id']}")]
        for r in rows[:20]
    ]
    if len(rows) > 20:
        keys.append([button("Далее", f"channels:{kind}:{category_id}:{offset + 20}")])
    if keys:
        await tg.say(
            chat, "Выберите канал из подключённых или перешлите его публикацию:", keys
        )


async def show_home(db, tg, chat):
    counts = {r['status']:r['n'] for r in await db.fetch("SELECT status,count(*) n FROM products p JOIN sources s ON s.id=p.source_channel_id WHERE s.enabled GROUP BY status")}
    mode=await setting(db,'mode','REVIEW')
    cooldown=await setting(db,'ai_retry_until',0)>time.time()
    text=(f"E-MODA · сортировка\nРежим: {mode} · {'⏸ Пауза' if await setting(db,'paused',True) else '▶️ Работает'}\n"
          f"Опубликовано: {counts.get('published',0)} · отправляется: {sum(counts.get(k,0) for k in ('queued','sending','finishing'))}\n"
          f"На ручной разбор: {sum(counts.get(k,0) for k in ('review','low_confidence','uncertain'))} · ожидают AI: {sum(counts.get(k,0) for k in ('collecting','analyzing'))}\n"
          + ('⚠️ AI ожидает восстановления квоты. Ручная отправка работает.\n' if cooldown else '')
          + 'AUTO: уверенные товары → все подходящие категории. Неопознанные → ручной выбор.\nВ «Проверка товаров» можно отправить в одну категорию или отметить несколько.')
    return await tg.say(chat,text,home())


async def ai_help(db,tg,chat):
    return await ai_admin.help_menu(db,tg,chat)


async def route_keyboard(db,p,department=None,offset=0):
    department=department or await db.fetchval('SELECT department FROM sources WHERE id=$1',p['source_channel_id']) or 'women'
    if department not in ('women','men','accessories','all'): department='women'
    cats=await db.fetch("SELECT * FROM categories WHERE enabled AND destination IS NOT NULL AND ($1='all' OR department=$1) ORDER BY id LIMIT 21 OFFSET $2",department,offset)
    choices=[button(c['title'][:35],f"route:{p['id']}:{c['id']}") for c in cats[:20]]
    rows=[choices[i:i+2] for i in range(0,len(choices),2)]
    nav=[]
    if offset:nav.append(button('←',f"routepage:{p['id']}:{department}:{max(0,offset-20)}"))
    if len(cats)>20:nav.append(button('Далее →',f"routepage:{p['id']}:{department}:{offset+20}"))
    if nav:rows.append(nav)
    rows.append([button('☑️ Несколько категорий',f"multiroute:{p['id']}")])
    rows.append([button('Женское',f"routepage:{p['id']}:women:0"),button('Мужское',f"routepage:{p['id']}:men:0"),button('Все',f"routepage:{p['id']}:all:0")])
    return rows


async def settings_menu(db, tg, chat):
    mode = await setting(db, "mode", "REVIEW")
    paused = await setting(db, "paused", True)
    retry_at = await setting(db, "ai_retry_until", 0)
    retry_text = (
        datetime.fromtimestamp(retry_at, timezone(timedelta(hours=3))).strftime(
            "%d.%m %H:%M МСК"
        )
        if retry_at > time.time()
        else "нет"
    )
    text = f"Режим: {mode}\nПауза: {'да' if paused else 'нет'}\nAUTO от: {await setting(db, 'auto_threshold', 0.85)}\nREVIEW от: {await setting(db, 'review_threshold', 0.60)}\nМодель: {await setting(db, 'model', 'gemma-4-26b-a4b-it')}\nУведомления: {await setting(db, 'notify_enabled', True)} · получатель {await setting(db, 'review_admin_id', 'не выбран')}\nЛимит AI: {await setting(db, 'ai_rpm', 12)} запросов/мин\nОжидание квоты AI до: {retry_text}\nНизкая уверенность всегда требует ручного решения.\nИзменения применяются к следующим анализам."
    rows = [
        [button("AUTO", "mode:AUTO"), button("REVIEW", "mode:REVIEW")],
        [button("▶️ Продолжить" if paused else "⏸ Пауза", "pause")],
        [
            button("Порог AUTO", "config:auto_threshold"),
            button("Порог REVIEW", "config:review_threshold"),
        ],
        [button("🤖 AI сервисы / модели", "aiproviders")],
        [
            button("API-ключ Google", "config:gemini_key"),
            button("Повторить доступ к AI", "retryai"),
        ],
        [button("Уведомления вкл/выкл", "notify")],
        [button("📩 Присылать товары мне", "reviewme")],
        [button("💳 Квота AI и оплата", "aihelp")],
        [button("Администраторы", "config:admins")],
        [button("🏠 Меню", "home")],
    ]
    rows.insert(-1, [button("Запросов AI в минуту", "config:ai_rpm")])
    await tg.say(chat, text, rows)


async def reviews(db, tg, chat, offset=0):
    rows = await db.fetch(
        "SELECT p.id,p.status,p.final_category,p.confidence,p.error,c.title FROM products p LEFT JOIN categories c ON c.code=p.final_category WHERE p.status IN ('review','low_confidence','uncertain','finishing') OR (p.status='collecting' AND p.closed AND p.ready_at<=now()) ORDER BY p.id DESC LIMIT 9 OFFSET $1",
        offset,
    )
    keys = [
        [
            button(
                f"#{r['id']} {STATUS_LABELS.get(r['status'], r['status'])} · {r['title'] or 'без категории'}",
                f"item:{r['id']}",
            )
        ]
        for r in rows[:8]
    ]
    if len(rows) > 8:
        keys.append([button("Далее", f"reviews:{offset + 8}")])
    keys.append([button("🏠 Меню", "home")])
    await tg.say(
        chat,
        "Выберите товар → нажмите категорию: весь товар сразу отправится в её канал. Квота AI для этого не нужна. Новые товары сверху. Неопределённые отправки требуют сверки.",
        keys,
    )


async def item(db, tg, chat, pid):
    p = await db.fetchrow("SELECT * FROM products WHERE id=$1", pid)
    if not p:
        return
    if await db.fetchval('SELECT 1 FROM product_deliveries WHERE product_id=$1',pid):
        return await routing_admin.show_delivery(db,tg,chat,p)
    if p["status"] == "finishing":
        return await tg.say(
            chat,
            f"#{pid}: товар скопирован, завершается восстановление URL-кнопок. Повторной отправки товара не будет.\n{p['error'] or ''}",
            [
                [button("Повторить восстановление кнопок", f"finish:{pid}")],
                [button("Меню", "home")],
            ],
        )
    if p["status"] == "uncertain":
        destination_title=await db.fetchval("SELECT title FROM available_channels WHERE id=$1",p["destination_channel_id"]) or "Название недоступно"
        await tg.say(
            chat,
            f"#{pid}: исход отправки неизвестен. Канал: {destination_title} ({p['destination_channel_id']})\n{p['error']}\nСверьте наличие ВСЕГО товара в канале. Автоповтор запрещён.",
            [
                [button("Весь товар уже есть", "reconcile_yes:" + str(pid))],
                [button("Товара точно нет", "reconcile_no:" + str(pid))],
                [button("Меню", "home")],
            ],
        )
        return
    ai = p["ai_result"] or {}
    mid = p["source_message_id"]
    sid = str(p["source_channel_id"]).removeprefix("-100")
    post_text = "\n".join(m.get("caption") or m.get("text", "") for m in p["messages"])
    category = await db.fetchrow(
        "SELECT title,department FROM categories WHERE code=$1", p["final_category"]
    )
    original_title = (
        await db.fetchval(
            "SELECT title FROM categories WHERE code=$1", p["ai_category"]
        )
        or "Не определено"
    )
    departments = {
        "women": "Женское",
        "men": "Мужское",
        "accessories": "Обувь и аксессуары",
    }
    final_title = (
        (
            departments.get(category["department"], category["department"])
            + " → "
            + category["title"]
        )
        if category
        else "Не определено"
    )
    confidence = (
        f"{p['confidence']:.0%}" if p["confidence"] is not None else "не определена"
    )
    photo_count = sum(bool(m.get("photo")) for m in p["messages"])
    video_count = sum(bool(m.get("video")) for m in p["messages"])
    text = f"Товар #{pid}\nAI предложил: {original_title}\nИтог: {final_title}\nУверенность AI: {confidence}\nФото: {photo_count} · видео: {video_count} · сообщений: {len(p['messages'])}\n{('Не удалось распознать: квота Google исчерпана. Выберите канал кнопкой ниже.' if p['error'] and '429' in p['error'] else ('Не удалось обработать автоматически. Выберите канал вручную.' if p['error'] and not source_unavailable(p) else ''))}\n\nПубликация:\n{post_text[:1800]}\nОригинал: https://t.me/c/{sid}/{mid}"
    if source_unavailable(p):text+='\n\n⚠️ '+source_unavailable_text()
    selected=selected_categories(p)
    if len(selected)>1:
        names=[]
        for code in selected:
            title=await db.fetchval('SELECT title FROM categories WHERE code=$1',code) or code
            extra=next((r for r in ai.get('additional_categories',[]) if r['category']==code),None)
            names.append(title+(f" ({extra['confidence']:.0%})" if extra else ''))
        text+='\nКатегории для отправки: '+', '.join(names)
    text += await omitted_text(db,p)
    text += f"\nСтатус: {STATUS_LABELS.get(p['status'],p['status'])}"
    if p['status']=='published' and p['destination_channel_id'] and p['destination_message_ids']:
        target=await db.fetchval('SELECT title FROM available_channels WHERE id=$1',p['destination_channel_id']) or final_title
        link=f"https://t.me/c/{str(p['destination_channel_id']).removeprefix('-100')}/{p['destination_message_ids'][0]}"
        text+=f"\nОпубликовано → {target}\n{link}"
    preview = []
    for message in p["messages"]:
        if message.get("photo"):
            preview.append({"type": "photo", "media": message["photo"][-1]["file_id"]})
        elif message.get("video"):
            preview.append({"type": "video", "media": message["video"]["file_id"]})
    try:
        for offset in range(0, len(preview), 10):
            chunk = preview[offset : offset + 10]
            if len(chunk) > 1:
                await tg.call("send_media_group", chat_id=chat, media=chunk)
            elif chunk:
                kind = chunk[0]["type"]
                await tg.call(
                    "send_photo" if kind == "photo" else "send_video",
                    chat_id=chat,
                    **{kind: chunk[0]["media"]},
                )
        for message in p["messages"]:
            if message.get("document"):
                await tg.call(
                    "send_document",
                    chat_id=chat,
                    document=message["document"]["file_id"],
                )
    except Exception:
        text += "\nПревью загружено не полностью. Откройте оригинал перед решением."
    keys=[[button("🏠 Меню","home")]]
    if p['status'] in ('review','low_confidence') or (p['status']=='collecting' and p['closed'] and p['ready_at']<=datetime.now(timezone.utc)):
        if not source_unavailable(p):text+='\n\nДля одного канала нажмите категорию. Для нескольких откройте «Несколько категорий» и отметьте подходящие.'
        keys=await route_keyboard(db,p)
        cat=await db.fetchrow('SELECT * FROM categories WHERE code=$1 AND enabled AND destination IS NOT NULL',p['final_category'])
        if len(selected)>1:
            keys.insert(0,[button(f'✅ Во все выбранные категории ({len(selected)})',f"approveroutes:{pid}")])
        elif cat:keys.insert(0,[button('✅ В '+cat['title'],f"route:{pid}:{cat['id']}")])
        keys += [[button('❌ Пропустить',f'skip:{pid}'),button('🔄 Повторить AI',f'reanalyze:{pid}')], [button('Очередь','reviews:0'),button('Меню','home')]]
    if source_unavailable(p):keys=[[button('❌ Пропустить',f'skip:{pid}')],[button('Очередь','reviews:0'),button('Меню','home')]]
    msg = await tg.say(chat,text,keys)
    return msg


async def handle(db, tg, u):
    cb = u.get("callback_query")
    m = cb.get("message", {}) if cb else u.get("message", {})
    actor = (cb or m).get("from", {}).get("id")
    chat = m.get("chat", {}).get("id")
    if not actor or not chat:
        return
    if actor not in await admins(db):
        if cb:
            await tg.call(
                "answer_callback_query",
                callback_query_id=cb["id"],
                text="Доступ только администратору",
                show_alert=True,
            )
        elif m.get("text", "").startswith("/"):
            await tg.say(chat, "Доступ только администратору E-MODA.")
        return
    # Administrative changes accepted only in the admin's private chat.
    if m.get("chat", {}).get("type") != "private":
        return
    input_error = None
    try:
        text = message_text(m) if not cb else ""
    except ValueError as exc:
        text, input_error = "", str(exc)
    if cb:
        await tg.call("answer_callback_query", callback_query_id=cb["id"])
        data = cb["data"]
        parts = data.split(":")
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        if await multi_admin.callback(db,tg,actor,chat,cmd,parts):
            return
        if await routing_admin.callback(db,tg,actor,chat,cmd,parts,ask):
            return
        if await ai_admin.callback(db,tg,chat,cmd,parts):
            return
        if cmd == "home":
            await show_home(db,tg,chat)
        elif cmd == 'aihelp':
            await ai_help(db,tg,chat)
        elif cmd == 'reviewme':
            await put(db,'review_admin_id',actor)
            await put(db,'review_notify_from',time.time())
            await put(db,'notify_enabled',True)
            await tg.say(chat,'Новые товары для ручного разбора будут приходить сюда. Накопленные доступны в «Проверка товаров».',[[button('Проверка товаров','reviews:0'),button('Меню','home')]])
        elif cmd == 'routepage':
            p=await db.fetchrow('SELECT * FROM products WHERE id=$1',int(arg))
            if not p or not (p['status'] in ('review','low_confidence') or (p['status']=='collecting' and p['closed'] and p['ready_at']<=datetime.now(timezone.utc))):raise ValueError('Товар уже обработан или отправляется')
            if source_unavailable(p):raise ValueError(source_unavailable_text())
            await tg.say(chat,f"#{p['id']}: нажмите категорию для немедленной отправки всего товара:",await route_keyboard(db,p,parts[2],int(parts[3])))
        elif cmd == 'route':
            cat=await queue_manual(db,int(arg),int(parts[2]),actor)
            await tg.say(chat,f"#{arg}: весь товар поставлен на отправку → {cat['title']}. После отправки придёт подтверждение.",[[button('Товар',f'item:{arg}'),button('Очередь','reviews:0')]])
        elif cmd == 'published':
            rows=await db.fetch("SELECT p.id,c.title FROM products p JOIN sources s ON s.id=p.source_channel_id LEFT JOIN categories c ON c.code=p.final_category WHERE s.enabled AND p.status='published' ORDER BY p.updated_at DESC LIMIT 10")
            await tg.say(chat,'Последние опубликованные товары:',[[button(f"#{r['id']} → {r['title']}",f"item:{r['id']}")] for r in rows]+[[button('Меню','home')]])
        elif cmd == 'cached':
            count=await cached_count(db)
            await tg.say(chat,f"Готовы к AUTO: {count} товаров. Основная категория распознана с уверенностью не ниже порога AUTO, её канал подключён. Неуверенные дополнительные категории не мешают основной; они не будут отправлены автоматически. Ручные изменения, ошибки и неоднозначные отправки исключены. Повторного запроса к AI и расходов не будет.",[[button(f'Отправить {count} товаров','resumecached')],[button('Меню','home')]])
        elif cmd == 'resumecached':
            ids=await resume_cached(db)
            await tg.say(chat,f'Поставлено на автоматическую отправку: {len(ids)}. Прогресс виден в меню и последних публикациях.',[[button('Меню','home')]])
        elif cmd == "settings":
            await settings_menu(db, tg, chat)
        elif cmd == "retryai":
            await reset_ai(db)
            await ai_admin.overview(db,tg,chat)
        elif cmd == "notify":
            await put(
                db, "notify_enabled", not await setting(db, "notify_enabled", True)
            )
            await settings_menu(db, tg, chat)
        elif cmd == "pause":
            await put(db, "paused", not await setting(db, "paused", True))
            await settings_menu(db, tg, chat)
        elif cmd == "mode":
            await put(db, "mode", arg)
            await settings_menu(db, tg, chat)
        elif cmd == "config":
            prompts = {
                "ai_rpm": "Введите общий лимит запросов AI в минуту (1–10000). На текущем бесплатном доступе безопасный старт — 12 при квоте 15. Этот лимит учитывает и повторные HTTP-запросы.",
                "gemini_key": "Пришлите API-ключ Google для Gemma. Сообщение с ключом будет удалено, значение не показывается в меню.",
                "mistral_key": "Пришлите API-ключ Mistral из аккаунта Free mode. При платном доступе Mistral может списывать средства; бот не определяет ваш тариф. Сообщение будет удалено.",
                "openrouter_key": "Пришлите API-ключ OpenRouter. Бот использует только разрешённые :free модели с ценой токенов 0. Сообщение будет удалено.",
                "nvidia_key": "Пришлите API-ключ NVIDIA NIM (nvapi-...). Сообщение с ключом будет удалено, значение не показывается в меню.",
                "auto_threshold": "Введите порог AUTO от 0 до 1.",
                "review_threshold": "Введите нижний порог REVIEW от 0 до 1.",
                "model": "Введите ID бесплатной Gemma: gemma-4-26b-a4b-it или gemma-4-31b-it.",
                "admins": "Введите Telegram ID администраторов через пробел. Ваш ID должен остаться в списке.",
                "input_price": "Тариф за 1 млн входных токенов, USD.",
                "output_price": "Тариф за 1 млн выходных токенов, USD.",
            }
            await ask(db, tg, actor, chat, "config", prompts[arg], {"key": arg})
        elif cmd == "tariffs":
            await tg.say(
                chat,
                f"Тариф, USD / 1 млн токенов: вход {await setting(db, 'input_price', 'не задан')}, выход {await setting(db, 'output_price', 'не задан')}. Стоимость — расчёт по usage, итоговый счёт поставщика может отличаться.",
                [
                    [
                        button("Вход", "config:input_price"),
                        button("Выход", "config:output_price"),
                    ],
                    [button("Назад", "settings")],
                ],
            )
        elif cmd == "stats":
            counts = await db.fetch(
                "SELECT status,count(*) n FROM products GROUP BY status ORDER BY status"
            )
            summary = await db.fetchrow(
                "SELECT count(*) n,sum((usage->>'promptTokenCount')::bigint) inp,sum((usage->>'candidatesTokenCount')::bigint) outp,round(avg(latency_ms)) latency,sum(estimated_cost_usd) cost FROM products WHERE ai_result IS NOT NULL"
            )
            await tg.say(
                chat,
                "Товары:\n"
                + "\n".join(
                    f"{STATUS_LABELS.get(r['status'], r['status'])}: {r['n']}"
                    for r in counts
                )
                + f"\nAI: {summary['n']}, tokens in/out {summary['inp'] or 0}/{summary['outp'] or 0}, задержка {summary['latency'] or 0} мс\nРасчётная стоимость USD: {summary['cost'] if summary['cost'] is not None else 'нет тарифа'}\nИсправлений: {await db.fetchval("SELECT count(*) FROM corrections WHERE action='change_category'")}",
                [[button("Меню", "home")]],
            )
        elif cmd == "sources":
            offset=int(arg or 0)
            rows = await db.fetch("SELECT * FROM sources ORDER BY id LIMIT 11 OFFSET $1",offset)
            await tg.say(
                chat,
                "Основные каналы. Сортировщик только читает публикации.",
                [
                    [
                        button(
                            ("✅ " if s["enabled"] else "⏸ ") + s["title"],
                            f"source:{s['id']}",
                        )
                    ]
                    for s in rows[:10]
                ]
                + ([[button("Далее",f"sources:{offset+10}")]] if len(rows)>10 else [])
                + [[button("➕ Источник", "addsource")], [button("Меню", "home")]],
            )
        elif cmd == "addsource":
            await ask(
                db,
                tg,
                actor,
                chat,
                "addsource",
                "Добавьте бота администратором исходного канала. Выберите канал ниже либо пришлите @username, ID -100… или перешлите публикацию.",
            )
            await channel_picker(db, tg, chat, "source")
        elif cmd == "source":
            s = await db.fetchrow("SELECT * FROM sources WHERE id=$1", int(arg))
            try:
                me = await tg.call("get_me")
                access = await tg.call(
                    "get_chat_member", chat_id=s["id"], user_id=me["id"]
                )
                access_text = access["status"]
            except Exception:
                access_text = "нет доступа: добавьте бота администратором"
            await tg.say(
                chat,
                f"{s['title']}\nID {s['id']}\nДоступ: {access_text}\nОтдел: {s['department']}\nСборка: {s['style']}\nВключён: {s['enabled']}",
                [
                    [button("Включить/выключить", "togglesource:" + arg)],
                    [
                        button("Изменить название", "namesource:" + arg),
                        button("Отдел", "deptsource:" + arg),
                    ],
                    [button("Альбом / autopost", "stylesource:" + arg)],
                    [button("Удалить источник", "delsource:" + arg)],
                    [button("Назад", "sources")],
                ],
            )
        elif cmd == "togglesource":
            await db.execute(
                "UPDATE sources SET enabled=NOT enabled WHERE id=$1", int(arg)
            )
            await tg.say(chat, "Готово", [[button("Назад", "sources")]])
        elif cmd == "stylesource":
            await db.execute(
                "UPDATE sources SET style=CASE WHEN style='autopost' THEN 'album' ELSE 'autopost' END WHERE id=$1",
                int(arg),
            )
            await tg.say(
                chat,
                "Стиль изменён. autopost: альбомы + завершающий текст. album: один media_group_id = один товар.",
                [[button("Назад", "sources")]],
            )
        elif cmd == "deptsource":
            await ask(
                db,
                tg,
                actor,
                chat,
                "deptsource",
                "Отдел источника: women / men / accessories / unknown. Это подсказка для неоднозначных изображений.",
                {"id": int(arg)},
            )
        elif cmd == "namesource":
            await ask(
                db, tg, actor, chat, "namesource", "Введите название.", {"id": int(arg)}
            )
        elif cmd == "delsource":
            await db.execute("DELETE FROM sources WHERE id=$1", int(arg))
            await tg.say(
                chat,
                "Источник удалён. История товаров сохранена.",
                [[button("Назад", "sources")]],
            )
        elif cmd == "categories":
            offset = int(arg)
            cats = await db.fetch(
                "SELECT * FROM categories ORDER BY id LIMIT 11 OFFSET $1", offset
            )
            keys = [
                [
                    button(
                        ("✅ " if c["enabled"] else "⏸ ") + c["title"],
                        f"category:{c['id']}",
                    )
                ]
                for c in cats[:10]
            ]
            if len(cats) > 10:
                keys.append([button("Далее", f"categories:{offset + 10}")])
            keys += [[button("➕ Категория", "addcat")], [button("Меню", "home")]]
            await tg.say(
                chat,
                "✅ — канал работает; ⏸ — категория выключена. Для новой категории: откройте её → «Подключить и включить канал» → выберите канал. Приватную ссылку вводить не нужно, если бот уже администратор.",
                keys,
            )
        elif cmd == "channels":
            await channel_picker(db, tg, chat, arg, int(parts[2]), int(parts[3]))
        elif cmd == "pickchannel":
            kind = arg
            cid = int(parts[2])
            target = int(parts[3])
            channel = await resolve(db, tg, {}, str(target), source=kind == "source")
            if kind == "source":
                await db.execute(
                    "INSERT INTO sources(id,title) VALUES($1,$2) ON CONFLICT(id) DO UPDATE SET title=$2,enabled=true",
                    channel["id"],
                    channel["title"],
                )
            else:
                await bind_category(db,cid,channel)
            await db.execute("DELETE FROM admin_state WHERE actor=$1", actor)
            await tg.say(chat, "Канал подключён, категория включена. В AUTO уверенные товары отправляются автоматически.", home())
        elif cmd == "category":
            c = await db.fetchrow("SELECT * FROM categories WHERE id=$1", int(arg))
            destination_title=await db.fetchval("SELECT title FROM available_channels WHERE id=$1",c["destination"]) or "Не подключён"
            await tg.say(
                chat,
                f"{c['title']}\n{c['code']} / {c['department']}\n{('Справочное описание (это дополнительная категория):' if c['code']=='women.plus_size' else 'Действующее правило AI:')}\n{c['description']}\nКанал: {destination_title} ({c['destination'] or 'не привязан'})\n{c['link'] or ''}\nВключена: {c['enabled']}" + (f"\nТолько отдельные вещи (AUTO): {'да — комплекты исключены' if c['single_only'] else 'нет — состав продажи не ограничен'}." if c['code']!='women.plus_size' else ''),
                [
                    [button("Включить/выключить", "togglecat:" + arg)],
                    [button("Подключить и включить канал", "destcat:" + arg)],
                    [
                        button("Название", "namecat:" + arg),
                        button("Описание" if c["code"]=="women.plus_size" else "Правило AI", "desccat:" + arg),
                    ],
                    *([[button("📏 Правило больших размеров", "plusrule")]] if c["code"]=="women.plus_size" else [[button("🧩 Только отдельные вещи: "+('ДА' if c['single_only'] else 'НЕТ'), "singlecat:"+arg)]]),
                    [button("Отключить и убрать канал", "clearcat:" + arg)],
                    [button("Назад", "categories:0")],
                ],
            )
        elif cmd == "singlecat":
            c=await db.fetchrow("UPDATE categories SET single_only=NOT single_only WHERE id=$1 AND code<>'women.plus_size' RETURNING *",int(arg))
            if not c:raise ValueError('Категория не найдена или использует отдельное правило больших размеров')
            await tg.say(chat,c['title']+'\nТолько отдельные вещи (AUTO): '+('ДА. Комплекты и их части исключены; отдельная вещь может относиться к нескольким совместимым категориям.' if c['single_only'] else 'НЕТ. Комплекты допускаются, если соответствуют правилу категории.')+'\nНастройка сохранена. Ручной выбор администратора остаётся доступен.',[[button('Категория','category:'+arg)]])
        elif cmd == "togglecat":
            c = await db.fetchrow("SELECT * FROM categories WHERE id=$1", int(arg))
            if not c["enabled"]:
                if not c["destination"]:
                    raise ValueError("Сначала привяжите канал")
                await resolve(db, tg, {}, str(c["destination"]))
            await db.execute(
                "UPDATE categories SET enabled=NOT enabled WHERE id=$1", int(arg)
            )
            await tg.say(chat, "Готово", [[button("Категория", "category:" + arg)]])
        elif cmd == "clearcat":
            await db.execute(
                "UPDATE categories SET enabled=false,destination=NULL WHERE id=$1",
                int(arg),
            )
            await tg.say(
                chat,
                "Канал отвязан; история сохранена.",
                [[button("Назад", "category:" + arg)]],
            )
        elif cmd in ("destcat", "namecat", "desccat"):
            category = await db.fetchrow("SELECT * FROM categories WHERE id=$1", int(arg))
            if not category:
                raise ValueError("Категория не найдена. Откройте актуальный список категорий.")
            prompts = {
                "destcat": "Добавьте бота администратором с правом публикации. Пришлите @username, ID -100… или перешлите сообщение.",
                "namecat": "Введите название категории.",
                "desccat": (
                    "Текущее справочное описание. Правило отправки задаётся в «Правило больших размеров»:\n"
                    if category['code']=='women.plus_size' else "Текущее правило AI:\n"
                ) + (category['description'] or 'Не задано') + "\n\nНовый текст ЗАМЕНИТ только правило этой категории целиком. Остальные правила сохранятся. Для дополнения включите и прежний текст. Укажите, какие товары относятся к категории и чем отличаются от соседних. До 1500 символов.",
            }
            await ask(db, tg, actor, chat, cmd,
                      category['title'] + "\n\n" + prompts[cmd], {"id": int(arg)})
            if cmd == "destcat":
                await channel_picker(db, tg, chat, "destination", int(arg))
        elif cmd == "addcat":
            await ask(
                db, tg, actor, chat, "newcat_name", "Введите название новой категории."
            )
        elif cmd == "finish":
            await db.execute(
                "UPDATE products SET ready_at=now() WHERE id=$1 AND status='finishing'",
                int(arg),
            )
            await tg.say(
                chat,
                "Запрошено восстановление кнопок. Товар повторно не отправляется.",
                [[button("Товар", f"item:{arg}")]],
            )
        elif cmd == "reviews":
            await reviews(db, tg, chat, int(arg))
        elif cmd == "item":
            await item(db, tg, chat, int(arg))
        elif cmd in (
            "approve",
            "skip",
            "choose",
            "setcat",
            "reconcile_yes",
            "reconcile_no",
            "reanalyze",
        ):
            pid = int(arg)
            p = await db.fetchrow("SELECT * FROM products WHERE id=$1", pid)
            if not p:
                raise ValueError("Товар не найден")
            if await db.fetchval('SELECT 1 FROM product_deliveries WHERE product_id=$1',pid):
                if cmd=='skip':
                    from .deliveries import skip_unstarted
                    await skip_unstarted(db,pid,actor)
                    await routing_admin.show_delivery(db,tg,chat,await db.fetchrow('SELECT * FROM products WHERE id=$1',pid))
                    return
                raise ValueError('У товара есть отправки по каналам. Откройте карточку: повтор или сверка доступны отдельно для каждого канала.')
            if cmd.startswith("reconcile_"):
                target = "published" if cmd == "reconcile_yes" else "review"
                changed = await db.fetchval(
                    "UPDATE products SET status=$2,error=$3,updated_at=now() WHERE id=$1 AND status='uncertain' RETURNING id",
                    pid,
                    target,
                    "Администратор выполнил ручную сверку",
                )
                if changed:
                    await db.execute(
                        "INSERT INTO corrections(product_id,ai_category,final_category,actor,action) VALUES($1,$2,$3,$4,$5)",
                        pid,
                        p["ai_category"],
                        p["final_category"],
                        actor,
                        cmd,
                    )
            elif p["status"] not in ("review", "low_confidence"):
                raise ValueError(
                    "Товар уже обработан или отправляется; повтор запрещён"
                )
            elif cmd == "reanalyze":
                await db.execute(
                    "UPDATE products SET status='collecting',final_categories=NULL,ready_at=now(),review_message_id=NULL,error=NULL,updated_at=now() WHERE id=$1 AND status IN ('review','low_confidence') AND destination_message_ids IS NULL",
                    pid,
                )
            elif cmd == "choose":
                cats = await db.fetch("SELECT * FROM categories ORDER BY id")
                await tg.say(
                    chat,
                    f"Категория для #{pid}:",
                    [[button(c["title"], f"setcat:{pid}:{c['id']}")] for c in cats],
                )
                return
            elif cmd == "setcat":
                c = await db.fetchrow(
                    "SELECT * FROM categories WHERE id=$1", int(parts[2])
                )
                async with db.acquire() as conn, conn.transaction():
                    changed = await conn.fetchval(
                        "UPDATE products SET final_category=$2,final_categories=jsonb_build_array($2::text),status='review',updated_at=now() WHERE id=$1 AND status IN ('review','low_confidence') RETURNING id",
                        pid,
                        c["code"],
                    )
                    if changed:
                        await conn.execute(
                            "INSERT INTO corrections(product_id,ai_category,final_category,actor,action) VALUES($1,$2,$3,$4,'change_category')",
                            pid,
                            p["ai_category"],
                            c["code"],
                            actor,
                        )
            else:
                if cmd == 'approve':
                    cid=await db.fetchval('SELECT id FROM categories WHERE code=$1',p['final_category'])
                    await queue_manual_many(db,pid,None,actor)
                    await tg.say(chat,f'#{pid}: товар поставлен на отправку. После публикации придёт подтверждение.',[[button('Товар',f'item:{pid}'),button('Очередь','reviews:0')]])
                    return
                target = "skipped"
                if target == "queued":
                    cat = await db.fetchrow(
                        "SELECT * FROM categories WHERE code=$1 AND enabled",
                        p["final_category"],
                    )
                    if not cat or not cat["destination"]:
                        raise ValueError(
                            "Включите категорию и привяжите канал через /admin → Категории"
                        )
                async with db.acquire() as conn, conn.transaction():
                    changed = await conn.fetchval(
                        "UPDATE products SET status=$2,ready_at=now(),updated_at=now() WHERE id=$1 AND status IN ('review','low_confidence') RETURNING id",
                        pid,
                        target,
                    )
                    if changed:
                        await conn.execute(
                            "INSERT INTO corrections(product_id,ai_category,final_category,actor,action) VALUES($1,$2,$3,$4,$5)",
                            pid,
                            p["ai_category"],
                            p["final_category"],
                            actor,
                            cmd,
                        )
            await tg.say(
                chat,
                f"Решение по #{pid} сохранено.",
                [[button("Товар", f"item:{pid}"), button("Очередь", "reviews:0")]],
            )
        return
    if text == '/rules':
        await routing_admin.all_rules(db,tg,chat)
        return
    if text in ("/start", "/admin", "/cancel"):
        await db.execute("DELETE FROM admin_state WHERE actor=$1", actor)
        await show_home(db,tg,chat)
        return
    state = await db.fetchrow("SELECT * FROM admin_state WHERE actor=$1", actor)
    if not state:
        return
    action = state["action"]
    data = state["data"]
    is_key = action == "config" and data.get("key") in KEYS.values()
    # Forwarded channel posts can bind a channel without a written body.
    forwarded_channel = action in ("addsource", "destcat") and m.get("forward_origin", {}).get("chat")
    if not is_key and not forwarded_channel and (input_error or not text):
        raise ValueError(input_error or "Пришлите непустой текст. Прежнее значение сохранено; /cancel — отмена.")
    if await routing_admin.text_input(db,tg,actor,chat,action,text):
        return
    if action == "config":
        key = data["key"]
        if key in ("auto_threshold", "review_threshold", "input_price", "output_price"):
            value = float(text.replace(",", "."))
            if not (value >= 0 and value < 10000):
                raise ValueError("Некорректное число")
            if key.endswith("threshold"):
                if value > 1:
                    raise ValueError("Порог должен быть от 0 до 1")
                other = await setting(
                    db,
                    "review_threshold" if key == "auto_threshold" else "auto_threshold",
                    0.6 if key == "auto_threshold" else 0.85,
                )
                if (key == "auto_threshold" and value < other) or (
                    key == "review_threshold" and value > other
                ):
                    raise ValueError("Порог REVIEW не может быть выше AUTO")
        elif key == "ai_rpm":
            value = int(text)
            if not 1 <= value <= 10000:
                raise ValueError("Лимит должен быть от 1 до 10000 запросов/мин")
        elif key in KEYS.values():
            # Remove even invalid input from durable updates before validation.
            if u.get('update_id') is not None:
                await db.execute("UPDATE updates SET payload=jsonb_set(payload #- '{message,rich_message}' #- '{message,caption}' #- '{message,entities}' #- '{message,caption_entities}','{message,text}',to_jsonb('[API key removed]'::text)) WHERE id=$1",u['update_id'])
            try:
                await tg.call('delete_message',chat_id=chat,message_id=m['message_id'])
            except Exception:
                await tg.say(chat,'Не удалось удалить сообщение с ключом. Удалите его вручную в чате.')
            if not re.fullmatch(r'[A-Za-z0-9_\-]{20,200}',text):
                raise ValueError('Некорректный API-ключ; попробуйте ещё раз')
            value=text
        elif key == "admins":
            value = [int(x) for x in text.split()]
            if actor not in value:
                raise ValueError("Ваш ID должен остаться в списке")
        else:
            if key!='model' or text not in MODELS['google']:
                raise ValueError("Выберите бесплатную Gemma через AI сервисы")
            value = text
        await put(db, key, value)
        if key in KEYS.values():
            name=next(n for n,k in KEYS.items() if k==key)
            await put(db,name+'_enabled',True)
            await reset_ai(db,name)
            await db.execute('DELETE FROM admin_state WHERE actor=$1',actor)
            await ai_admin.provider_menu(db,tg,chat,name)
            return
        if key == 'model':
            await reset_ai(db,'google')
            await put(db,'input_price',0)
            await put(db,'output_price',0)
    elif action == "addsource":
        channel = await resolve(db, tg, m, text, source=True)
        await db.execute(
            "INSERT INTO sources(id,title) VALUES($1,$2) ON CONFLICT(id) DO UPDATE SET title=$2,enabled=true",
            channel["id"],
            channel["title"],
        )
    elif action == "deptsource":
        if text not in ("women", "men", "accessories", "unknown"):
            raise ValueError("Введите women, men, accessories или unknown")
        await db.execute(
            "UPDATE sources SET department=$2 WHERE id=$1", data["id"], text
        )
    elif action == "namesource":
        await db.execute(
            "UPDATE sources SET title=$2 WHERE id=$1", data["id"], text[:200]
        )
    elif action == "destcat":
        channel = await resolve(db, tg, m, text)
        await bind_category(db,data["id"],channel)
    elif action in ("namecat", "desccat"):
        if len(text) > 1500:
            raise ValueError("Правило должно быть не длиннее 1500 символов. Прежнее значение сохранено.")
        column = "title" if action == "namecat" else "description"
        saved = await db.fetchrow(
            f"UPDATE categories SET {column}=$2 WHERE id=$1 RETURNING title,description",
            data["id"], text,
        )
        if not saved:
            raise ValueError("Категория не найдена. Ничего не сохранено; откройте актуальный список категорий.")
        await db.execute("DELETE FROM admin_state WHERE actor=$1", actor)
        await tg.say(chat, f"Сохранено: {saved['title']}\n\n" + (
            "Правило AI:\n" + saved['description'] if action == "desccat" else "Название категории обновлено."
        ), [[button("Открыть категорию", f"category:{data['id']}")],
            [button("Все заданные правила AI", "airulesall:0"), button("Меню", "home")]])
        return
    elif action == "newcat_name":
        await ask(
            db,
            tg,
            actor,
            chat,
            "newcat_department",
            "Отдел: women / men / accessories",
            {"title": text[:200]},
        )
        return
    elif action == "newcat_department":
        if text not in ("women", "men", "accessories"):
            raise ValueError("Введите women, men или accessories")
        await ask(
            db,
            tg,
            actor,
            chat,
            "newcat_description",
            "Опишите товары для этой категории и их отличия.",
            {**data, "department": text},
        )
        return
    elif action == "newcat_description":
        cid = await db.fetchval(
            "SELECT nextval(pg_get_serial_sequence('categories','id'))"
        )
        await db.execute(
            "INSERT INTO categories(id,code,title,department,description) VALUES($1,$2,$3,$4,$5)",
            cid,
            data["department"] + ".custom_" + str(cid),
            data["title"],
            data["department"],
            text[:1500],
        )
    else:
        raise ValueError("Это действие устарело. Откройте нужную настройку заново; ничего не сохранено.")
    await db.execute("DELETE FROM admin_state WHERE actor=$1", actor)
    await tg.say(chat, "Сохранено.", home())
