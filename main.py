import asyncio
import io
import logging
import os
import zipfile
from typing import Dict, List, Tuple, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)

TOKEN: Optional[str] = os.getenv("BOT_TOKEN")

dp = Dispatcher()

cache: Dict[str, Tuple[str, str, str]] = {}


def get_sticker_type(sticker) -> str:
    if sticker.is_animated:
        return "animated"
    if sticker.is_video:
        return "video"
    return "static"


def get_extension(sticker) -> str:
    if sticker.is_animated:
        return "tgs"
    if sticker.is_video:
        return "webm"
    return "webp"


def make_keyboard(key: str):
    kb = InlineKeyboardBuilder()

    kb.button(
        text="Download full pack ZIP",
        callback_data=f"zip:{key}",
    )

    kb.button(
        text="Download original sticker",
        callback_data=f"lossless:{key}",
    )

    kb.button(
        text="Show sticker",
        callback_data=f"show:{key}",
    )

    kb.adjust(1)
    return kb.as_markup()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Send any Telegram sticker")


@dp.message(F.sticker)
async def sticker_handler(
    message: Message,
    bot: Bot,
) -> None:
    sticker = message.sticker

    if sticker is None or message.from_user is None:
        return

    if sticker.set_name is None:
        await message.answer("Sticker has no set")
        return

    try:
        pack = await bot.get_sticker_set(sticker.set_name)
    except TelegramAPIError:
        logger.exception("Failed to get sticker set")
        await message.answer("Failed to get sticker set")
        return

    key = f"{message.from_user.id}:{sticker.file_unique_id}"
    ext = get_extension(sticker)

    cache[key] = (pack.name, sticker.file_id, ext)

    await message.answer(
        f"Title: {pack.title}\n"
        f"ID: {pack.name}\n"
        f"Count: {len(pack.stickers)}\n"
        f"Type: {get_sticker_type(sticker)}\n"
        f"Format: {ext}\n"
        f"Emoji: {sticker.emoji or '-'}",
        reply_markup=make_keyboard(key),
    )


async def download_file_bytes(
    bot: Bot,
    file_id: str,
) -> bytes | None:
    try:
        file = await bot.get_file(file_id)
        if file.file_path is None:
            return None
        stream = await bot.download_file(file.file_path)
        if stream is None:
            return None
        return stream.read()
    except TelegramAPIError:
        logger.exception("File download failed")
        return None


async def download_pack(
    bot: Bot,
    name: str,
) -> List[Tuple[str, bytes]]:
    pack = await bot.get_sticker_set(name)
    files = []

    for idx, sticker in enumerate(pack.stickers, start=1):
        data = await download_file_bytes(bot, sticker.file_id)
        if data is None:
            continue
        ext = get_extension(sticker)
        filename = f"sticker_{idx}.{ext}"
        files.append((filename, data))

    return files


async def send_lossless(
    call: CallbackQuery,
    bot: Bot,
    file_id: str,
    extension: str,
) -> None:
    if call.message is None:
        return

    data = await download_file_bytes(bot, file_id)
    if data is None:
        await call.message.answer("Download failed")
        return

    await call.message.answer_document(
        BufferedInputFile(data, f"sticker.{extension}")
    )


async def send_sticker(
    call: CallbackQuery,
    bot: Bot,
    file_id: str,
) -> None:
    if call.message is None:
        return

    try:
        await bot.send_sticker(
            chat_id=call.message.chat.id,
            sticker=file_id,
        )
    except TelegramAPIError:
        logger.exception("Failed to send sticker")
        await call.message.answer("Failed to send sticker")


@dp.callback_query(
    F.data.startswith(
        (
            "zip:",
            "lossless:",
            "show:",
        )
    )
)
async def callback_handler(
    call: CallbackQuery,
    bot: Bot,
) -> None:
    if call.data is None or call.message is None:
        return

    mode, key = call.data.split(":", 1)

    data = cache.get(key)
    if data is None:
        await call.answer("Expired", show_alert=True)
        return

    pack_name, sticker_id, extension = data

    await call.answer("Preparing")

    try:
        if mode == "lossless":
            await send_lossless(call, bot, sticker_id, extension)

        elif mode == "show":
            await send_sticker(call, bot, sticker_id)

        elif mode == "zip":
            files = await download_pack(bot, pack_name)

            archive = io.BytesIO()
            with zipfile.ZipFile(
                archive,
                "w",
                zipfile.ZIP_STORED,
            ) as z:
                for filename, content in files:
                    z.writestr(filename, content)

            await call.message.answer_document(
                BufferedInputFile(
                    archive.getvalue(),
                    "sticker_pack.zip",
                )
            )

    except TelegramAPIError:
        logger.exception("Telegram API error")
        await call.message.answer("Telegram error")
    except Exception:
        logger.exception("Callback error")
        await call.message.answer("Internal error")


async def main() -> None:
    if TOKEN is None:
        raise RuntimeError("BOT_TOKEN missing")

    bot = Bot(
        TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    logger.info("Bot started")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
