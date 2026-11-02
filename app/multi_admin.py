"""Per-admin durable selection; a click on a checkbox never publishes anything."""
from .operations import route_draft, queue_manual_many


def b(text, data):return {'text':text,'callback_data':data}


async def picker(db,tg,actor,chat,pid,department='all',offset=0,toggle=None):
    p,ids=await route_draft(db,pid,actor,toggle)
    if department not in ('all','women','men','accessories'):raise ValueError('Неизвестный отдел')
    cats=await db.fetch("SELECT * FROM categories WHERE enabled AND destination IS NOT NULL AND ($1='all' OR department=$1) ORDER BY id LIMIT 21 OFFSET $2",department,offset)
    selected=await db.fetch('SELECT id,title,destination FROM categories WHERE id=ANY($1::int[]) ORDER BY array_position($1::int[],id)',ids)
    lines='\n'.join('✓ '+c['title'] for c in selected) or 'Пока ничего не выбрано.'
    text=f"Товар #{pid} · несколько категорий\n\nОтметьте все подходящие категории галочками. Выбор пока ничего не отправляет. Первая выбранная категория — основная.\n\nВыбрано:\n{lines}\n\nНажмите «Отправить выбранное» — весь товар с фотографиями и текстом попадёт в каждый выбранный канал. Совпадающие каналы получат одну копию. Большие размеры по действующему правилу добавляются автоматически."
    keys=[[b(('☑️ ' if c['id'] in ids else '⬜️ ')+c['title'][:40],f"multitoggle:{pid}:{c['id']}:{department}:{offset}")] for c in cats[:20]]
    nav=[]
    if offset:nav.append(b('←',f'multiroute:{pid}:{department}:{max(0,offset-20)}'))
    if len(cats)>20:nav.append(b('Далее →',f'multiroute:{pid}:{department}:{offset+20}'))
    if nav:keys.append(nav)
    keys.append([b('Женское',f'multiroute:{pid}:women:0'),b('Мужское',f'multiroute:{pid}:men:0'),b('Все',f'multiroute:{pid}:all:0')])
    if ids:keys.append([b('✅ Отправить выбранное',f'multisend:{pid}')])
    keys.append([b('Назад к товару',f'item:{pid}')])
    await tg.say(chat,text,keys)


async def callback(db,tg,actor,chat,cmd,parts):
    if cmd=='multiroute':
        await picker(db,tg,actor,chat,int(parts[1]),parts[2] if len(parts)>2 else 'all',int(parts[3]) if len(parts)>3 else 0)
    elif cmd=='multitoggle':
        await picker(db,tg,actor,chat,int(parts[1]),parts[3],int(parts[4]),int(parts[2]))
    elif cmd in ('multisend','approveroutes'):
        pid=int(parts[1]);cats=await queue_manual_many(db,pid,None,actor,use_draft=cmd=='multisend')
        await tg.say(chat,f"#{pid}: весь товар поставлен на отправку в выбранные категории:\n"+'\n'.join('→ '+c['title'] for c in cats)+'\nПосле отправки придёт подтверждение по каналам.',[[b('Товар',f'item:{pid}'),b('Очередь','reviews:0')]])
    else:return False
    return True
