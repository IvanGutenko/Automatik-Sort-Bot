import io, os
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter


class Telegram:
    def __init__(self):
        self.bot = Bot(os.environ["SORT_BOT_TOKEN"])

    async def call(self, method, **kw):
        result = await getattr(self.bot, method)(**kw)

        def plain(v):
            if hasattr(v, "model_dump"):
                return v.model_dump(
                    mode="json", exclude_none=True, by_alias=True, exclude_unset=True
                )
            if isinstance(v, list):
                return [plain(x) for x in v]
            return v

        return plain(result)

    async def say(self, chat, text, rows=None):
        return await self.call(
            "send_message",
            chat_id=chat,
            text=text[:4000],
            reply_markup={"inline_keyboard": rows} if rows else None,
        )

    async def photo(self, message):
        if message.get("photo"):
            file_id = message["photo"][-1]["file_id"]
        elif message.get("document", {}).get("mime_type", "").startswith("image/"):
            file_id = message["document"]["file_id"]
        elif message.get("video", {}).get("thumbnail"):
            file_id = message["video"]["thumbnail"]["file_id"]
        else:
            return None
        output = io.BytesIO()
        await self.bot.download(file_id, destination=output)
        return output.getvalue()
