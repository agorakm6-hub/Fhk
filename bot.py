import os
import time
import asyncio
import logging
from aiohttp import web

from aiogram import Bot, Dispatcher, BaseMiddleware, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, FSInputFile, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from store import Store
from stats import build_report
from roller import Pacer, collect, collect_missing

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_ID", "").replace(" ", "").split(",") if x.isdigit()}
DATA_DIR = os.getenv("DATA_DIR", ".")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing!")
if not WEBHOOK_URL:
    raise ValueError("RENDER_EXTERNAL_URL is missing!")

GAMES = {
    "dice": {"emoji": "🎲", "name": "Кости", "max": 6},
    "football": {"emoji": "⚽", "name": "Футбол", "max": 5},
    "basketball": {"emoji": "🏀", "name": "Баскетбол", "max": 5},
    "dart": {"emoji": "🎯", "name": "Дротик", "max": 6},
    "bowling": {"emoji": "🎳", "name": "Боулинг", "max": 6},
}

OPTIONS = {
    "football": ["Промах", "Гол в центр", "Гол в угол", "Штанга", "Гол от штанги"],
    "basketball": ["Промах", "Чистый гол", "Прокрут", "Застрял мяч", "Отскок"],
    "dart": ["Промах", "Красный сектор", "Белый сектор", "Центр", "Отскок", "Другой"],
    "bowling": ["Сбито 1/6", "Сбито 3/6", "Сбито 4/6", "Сбито 5/6", "Страйк", "Промах"],
}

ANIMATION_DELAY = 4.5

store = Store(DATA_DIR)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

active = {}
pacers = {}


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if ADMIN_IDS and (user is None or user.id not in ADMIN_IDS):
            if isinstance(event, types.CallbackQuery):
                await event.answer("Доступ запрещен", show_alert=True)
            elif isinstance(event, types.Message):
                await event.answer("⛔ Доступ запрещен.")
            return None
        return await handler(event, data)


dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())


def pacer_for(user_id):
    if user_id not in pacers:
        pacers[user_id] = Pacer()
    return pacers[user_id]


def dice_sender(chat_id, emoji):
    async def send():
        msg = await bot.send_dice(chat_id=chat_id, emoji=emoji)
        return msg.dice.value, msg.message_id
    return send


async def safe_edit(message, text, markup=None):
    try:
        await message.edit_text(text, reply_markup=markup)
    except (TelegramBadRequest, TelegramRetryAfter) as e:
        logging.debug(f"edit пропущен: {e}")


def main_menu():
    kb = InlineKeyboardBuilder()
    for gid, g in GAMES.items():
        kb.button(text=f"{g['emoji']} {g['name']}", callback_data=f"game:{gid}")
    kb.button(text="🎓 Обучение эмодзи", callback_data="learn")
    kb.button(text="📥 Выгрузить БД (JSON)", callback_data="export")
    kb.adjust(2)
    return kb.as_markup()


def attempts_menu(gid):
    kb = InlineKeyboardBuilder()
    for n in (10, 50, 100, 300):
        kb.button(text=f"{n} раз", callback_data=f"att:{gid}:{n}")
    kb.button(text="📈 За всё время", callback_data=f"all:{gid}")
    kb.button(text="📄 CSV", callback_data=f"csv:{gid}")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(4, 2, 1)
    return kb.as_markup()


def result_menu(gid):
    kb = InlineKeyboardBuilder()
    kb.button(text="📈 За всё время", callback_data=f"all:{gid}")
    kb.button(text="📄 CSV", callback_data=f"csv:{gid}")
    kb.button(text="🗑 Удалить эмодзи из чата", callback_data="clear")
    kb.adjust(2, 1)
    return kb.as_markup()


def stop_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="⏹ Стоп", callback_data="stop")
    return kb.as_markup()


def report_for(gid, values, scope):
    g = GAMES[gid]
    return build_report(f"{g['emoji']} {g['name']}", scope, values, g["max"], store.labels(gid))


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("👋 <b>Анализатор.</b>\nВыбери игру:", reply_markup=main_menu())


@dp.callback_query(F.data == "menu")
async def back_to_menu(cb: types.CallbackQuery):
    await safe_edit(cb.message, "👋 <b>Анализатор.</b>\nВыбери игру:", main_menu())
    await cb.answer()


@dp.callback_query(F.data == "export")
async def export_map(cb: types.CallbackQuery):
    await cb.answer()
    if not os.path.exists(store.map_path):
        await cb.message.answer("БД пока пустая. Сначала пройди обучение.")
        return
    await cb.message.answer_document(
        FSInputFile(store.map_path, filename="emoji_map.json"),
        caption="📁 <b>База знаний эмодзи.</b>"
    )


@dp.callback_query(F.data.startswith("game:"))
async def pick_game(cb: types.CallbackQuery):
    gid = cb.data.split(":")[1]
    g = GAMES[gid]
    await safe_edit(cb.message, f"Выбрано: {g['emoji']} <b>{g['name']}</b>\nСколько бросков?", attempts_menu(gid))
    await cb.answer()


@dp.callback_query(F.data.startswith("att:"))
async def run_attempts(cb: types.CallbackQuery):
    _, gid, n = cb.data.split(":")
    uid = cb.from_user.id
    if uid in active:
        await cb.answer("Сбор уже идёт", show_alert=True)
        return
    await cb.answer()

    game = GAMES[gid]
    total = int(n)
    stop = asyncio.Event()
    active[uid] = stop
    status = await cb.message.answer(f"⏳ Сбор данных... [0 / {total}]", reply_markup=stop_menu())
    last = [0.0]

    async def progress(done):
        now = time.monotonic()
        if done == total or now - last[0] >= 1.5:
            last[0] = now
            await safe_edit(status, f"⏳ Сбор данных... [{done} / {total}]", stop_menu())

    try:
        rows, errors = await collect(dice_sender(uid, game["emoji"]), total, pacer_for(uid), stop, progress)
    finally:
        active.pop(uid, None)

    if rows:
        await asyncio.to_thread(store.add_rolls, uid, gid, rows)

    note = ""
    if stop.is_set():
        note = " (остановлено)"
    elif errors:
        note = f" (ошибок: {errors})"
    await safe_edit(status, f"✅ Собрано {len(rows)} из {total}{note}")

    if not rows:
        return
    values = [v for v, _ in rows]
    await cb.message.answer(report_for(gid, values, "Бросков в серии"), reply_markup=result_menu(gid))


@dp.callback_query(F.data == "stop")
async def stop_run(cb: types.CallbackQuery):
    ev = active.get(cb.from_user.id)
    if ev:
        ev.set()
        await cb.answer("Останавливаю...")
    else:
        await cb.answer("Нет активного сбора")


@dp.callback_query(F.data.startswith("all:"))
async def all_time(cb: types.CallbackQuery):
    gid = cb.data.split(":")[1]
    await cb.answer()
    values = await asyncio.to_thread(store.values, cb.from_user.id, gid)
    await cb.message.answer(report_for(gid, values, "Бросков за всё время"))


@dp.callback_query(F.data.startswith("csv:"))
async def send_csv(cb: types.CallbackQuery):
    gid = cb.data.split(":")[1]
    await cb.answer()
    data, count = await asyncio.to_thread(store.export_csv, cb.from_user.id, gid)
    if not count:
        await cb.message.answer("Данных пока нет.")
        return
    await cb.message.answer_document(
        BufferedInputFile(data, filename=f"rolls_{gid}.csv"),
        caption=f"📄 Бросков: {count}"
    )


@dp.callback_query(F.data == "clear")
async def clear_chat(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()
    ids = await asyncio.to_thread(store.live_message_ids, uid)
    if not ids:
        await cb.message.answer("Нечего удалять.")
        return
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        while True:
            try:
                await bot.delete_messages(chat_id=uid, message_ids=chunk)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                logging.error(f"Удаление пачки не удалось: {e}")
                break
    await asyncio.to_thread(store.mark_deleted, uid, ids)
    await cb.message.answer(f"🗑 Отправлено на удаление: {len(ids)}")


@dp.callback_query(F.data == "learn")
async def learn_menu(cb: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    for gid, g in GAMES.items():
        if gid in OPTIONS:
            done = len(store.labels(gid))
            kb.button(text=f"{g['emoji']} {g['name']} {done}/{g['max']}", callback_data=f"ls:{gid}")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(2)
    await safe_edit(
        cb.message,
        "🎓 <b>Режим обучения.</b>\nВыбери игру. Я брошу эмодзи, которых тебе не хватает, и под каждым задам вопрос.",
        kb.as_markup()
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("ls:"))
async def learn_start(cb: types.CallbackQuery):
    uid = cb.from_user.id
    gid = cb.data.split(":")[1]
    game = GAMES[gid]
    known = {int(k) for k in store.labels(gid)}
    missing = set(range(1, game["max"] + 1)) - known

    if not missing:
        kb = InlineKeyboardBuilder()
        kb.button(text="♻️ Переучить заново", callback_data=f"lr:{gid}")
        kb.button(text="⬅️ Назад", callback_data="learn")
        kb.adjust(1)
        await safe_edit(cb.message, f"✅ Все значения для {game['name']} уже изучены.", kb.as_markup())
        await cb.answer()
        return

    if uid in active:
        await cb.answer("Сбор уже идёт", show_alert=True)
        return
    await cb.answer("Бросаю, подожди несколько секунд")

    stop = asyncio.Event()
    active[uid] = stop
    try:
        found = await collect_missing(dice_sender(uid, game["emoji"]), missing, pacer_for(uid), stop)
    finally:
        active.pop(uid, None)

    if not found:
        await cb.message.answer("Не получилось выбить новые значения, попробуй ещё раз.")
        return

    await asyncio.sleep(ANIMATION_DELAY)

    for val in sorted(found):
        kb = InlineKeyboardBuilder()
        for i, opt in enumerate(OPTIONS[gid]):
            kb.button(text=opt, callback_data=f"lc:{gid}:{val}:{i}")
        kb.adjust(2)
        await bot.send_message(
            uid, "Что показала анимация на этом броске?",
            reply_parameters=ReplyParameters(message_id=found[val]),
            reply_markup=kb.as_markup()
        )

    if len(found) < len(missing):
        await cb.message.answer("Часть значений не выпала за лимит бросков. Запусти обучение ещё раз после ответов.")


@dp.callback_query(F.data.startswith("lc:"))
async def learn_confirm(cb: types.CallbackQuery):
    _, gid, val, idx = cb.data.split(":")
    opts = OPTIONS[gid]
    idx = int(idx)
    if idx >= len(opts):
        await cb.answer("Неверный вариант", show_alert=True)
        return

    name = opts[idx]
    clash = [k for k, v in store.labels(gid).items() if v == name and k != val]
    store.set_label(gid, val, name)
    done = len(store.labels(gid))
    total = GAMES[gid]["max"]

    text = f"✅ Запомнил: <b>{val}</b> = <b>{name}</b>.\nОбучено для {GAMES[gid]['name']}: {done} / {total}."
    if clash:
        text += f"\n⚠️ Такое же название уже стоит у значения {', '.join(sorted(clash))}. Если это ошибка, переучи игру."
    if done == total:
        text += "\n🎉 Все значения изучены."
    await safe_edit(cb.message, text)
    await cb.answer()


@dp.callback_query(F.data.startswith("lr:"))
async def learn_reset(cb: types.CallbackQuery):
    gid = cb.data.split(":")[1]
    store.reset_labels(gid)
    kb = InlineKeyboardBuilder()
    kb.button(text="🎓 Начать обучение", callback_data=f"ls:{gid}")
    kb.button(text="⬅️ Назад", callback_data="learn")
    kb.adjust(1)
    await safe_edit(cb.message, f"Обучение для {GAMES[gid]['name']} сброшено.", kb.as_markup())
    await cb.answer()


async def health(request):
    return web.Response(text="ok")


async def on_startup(app: web.Application):
    url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
    await bot.set_webhook(url, drop_pending_updates=True, allowed_updates=dp.resolve_used_update_types())
    logging.info(f"Webhook set to: {url}")


async def on_shutdown(app: web.Application):
    for ev in active.values():
        ev.set()
    store.close()


def main():
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
