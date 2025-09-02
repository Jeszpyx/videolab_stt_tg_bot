import asyncio
import base64
import io
import logging
import os
import re
import tempfile

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile, BotCommand
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, \
    InlineKeyboardButton, CallbackQuery
from faststream import FastStream
from faststream.rabbit import RabbitBroker, RabbitExchange, RabbitQueue, RabbitMessage
from pydantic import AmqpDsn, Field, BaseModel, PositiveInt
from pydantic_settings import BaseSettings

# 🟩 Получаем логгер до инициализации всего остального
logger = logging.getLogger("faststream")

logger.info("Начало инициализации приложения")

logger.info('Валидирую переменные окружения...')


class Config(BaseSettings):
    BOT_TOKEN: str = Field()
    RABBITMQ_URL: AmqpDsn = Field()
    RABBITMQ_EXCHANGE: str = Field(default="remaindme")
    RABBITMQ_VIDEOLAB_QUEUE_IN: str = Field(default="stt.videolab_in")
    RABBITMQ_VIDEOLAB_QUEUE_OUT: str = Field(default="stt.videolab_out")

    class Config:
        env_file: str = ".env"
        env_file_encoding: str = "utf-8"
        # Игнорировать неизвестные переменные
        extra = "ignore"


config = Config()

logger.info('Инициализирую брокер, обменник и очереди...')
broker = RabbitBroker(config.RABBITMQ_URL.encoded_string())

exchange = RabbitExchange(config.RABBITMQ_EXCHANGE)

videolab_input_queue = RabbitQueue(config.RABBITMQ_VIDEOLAB_QUEUE_IN)
videolab_output_queue = RabbitQueue(config.RABBITMQ_VIDEOLAB_QUEUE_OUT)


class Text(BaseModel):
    text: str


class Audio(BaseModel):
    audio: str


class SttBaseReminder(BaseModel):
    user_id: PositiveInt
    message_id: PositiveInt


class SttCreateVideoLabDto(SttBaseReminder, Audio):
    pass


class SttResponseVideoLabDto(SttBaseReminder, Text):
    pass


app = FastStream(broker)
dp = Dispatcher()
bot = Bot(token=config.BOT_TOKEN)

get_text_from_youtube_url = "Получить текст из YouTube"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=get_text_from_youtube_url)]
    ],
    resize_keyboard=True
)


class Form(StatesGroup):
    youtube_url = State()


YOUTUBE_REGEX = re.compile(
    r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+$'
)

transcriptions: dict[int, str] = {}


@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    await message.answer(f"Привет, {message.from_user.full_name}!\nВыбери действие на клавиатуре:",
                         reply_markup=main_kb)


@dp.message(F.text == get_text_from_youtube_url)
async def get_youtube_url(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.youtube_url)
    await message.answer(
        "Хорошо, отправь мне ссылку на YouTube",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Form.youtube_url)
async def process_youtube_url(message: Message, state: FSMContext) -> None:
    try:
        url = message.text.strip()

        if not YOUTUBE_REGEX.match(url):
            await message.answer("Это не похоже на ссылку YouTube, попробуйте ещё раз 🙂")
            return

        await message.answer("Отлично, ссылка на YouTube получена, скоро пришлю ответ...", reply_markup=main_kb)

        audio_base64_str = await download_audio(url)

        dto: SttCreateVideoLabDto = SttCreateVideoLabDto(
            audio=audio_base64_str,
            user_id=message.from_user.id,
            message_id=message.message_id,
        )

        await broker.publish(dto, queue=videolab_input_queue, exchange=exchange)
        await state.clear()
    except Exception as e:
        logger.error(e)
        await message.answer(f'Произошла ошибка при скачивании аудио:\n{e}')


@broker.subscriber(queue=videolab_output_queue, exchange=exchange, no_ack=True)
async def process_transcribed_text(body: SttResponseVideoLabDto, message: RabbitMessage) -> None:
    try:
        MAX_MESSAGE_LENGTH = 4000  # немного меньше лимита для безопасности

        text = body.text
        last_msg = None
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            last_msg = await bot.send_message(
                chat_id=body.user_id,
                reply_to_message_id=body.message_id,
                text=text[i:i + MAX_MESSAGE_LENGTH]
            )

        file = BufferedInputFile(io.BytesIO(body.text.encode("utf-8")).getvalue(), filename="transcription.txt")
        if last_msg:
            await bot.send_document(chat_id=body.user_id, reply_to_message_id=last_msg.message_id, document=file, caption="✅ Вот текст в документе")
        else:
            await bot.send_document(chat_id=body.user_id, document=file, caption="✅ Вот текст в документе")
        await message.ack()
    except Exception as e:
        logger.error(e)
        await message.nack(requeue=True)



async def download_audio(url: str) -> str:
    """
    Скачивает аудио из YouTube во временный файл,
    конвертирует его в base64-строку и возвращает.
    """

    def _download():
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "%(id)s.%(ext)s")

            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": output_path,
                "quiet": True,
                "noplaylist": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info)
                file_path = os.path.splitext(file_path)[0] + ".mp3"

            with open(file_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")

    return await asyncio.to_thread(_download)


# Run the bot + faststream
async def main() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота и показать клавиатуру")
    ])

    # запускаем faststream и aiogram параллельно
    await asyncio.gather(
        dp.start_polling(bot),
        app.run()
    )

if __name__ == "__main__":
    asyncio.run(main())
