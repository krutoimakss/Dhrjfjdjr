import asyncio
import html
import logging
import os
import random
import re
import secrets
import sqlite3
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession
from telethon.tl.types import User

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("randombot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "8629355594:AAEfPaJyK9W8NSILOzaSFg0PbK935_4y2dc")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
TG_SESSION = os.getenv("TG_SESSION")
DB_PATH = os.getenv("DB_PATH", "seen.db")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}

tg: TelegramClient | None = None  # userbot для режима "по ссылке"
tg_lock = asyncio.Lock()
router = Router()

# ---------------------------------------------------------------- хранилище

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    "CREATE TABLE IF NOT EXISTS seen ("
    "chat_id INTEGER, user_id INTEGER, name TEXT, username TEXT, "
    "PRIMARY KEY (chat_id, user_id))"
)
db.commit()


def remember(chat_id: int, user) -> None:
    if user is None or user.is_bot:
        return
    db.execute(
        "INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?)",
        (chat_id, user.id, user.full_name, user.username),
    )
    db.commit()


def seen_users(chat_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT user_id, name, username FROM seen WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    return [{"id": r[0], "name": r[1], "username": r[2]} for r in rows]


# ---------------------------------------------------------------- утилиты


def fmt(p: dict) -> str:
    name = html.escape(p["name"] or "Без имени")
    if p["username"]:
        return f'<a href="https://t.me/{p["username"]}">{name}</a> (@{p["username"]})'
    return f'<a href="tg://user?id={p["id"]}">{name}</a>'


def result_text(p: dict, total: int) -> str:
    return f"🎲 Рандомный пользователь:\n\n{fmt(p)}\n\nВсего в выборке: {total}"


def kb(data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Ещё раз", callback_data=data)]]
    )


INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)", re.I
)
PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][\w]{3,})(?:/\d+)?", re.I
)


def parse_link(text: str):
    m = INVITE_RE.search(text)
    if m:
        return ("invite", m.group(1))
    m = PUBLIC_RE.search(text)
    if m and m.group(1).lower() != "joinchat":
        return ("public", m.group(1))
    return None


class UserError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


# ---------------------------------------------------------------- режим "по ссылке"

LINKS: dict[str, tuple[str, str]] = {}
CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
CACHE_TTL = 300


async def fetch_participants(kind: str, key: str) -> list[dict]:
    cached = CACHE.get((kind, key))
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    joined = False
    entity = None
    async with tg_lock:
        try:
            if kind == "invite":
                try:
                    updates = await tg(functions.messages.ImportChatInviteRequest(key))
                    entity = updates.chats[0]
                    joined = True
                except errors.UserAlreadyParticipantError:
                    info = await tg(functions.messages.CheckChatInviteRequest(key))
                    entity = info.chat
                except errors.InviteRequestSentError:
                    raise UserError(
                        "Чат с заявками на вступление: заявка отправлена, "
                        "но нужно, чтобы админ её одобрил. Попробуй позже."
                    )
            else:
                entity = await tg.get_entity(key)

            users = []
            async for u in tg.iter_participants(entity):
                if isinstance(u, User) and not u.bot and not u.deleted and not u.is_self:
                    name = " ".join(x for x in (u.first_name, u.last_name) if x)
                    users.append({"id": u.id, "name": name, "username": u.username})
        except UserError:
            raise
        except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
            raise UserError("Ссылка-приглашение недействительна или истекла.")
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
            raise UserError("Не нашёл такой чат. Проверь ссылку.")
        except errors.ChatAdminRequiredError:
            raise UserError(
                "Список участников этого чата скрыт (например, это канал). "
                "Для каналов он доступен только админам."
            )
        except errors.FloodWaitError as e:
            raise UserError(f"Telegram просит подождать {e.seconds} сек. Попробуй позже.")
        except errors.RPCError as e:
            log.exception("Telegram error")
            raise UserError(f"Ошибка Telegram: {e.__class__.__name__}")
        finally:
            if joined and entity is not None:
                try:
                    await tg.delete_dialog(entity)  # выходим из чата после выборки
                except Exception:
                    log.warning("Не удалось выйти из чата", exc_info=True)

    if not users:
        raise UserError("Не удалось получить ни одного участника.")
    CACHE[(kind, key)] = (time.time(), users)
    return users


# ---------------------------------------------------------------- хэндлеры

HELP = (
    "Привет! Я выбираю <b>рандомного пользователя</b> из чата.\n\n"
    "<b>Способ 1.</b> Пришли мне ссылку на чат (https://t.me/... или "
    "приглашение t.me/+...) — я выберу случайного участника.\n\n"
    "<b>Способ 2.</b> Добавь меня в группу и напиши <code>/random</code>. "
    "Я выбираю из тех, кто писал в чате после моего появления, и из админов. "
    "Чтобы я видел всех, отключи Privacy Mode в @BotFather или сделай меня админом."
)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(m: Message):
    await m.answer(HELP)


@router.message(Command("random", "random_user"), F.chat.type.in_(GROUP_TYPES))
async def group_random(m: Message, bot: Bot):
    remember(m.chat.id, m.from_user)
    text = await pick_in_group(bot, m.chat.id)
    await m.answer(text, reply_markup=kb("g"))


async def pick_in_group(bot: Bot, chat_id: int) -> str:
    try:
        for adm in await bot.get_chat_administrators(chat_id):
            remember(chat_id, adm.user)
    except TelegramBadRequest:
        pass
    users = seen_users(chat_id)
    if not users:
        return "Пока никого не знаю в этом чате. Пусть люди напишут пару сообщений!"
    return result_text(random.choice(users), len(users))


@router.callback_query(F.data == "g")
async def group_reroll(cb: CallbackQuery, bot: Bot):
    text = await pick_in_group(bot, cb.message.chat.id)
    await safe_edit(cb, text, "g")


@router.callback_query(F.data.startswith("r:"))
async def link_reroll(cb: CallbackQuery):
    token = cb.data[2:]
    parsed = LINKS.get(token)
    if not parsed:
        await cb.answer("Ссылка устарела, пришли её заново.", show_alert=True)
        return
    try:
        users = await fetch_participants(*parsed)
    except UserError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await safe_edit(cb, result_text(random.choice(users), len(users)), cb.data)


async def safe_edit(cb: CallbackQuery, text: str, data: str):
    try:
        await cb.message.edit_text(text, reply_markup=kb(data))
    except TelegramBadRequest:
        pass  # тот же пользователь выпал — текст не изменился
    await cb.answer()


@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def private_text(m: Message):
    parsed = parse_link(m.text)
    if not parsed:
        await m.answer("Пришли ссылку на чат вида https://t.me/... 🙂")
        return
    if tg is None:
        await m.answer(
            "Режим по ссылке не настроен на сервере. "
            "Добавь меня в группу и используй /random."
        )
        return
    status = await m.answer("⏳ Смотрю участников…")
    try:
        users = await fetch_participants(*parsed)
    except UserError as e:
        await status.edit_text(str(e))
        return
    token = secrets.token_urlsafe(6)
    LINKS[token] = parsed
    await status.edit_text(
        result_text(random.choice(users), len(users)), reply_markup=kb(f"r:{token}")
    )


@router.message(F.chat.type.in_(GROUP_TYPES))
async def track(m: Message):
    remember(m.chat.id, m.from_user)
    for u in m.new_chat_members or []:
        remember(m.chat.id, u)


# ---------------------------------------------------------------- запуск


async def main():
    global tg
    if API_ID and API_HASH and TG_SESSION:
        client = TelegramClient(StringSession(TG_SESSION), int(API_ID), API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            tg = client
            log.info("Режим по ссылке включён")
        else:
            log.error("TG_SESSION недействителен — режим по ссылке выключен")
    else:
        log.info("API_ID/API_HASH/TG_SESSION не заданы — работает только /random в группах")

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview_is_disabled=True
        ),
    )
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
