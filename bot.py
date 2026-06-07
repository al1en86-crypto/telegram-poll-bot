import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PollAnswer,
)

import database

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

router = Router()


class TopCallback(CallbackData, prefix="top"):
    poll_id: str


class DelPollCallback(CallbackData, prefix="del"):
    poll_id: str


class DelConfirmCallback(CallbackData, prefix="delconf"):
    poll_id: str
    yes: bool


class BroadcastState(StatesGroup):
    waiting_for_content = State()


# ─── helpers ────────────────────────────────────────────────────────────────

async def _send_one_poll(
    bot: Bot,
    user_id: int,
    question: str,
    options: list[str],
    correct_option_id: int | None,
    allows_multiple: bool = False,
) -> str | None:
    """Send one poll to one user. Returns the telegram poll_id or None on failure."""
    try:
        if correct_option_id is not None:
            msg = await bot.send_poll(
                chat_id=user_id,
                question=question,
                options=options,
                type="quiz",
                correct_option_id=correct_option_id,
                is_anonymous=False,
            )
        else:
            msg = await bot.send_poll(
                chat_id=user_id,
                question=question,
                options=options,
                is_anonymous=False,
                allows_multiple_answers=allows_multiple,
            )
        return msg.poll.id
    except TelegramForbiddenError:
        logger.warning("User %s blocked the bot", user_id)
    except TelegramBadRequest as e:
        logger.warning("Bad request for user %s: %s", user_id, e)
    return None


async def _broadcast_poll(
    bot: Bot,
    users: list[tuple],
    question: str,
    options: list[str],
    correct_option_id: int | None,
    allows_multiple: bool = False,
) -> tuple[str | None, int, int, list[tuple[str, int]]]:
    """
    Broadcast a poll to all users.
    Returns (canonical_poll_id, sent, failed, sends).
    `sends` is a list of (telegram_poll_id, user_id) for every successful send.
    """
    sends: list[tuple[str, int]] = []
    failed = 0

    for user_id, _, _ in users:
        tg_poll_id = await _send_one_poll(
            bot, user_id, question, options, correct_option_id, allows_multiple
        )
        if tg_poll_id is not None:
            sends.append((tg_poll_id, user_id))
        else:
            failed += 1

    canonical_poll_id = sends[0][0] if sends else None
    return canonical_poll_id, len(sends), failed, sends


# ─── user commands ───────────────────────────────────────────────────────────

@router.message(CommandStart())
async def start_handler(message: Message):
    user = message.from_user
    await database.save_user(user.id, user.username, user.first_name)
    await message.answer(
        f"👋 Привет, <b>{user.first_name}</b>! Ты зарегистрирован.\n\n"
        "Ты будешь получать опросы от администратора.\n"
        "Напиши /polls, чтобы пройти все актуальные опросы прямо сейчас.",
        parse_mode="HTML",
    )
    logger.info("User registered: %s (%s)", user.id, user.first_name)


@router.message(Command("polls"))
async def polls_handler(message: Message, bot: Bot):
    """Send all unanswered polls to the user, regardless of when they were sent."""
    user = message.from_user
    await database.save_user(user.id, user.username, user.first_name)

    unanswered = await database.get_unanswered_polls(user.id)

    if not unanswered:
        await message.answer("✅ Ты уже ответил на все опросы! Следи за новыми.")
        return

    await message.answer(
        f"📋 Для тебя <b>{len(unanswered)}</b> опрос(а). Отвечай в своём темпе!",
        parse_mode="HTML",
    )

    sends: list[tuple[str, str, int]] = []
    for poll in unanswered:
        tg_poll_id = await _send_one_poll(
            bot,
            user.id,
            poll["question"],
            poll["options"],
            poll["correct_option_id"],
        )
        if tg_poll_id is not None:
            sends.append((tg_poll_id, poll["poll_id"], user.id))

    if sends:
        await database.save_poll_sends(sends)

    logger.info("Sent %d unanswered polls to user %s", len(sends), user.id)


# ─── admin commands ──────────────────────────────────────────────────────────

@router.message(Command("sendpoll"), F.from_user.id == ADMIN_ID)
async def sendpoll_handler(message: Message, bot: Bot):
    """
    Формат: /sendpoll Вопрос; Вариант1; Вариант2; *Вариант3
    Вариант с * в начале — правильный ответ (квиз-режим).
    Без * — обычный опрос.
    """
    args = message.text.removeprefix("/sendpoll").strip()

    if not args or ";" not in args:
        await message.answer(
            "📋 <b>Использование:</b>\n"
            "<code>/sendpoll Вопрос; Вариант1; Вариант2; Вариант3</code>\n\n"
            "Чтобы создать квиз — поставь <code>*</code> перед правильным ответом:\n"
            "<code>/sendpoll Столица России?; Киев; *Москва; Минск</code>",
            parse_mode="HTML",
        )
        return

    parts = [p.strip() for p in args.split(";") if p.strip()]
    if len(parts) < 3:
        await message.answer(
            "⚠️ Укажи вопрос и минимум 2 варианта ответа.\n\n"
            "<b>Пример:</b>\n"
            "<code>/sendpoll Лучшее время года?; Весна; Лето; *Осень; Зима</code>",
            parse_mode="HTML",
        )
        return

    question = parts[0]
    raw_options = parts[1:]

    if len(question) > 300:
        await message.answer("⚠️ Вопрос слишком длинный (максимум 300 символов).")
        return

    correct_option_id: int | None = None
    options: list[str] = []
    for i, opt in enumerate(raw_options):
        if opt.startswith("*"):
            correct_option_id = i
            options.append(opt[1:].strip())
        else:
            options.append(opt)

    users = await database.get_all_users()
    if not users:
        await message.answer("⚠️ Нет зарегистрированных пользователей.")
        return

    mode = "квиз" if correct_option_id is not None else "опрос"
    status_msg = await message.answer(f"📤 Рассылаю {mode} {len(users)} пользователям…")

    canonical_poll_id, sent, failed, sends = await _broadcast_poll(
        bot, users, question, options, correct_option_id
    )

    if canonical_poll_id:
        await database.save_poll(canonical_poll_id, question, options, correct_option_id)
        await database.save_poll_sends(
            [(tg_id, canonical_poll_id, uid) for tg_id, uid in sends]
        )
        logger.info("Poll broadcast: %s | question: %s", canonical_poll_id, question)

    await status_msg.edit_text(
        f"✅ {'Квиз' if correct_option_id is not None else 'Опрос'} разослан!\n\n"
        f"📨 Доставлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>",
        parse_mode="HTML",
    )


@router.message(F.from_user.id == ADMIN_ID, F.poll)
async def forwarded_poll_handler(message: Message, bot: Bot):
    poll = message.poll
    question = poll.question
    options = [opt.text for opt in poll.options]
    correct_option_id = poll.correct_option_id

    users = await database.get_all_users()
    if not users:
        await message.answer("⚠️ Нет зарегистрированных пользователей.")
        return

    mode = "квиз" if correct_option_id is not None else "опрос"
    status_msg = await message.answer(
        f"📤 Рассылаю {mode} <b>{len(users)}</b> пользователям…",
        parse_mode="HTML",
    )

    canonical_poll_id, sent, failed, sends = await _broadcast_poll(
        bot,
        users,
        question,
        options,
        correct_option_id,
        allows_multiple=poll.allows_multiple_answers,
    )

    if canonical_poll_id:
        await database.save_poll(canonical_poll_id, question, options, correct_option_id)
        await database.save_poll_sends(
            [(tg_id, canonical_poll_id, uid) for tg_id, uid in sends]
        )
        logger.info("Forwarded poll: %s | question: %s", canonical_poll_id, question)

    await status_msg.edit_text(
        f"✅ {'Квиз' if correct_option_id is not None else 'Опрос'} разослан!\n\n"
        f"📨 Доставлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>",
        parse_mode="HTML",
    )


@router.message(Command("results"), F.from_user.id == ADMIN_ID)
async def results_handler(message: Message):
    polls = await database.get_polls_with_results()

    if not polls:
        await message.answer("📭 Опросов пока нет.")
        return

    lines = []
    for i, poll in enumerate(polls, 1):
        total = poll["total"]
        lines.append(f"<b>Опрос {i}: {poll['question']}</b>")
        for opt in poll["options"]:
            pct = (opt["count"] / total * 100) if total > 0 else 0
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            lines.append(f"  {bar} {opt['text']}: <b>{opt['count']}</b> ({pct:.1f}%)")
        lines.append(f"  👥 Всего ответов: <b>{total}</b>\n")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("top"), F.from_user.id == ADMIN_ID)
async def top_handler(message: Message):
    polls = await database.get_quiz_polls()

    if not polls:
        await message.answer(
            "📭 Квиз-опросов пока нет.\n\n"
            "Чтобы создать квиз, поставь <code>*</code> перед правильным ответом:\n"
            "<code>/sendpoll Вопрос?; Вариант1; *Правильный; Вариант3</code>\n"
            "Или перешли квиз-опрос из Telegram.",
            parse_mode="HTML",
        )
        return

    buttons = [
        [InlineKeyboardButton(
            text=f"❓ {p['question'][:50]}{'…' if len(p['question']) > 50 else ''}",
            callback_data=TopCallback(poll_id=p["poll_id"]).pack(),
        )]
        for p in polls
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        "🏆 <b>Топ-5 по квизу</b>\n\nВыбери опрос:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(TopCallback.filter(), F.from_user.id == ADMIN_ID)
async def top_callback(callback: CallbackQuery, callback_data: TopCallback):
    polls = await database.get_quiz_polls()
    poll = next((p for p in polls if p["poll_id"] == callback_data.poll_id), None)

    if not poll:
        await callback.answer("Опрос не найден.", show_alert=True)
        return

    correct_idx = poll["correct_option_id"]
    correct_text = poll["options"][correct_idx]
    top5 = await database.get_top5_correct(poll["poll_id"], correct_idx)

    lines = [
        f"🏆 <b>Топ-5: {poll['question']}</b>",
        f"✅ Правильный ответ: <b>{correct_text}</b>\n",
    ]

    if not top5:
        lines.append("Никто ещё не ответил правильно.")
    else:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, user in enumerate(top5):
            name = user["first_name"]
            if user["username"]:
                name += f" (@{user['username']})"
            lines.append(f"{medals[i]} {name}")

    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.answer()


# ─── users ───────────────────────────────────────────────────────────────────

@router.message(Command("users"), F.from_user.id == ADMIN_ID)
async def users_handler(message: Message):
    users = await database.get_users_list()

    if not users:
        await message.answer("📭 Подписчиков пока нет.")
        return

    lines = [f"👥 <b>Подписчики: {len(users)}</b>\n"]
    for i, u in enumerate(users, 1):
        name = u["first_name"]
        if u["username"]:
            name += f" (@{u['username']})"
        date = u["joined_at"][:10] if u["joined_at"] else "—"
        lines.append(f"{i}. {name} — <i>{date}</i>")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"

    await message.answer(text, parse_mode="HTML")


# ─── broadcast ───────────────────────────────────────────────────────────────

@router.message(Command("broadcast"), F.from_user.id == ADMIN_ID)
async def broadcast_start(message: Message, state: FSMContext):
    await state.set_state(BroadcastState.waiting_for_content)
    await message.answer(
        "📢 <b>Рассылка</b>\n\n"
        "Отправь любое сообщение — текст, фото, видео, документ и т.д.\n"
        "Оно будет разослано всем пользователям без плашки «переслано».\n\n"
        "Для отмены — /cancel",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), F.from_user.id == ADMIN_ID)
async def broadcast_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("❌ Рассылка отменена.")


@router.message(BroadcastState.waiting_for_content, F.from_user.id == ADMIN_ID)
async def broadcast_send(message: Message, bot: Bot, state: FSMContext):
    await state.clear()

    users = await database.get_all_users()
    if not users:
        await message.answer("⚠️ Нет зарегистрированных пользователей.")
        return

    status_msg = await message.answer(
        f"📤 Рассылаю сообщение {len(users)} пользователям…"
    )

    sent = 0
    failed = 0
    for user_id, _, _ in users:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent += 1
        except TelegramForbiddenError:
            logger.warning("User %s blocked the bot", user_id)
            failed += 1
        except TelegramBadRequest as e:
            logger.warning("Bad request for user %s: %s", user_id, e)
            failed += 1

    logger.info("Broadcast done: sent=%d failed=%d", sent, failed)
    await status_msg.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"📨 Доставлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>",
        parse_mode="HTML",
    )


# ─── delpoll ─────────────────────────────────────────────────────────────────

@router.message(Command("delpoll"), F.from_user.id == ADMIN_ID)
async def delpoll_handler(message: Message):
    polls = await database.get_all_polls()

    if not polls:
        await message.answer("📭 Опросов пока нет.")
        return

    buttons = []
    for p in polls:
        label = p["question"][:48]
        if len(p["question"]) > 48:
            label += "…"
        kind = "🧠" if p["correct_option_id"] is not None else "📊"
        buttons.append([InlineKeyboardButton(
            text=f"{kind} {label}",
            callback_data=DelPollCallback(poll_id=p["poll_id"]).pack(),
        )])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        "🗑 <b>Удалить опрос</b>\n\nВыбери опрос для удаления:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(DelPollCallback.filter(), F.from_user.id == ADMIN_ID)
async def delpoll_select_callback(callback: CallbackQuery, callback_data: DelPollCallback):
    polls = await database.get_all_polls()
    poll = next((p for p in polls if p["poll_id"] == callback_data.poll_id), None)

    if not poll:
        await callback.answer("Опрос не найден.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Да, удалить",
            callback_data=DelConfirmCallback(poll_id=poll["poll_id"], yes=True).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=DelConfirmCallback(poll_id=poll["poll_id"], yes=False).pack(),
        ),
    ]])

    await callback.message.edit_text(
        f"⚠️ <b>Удалить опрос?</b>\n\n"
        f"«{poll['question']}»\n\n"
        f"Все ответы и статистика будут удалены безвозвратно.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(DelConfirmCallback.filter(), F.from_user.id == ADMIN_ID)
async def delpoll_confirm_callback(callback: CallbackQuery, callback_data: DelConfirmCallback):
    if not callback_data.yes:
        await callback.message.edit_text("❌ Удаление отменено.")
        await callback.answer()
        return

    polls = await database.get_all_polls()
    poll = next((p for p in polls if p["poll_id"] == callback_data.poll_id), None)
    question = poll["question"] if poll else "опрос"

    await database.delete_poll(callback_data.poll_id)
    logger.info("Poll deleted: %s", callback_data.poll_id)

    await callback.message.edit_text(
        f"🗑 Опрос «{question}» удалён.\n"
        f"Статистика и ответы очищены.",
    )
    await callback.answer("Удалено!")


# ─── poll answer tracking ────────────────────────────────────────────────────

@router.poll_answer()
async def poll_answer_handler(poll_answer: PollAnswer):
    # Resolve canonical poll_id via poll_sends mapping (handles resent polls)
    canonical_poll_id = await database.get_canonical_poll_id(poll_answer.poll_id)
    poll_id = canonical_poll_id or poll_answer.poll_id

    await database.save_poll_answer(poll_id, poll_answer.user.id, poll_answer.option_ids)
    logger.info(
        "Answer recorded — canonical_poll: %s, user: %s, options: %s",
        poll_id,
        poll_answer.user.id,
        poll_answer.option_ids,
    )


async def _setup_commands(bot: Bot):
    user_commands = [
        BotCommand(command="start", description="Зарегистрироваться"),
        BotCommand(command="polls", description="Пройти все актуальные опросы"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    logger.info("Bot commands registered")


async def main():
    await database.init_db()
    logger.info("Database initialised")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await _setup_commands(bot)

    logger.info("Bot starting…")
    await dp.start_polling(
        bot, allowed_updates=["message", "poll_answer", "callback_query"]
    )


if __name__ == "__main__":
    asyncio.run(main())
