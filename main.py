import asyncio
import io
import logging
import re
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile, BotCommand, TelegramObject
from aiogram.types import Message
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


class SttCreateVideoLabDto(SttBaseReminder):
    youtube_url: str


class SttResponseVideoLabDto(SttBaseReminder, Text):
    pass


app = FastStream(broker)
dp = Dispatcher()
bot = Bot(token=config.BOT_TOKEN)

admins = [556203349, 2025671326]


class Form(StatesGroup):
    youtube_url = State()


YOUTUBE_URL_REGEX = re.compile(
    r'(https?://)?(www\.)?(youtube|youtu\.be)/.+',
    re.IGNORECASE
)

transcriptions: dict[int, str] = {}


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(
            self,
            handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: Dict[str, Any]
    ) -> Any:
        # Убедимся, что событие — это Message и у него есть from_user
        if isinstance(event, Message):
            user_id = event.from_user.id
            if user_id not in admins:
                # Не пропускаем дальше, можно ответить или проигнорировать
                if event.text:  # чтобы не сломать обработку других типов
                    await event.answer("❌ Доступ только для администраторов.")
                return

        # Если админ — продолжаем обработку
        return await handler(event, data)


dp.message.middleware(AdminOnlyMiddleware())


@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    await message.answer(
        f"Привет, {message.from_user.full_name}!\nПрисылай мне ссылку на YouTube, а я в ответ пришлю текст сообщением и текстовым файлом 😊")


@dp.message(F.text.regexp(YOUTUBE_URL_REGEX) | F.text.startswith("https://www.youtube.com/watch"))
async def get_youtube_url(message: Message, state: FSMContext) -> None:
    try:
        url = message.text.strip()

        await message.answer("Отлично, ссылка на YouTube получена, скоро пришлю ответ...", )

        dto: SttCreateVideoLabDto = SttCreateVideoLabDto(
            youtube_url=url,
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

        text = body.text
        last_msg = None
        for i in range(0, len(text), 4000):
            last_msg = await bot.send_message(
                chat_id=body.user_id,
                reply_to_message_id=body.message_id,
                text=text[i:i + 4000]
            )

        file = BufferedInputFile(io.BytesIO(body.text.encode("utf-8")).getvalue(), filename="transcription.txt")
        if last_msg:
            await bot.send_document(chat_id=body.user_id, reply_to_message_id=last_msg.message_id, document=file,
                                    caption="✅ Вот текст в документе")
        else:
            await bot.send_document(chat_id=body.user_id, document=file, caption="✅ Вот текст в документе")
        await message.ack()
    except Exception as e:
        logger.error(e)
        await message.nack(requeue=True)


# Run the bot + faststream
async def main() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота и показать клавиатуру")
    ])

    for admin_id in admins:
        await bot.send_message(chat_id=admin_id, text='Привет, я запустился и готов принимать ссылки 📹',
                               disable_notification=True)

    # запускаем faststream и aiogram параллельно
    await asyncio.gather(
        dp.start_polling(bot),
        app.run()
    )


if __name__ == "__main__":
    asyncio.run(main())
