import asyncio
import io
import logging
import os
import signal
import zipfile
from typing import Dict, List, Tuple, Optional

from dotenv import load_dotenv
from PIL import Image

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)

TOKEN: Optional[str] = os.getenv("BOT_TOKEN")

dp = Dispatcher()

cache: Dict[str, Tuple[str, str]] = {}


def make_keyboard(key: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="Download full pack ZIP", callback_data=f"zip:{key}")
    kb.button(text="Download lossless sticker", callback_data=f"lossless:{key}")
    kb.button(text="Download as photo", callback_data=f"photo:{key}")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Send a sticker")


@dp.message(F.sticker)
async def sticker_handler(message: Message, bot: Bot) -> None:
    sticker = message.sticker

    if sticker is None or message.from_user is None:
        return

    if sticker.set_name is None:
        await message.answer("This sticker has no set")
        return

    try:
        pack = await bot.get_sticker_set(sticker.set_name)
    except TelegramAPIError:
        logger.exception("Failed to get sticker set")
        await message.answer("Failed to get sticker set")
        return

    key = f"{message.from_user.id}:{sticker.file_unique_id}"

    cache[key] = (
        pack.name,
        sticker.file_id,
    )

    sticker_type = "regular"

    if sticker.is_animated:
        sticker_type = "animated"
    elif sticker.is_video:
        sticker_type = "video"

    await message.answer(
        f"Title: {pack.title}\n"
        f"ID: {pack.name}\n"
        f"Count: {len(pack.stickers)}\n"
        f"Type: {sticker_type}\n"
        f"Emoji: {sticker.emoji or '-'}",
        reply_markup=make_keyboard(key),
    )

    logger.info("Sticker received: %s", pack.name)


async def download_file_bytes(bot: Bot, file_id: str) -> bytes | None:
    try:
        file = await bot.get_file(file_id)

        if file.file_path is None:
            logger.warning("Missing file path: %s", file_id)
            return None

        data = await bot.download_file(file.file_path)

        if data is None:
            return None

        return data.read()

    except TelegramAPIError:
        logger.exception("File download failed: %s", file_id)
        return None


async def download_pack(bot: Bot, name: str) -> List[Tuple[str, bytes]]:
    pack = await bot.get_sticker_set(name)
    result: List[Tuple[str, bytes]] = []

    for sticker in pack.stickers:
        data = await download_file_bytes(
            bot,
            sticker.file_id,
        )

        if data is None:
            continue

        result.append(
            (
                f"{sticker.file_unique_id}.webp",
                data,
            )
        )

    return result


async def send_lossless(call: CallbackQuery, bot: Bot, file_id: str) -> None:
    if call.message is None:
        return

    data = await download_file_bytes(bot, file_id)

    if data is None:
        await call.message.answer("Failed to download file")
        return

    await call.message.answer_document(
        BufferedInputFile(data, "sticker.webp")
    )


async def send_photo(call: CallbackQuery, bot: Bot, file_id: str) -> None:
    if call.message is None:
        return

    data = await download_file_bytes(bot, file_id)

    if data is None:
        await call.message.answer("Failed to download file")
        return

    try:
        image = Image.open(io.BytesIO(data))

        if getattr(image, "is_animated", False):
            await call.message.answer(
                "Animated stickers cannot be sent as photo"
            )
            return

        output = io.BytesIO()

        image.convert("RGBA").save(
            output,
            "PNG",
        )

        await call.message.answer_photo(
            BufferedInputFile(
                output.getvalue(),
                "sticker.png",
            )
        )

    except Exception:
        logger.exception("Photo conversion failed")
        await call.message.answer("Sticker cannot be converted to photo")


@dp.callback_query(
    F.data.startswith(("zip:", "lossless:", "photo:"))
)
async def callback_handler(call: CallbackQuery, bot: Bot) -> None:
    if call.data is None or call.message is None:
        return

    mode, key = call.data.split(":", 1)

    data = cache.get(key)

    if data is None:
        await call.answer(
            "Data is outdated",
            show_alert=True,
        )
        return

    pack_name, sticker_file_id = data

    await call.answer("Preparing")

    try:
        if mode == "lossless":
            await send_lossless(
                call,
                bot,
                sticker_file_id,
            )
            return

        if mode == "photo":
            await send_photo(
                call,
                bot,
                sticker_file_id,
            )
            return

        if mode == "zip":
            files = await download_pack(
                bot,
                pack_name,
            )

            archive = io.BytesIO()

            with zipfile.ZipFile(
                archive,
                "w",
                zipfile.ZIP_STORED,
            ) as z:
                for name, content in files:
                    z.writestr(
                        name,
                        content,
                    )

            await call.message.answer_document(
                BufferedInputFile(
                    archive.getvalue(),
                    "sticker_pack.zip",
                )
            )

    except TelegramAPIError:
        logger.exception("Telegram API error")
        await call.message.answer("Telegram API error")

    except Exception:
        logger.exception("Callback processing failed")
        await call.message.answer("Internal error")


async def shutdown(bot: Bot) -> None:
    logger.info("Stopping bot")

    try:
        await bot.delete_webhook(
            drop_pending_updates=False,
        )
    except Exception:
        logger.exception("Webhook cleanup failed")

    await bot.session.close()

    logger.info("Bot stopped")


async def main() -> None:
    if TOKEN is None:
        raise RuntimeError("BOT_TOKEN is missing")

    bot = Bot(
        TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def stop_signal() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (
        signal.SIGINT,
        signal.SIGTERM,
    ):
        try:
            loop.add_signal_handler(
                sig,
                stop_signal,
            )
        except NotImplementedError:
            pass

    logger.info("Bot started")

    polling_task = asyncio.create_task(
        dp.start_polling(bot)
    )

    await stop_event.wait()

    await dp.stop_polling()

    polling_task.cancel()

    try:
        await polling_task
    except asyncio.CancelledError:
        pass

    await shutdown(bot)


if __name__ == "__main__":
    asyncio.run(main())
