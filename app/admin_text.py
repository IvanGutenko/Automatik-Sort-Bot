"""Read complete written admin input, including Telegram Rich Messages.

Unsupported blocks are rejected as a whole: saving a partial rule loses intent.
"""


def _rich_text(value, depth=0):
    if depth > 32:
        raise ValueError('Слишком сложное форматирование текста')
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(_rich_text(v, depth + 1) for v in value)
    if isinstance(value, dict):
        if 'text' in value:
            return _rich_text(value['text'], depth + 1)
        if value.get('type') == 'custom_emoji':
            return _rich_text(value['alternative_text'], depth + 1)
        if value.get('type') == 'mathematical_expression':
            return _rich_text(value['expression'], depth + 1)
        if value.get('type') == 'anchor':
            return ''
    raise ValueError('Неподдерживаемое форматирование текста')


def _blocks(blocks, depth=0):
    if depth > 32 or not isinstance(blocks, list):
        raise ValueError('Неподдерживаемый формат сообщения')
    lines = []
    for block in blocks:
        kind = block['type']
        if kind in ('paragraph', 'heading', 'pre', 'footer'):
            lines.append(_rich_text(block['text']))
        elif kind == 'list':
            items = block['items']
            if not isinstance(items, list):
                raise ValueError('Некорректный список')
            for item in items:
                label = _rich_text(item.get('label', ''))
                body = _blocks(item['blocks'], depth + 1)
                lines.append((label + ' ' if label else '') + body)
        elif kind == 'blockquote':
            lines.append(_blocks(block['blocks'], depth + 1))
            if block.get('credit'):
                lines.append(_rich_text(block['credit']))
        elif kind in ('expandable_blockquote', 'pullquote'):
            lines.append(_rich_text(block['text']))
            if block.get('credit'):
                lines.append(_rich_text(block['credit']))
        elif kind == 'mathematical_expression':
            lines.append(_rich_text(block['expression']))
        elif kind == 'divider':
            lines.append('')
        elif kind != 'anchor':
            raise ValueError('Пришлите правило текстом: сообщение содержит неподдерживаемые блоки')
    return '\n'.join(lines)


def message_text(message):
    """Return all written content or raise; never silently save partial rich input."""
    try:
        if message.get('rich_message') is not None:
            text = _blocks(message['rich_message']['blocks'])
        else:
            text = message.get('text') or message.get('caption') or ''
        if not isinstance(text, str):
            raise ValueError('Ожидается текст')
        return text.strip()
    except (KeyError, TypeError, RecursionError) as exc:
        raise ValueError('Не удалось прочитать сообщение. Пришлите правило обычным текстом.') from exc
