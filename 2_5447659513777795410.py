"""
Telegram-бот: Resellers -> Counter Flame -> цены -> кол-во ключей -> оплата.
Админ-панель, персональные пароли, блокировка после 3 неверных попыток.

Установка:  pip install aiogram
Запуск:     BOT_TOKEN=123:ABC ADMIN_IDS=111111,222222 python bot.py
"""
import asyncio
import hashlib
import logging
import os
import secrets
import sqlite3

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("9250971448:AAGh7kpIYv4C4iY9yR8YTlg94OGFQg8aVQQ")
ADMIN_IDS = {int(x) for x in os.getenv("6780071717", "0").split(",") if x.strip()}
CURRENCY = "$"
MAX_ATTEMPTS = 3
DEFAULT_CATEGORY = "Counter Flame"
# Ссылка на оплату — вставишь сюда позже. {order_id} и {amount} подставятся сами.
PAYMENT_URL = "T-g4BYEaHprp4-zu9UndOI7AVsXaLpRnd-sFHEDutVg"
# ===================================================

logging.basicConfig(level=logging.INFO)
router = Router()
AUTHED: set[int] = set()  # кто сейчас ввёл верный пароль

# ---------------------- БАЗА ----------------------
db = sqlite3.connect("shop.db")
db.row_factory = sqlite3.Row


def q(sql, args=(), one=False):
    cur = db.execute(sql, args)
    db.commit()
    return cur.fetchone() if one else cur.fetchall()


def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, pw TEXT, attempts INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, name TEXT, price REAL);
    CREATE TABLE IF NOT EXISTS keys(
        id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, key TEXT,
        used INTEGER DEFAULT 0, order_id INTEGER);
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
        qty INTEGER, total REAL, status TEXT DEFAULT 'new');
    """)
    if not q("SELECT 1 FROM products LIMIT 1"):
        for name, price in (("1 день", 5), ("7 дней", 15), ("30 дней", 40)):
            q("INSERT INTO products(category,name,price) VALUES(?,?,?)",
              (DEFAULT_CATEGORY, name, price))


def hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.sha256((salt + pw).encode()).hexdigest()}"


def check_pw(pw: str, stored: str) -> bool:
    salt = stored.split("$")[0]
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def stock(pid: int) -> int:
    return q("SELECT COUNT(*) c FROM keys WHERE product_id=? AND used=0", (pid,), one=True)["c"]


# ---------------------- ХЕЛПЕРЫ ----------------------
def kb(*rows):
    out = []
    for row in rows:
        out.append([
            InlineKeyboardButton(text=t, url=d) if d.startswith("http")
            else InlineKeyboardButton(text=t, callback_data=d)
            for t, d in row
        ])
    return InlineKeyboardMarkup(inline_keyboard=out)


async def screen(ev, text, markup=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup)
        except Exception:
            await ev.message.answer(text, reply_markup=markup)
        await ev.answer()
    else:
        await ev.answer(text, reply_markup=markup)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


async def guard(c: CallbackQuery) -> bool:
    if c.from_user.id in AUTHED or is_admin(c.from_user.id):
        return True
    await c.answer("Нажми /start и введи пароль", show_alert=True)
    return False


def main_menu(uid):
    rows = [[("🛒 Resellers", "res")]]
    if is_admin(uid):
        rows.append([("⚙️ Админ-панель", "adm")])
    return kb(*rows)


# ---------------------- СОСТОЯНИЯ ----------------------
class Auth(StatesGroup):
    password = State()


class A(StatesGroup):
    keys = State()
    price = State()
    prod_name = State()
    prod_price = State()
    user_id = State()
    user_pw = State()
    unblock = State()
    broadcast = State()


# ---------------------- /start И ПАРОЛЬ ----------------------
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    AUTHED.discard(uid)

    if is_admin(uid):
        AUTHED.add(uid)
        await m.answer("👋 Главное меню", reply_markup=main_menu(uid))
        return

    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        await m.answer("⛔ У тебя нет доступа. Обратись к администратору.")
    elif u["blocked"]:
        await m.answer("🚫 Вход заблокирован. Обратись к администратору.")
    else:
        await state.set_state(Auth.password)
        await m.answer("🔐 Введи пароль:")


@router.message(Auth.password)
async def auth_password(m: Message, state: FSMContext):
    uid = m.from_user.id
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    try:
        await m.delete()  # убираем пароль из чата
    except Exception:
        pass
    if not u or u["blocked"]:
        await state.clear()
        await m.answer("🚫 Вход заблокирован.")
        return

    if check_pw((m.text or "").strip(), u["pw"]):
        q("UPDATE users SET attempts=0 WHERE id=?", (uid,))
        AUTHED.add(uid)
        await state.clear()
        await m.answer("✅ Добро пожаловать!", reply_markup=main_menu(uid))
        return

    attempts = u["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        q("UPDATE users SET attempts=?, blocked=1 WHERE id=?", (attempts, uid))
        await state.clear()
        await m.answer("🚫 3 неверные попытки. Вход заблокирован.")
    else:
        q("UPDATE users SET attempts=? WHERE id=?", (attempts, uid))
        await m.answer(f"❌ Неверный пароль. Осталось попыток: {MAX_ATTEMPTS - attempts}")


# ---------------------- МАГАЗИН ----------------------
@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    if await guard(c):
        await screen(c, "👋 Главное меню", main_menu(c.from_user.id))


@router.callback_query(F.data == "res")
async def cb_res(c: CallbackQuery):
    if not await guard(c):
        return
    cats = q("SELECT DISTINCT category FROM products")
    rows = [[(r["category"], f"cat:{r['category']}")] for r in cats]
    rows.append([("⬅️ Назад", "menu")])
    await screen(c, "🛒 <b>Resellers</b>\nВыбери товар:", kb(*rows))


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(c: CallbackQuery):
    if not await guard(c):
        return
    cat = c.data[4:]
    prods = q("SELECT * FROM products WHERE category=?", (cat,))
    lines, rows = [f"🔥 <b>{cat}</b> — цены:\n"], []
    for p in prods:
        s = stock(p["id"])
        lines.append(f"• {p['name']} — {p['price']:g}{CURRENCY} (в наличии: {s})")
        rows.append([(f"{p['name']} — {p['price']:g}{CURRENCY}", f"prod:{p['id']}")])
    rows.append([("⬅️ Назад", "res")])
    await screen(c, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("prod:"))
async def cb_prod(c: CallbackQuery):
    if not await guard(c):
        return
    pid = int(c.data[5:])
    p = q("SELECT * FROM products WHERE id=?", (pid,), one=True)
    s = stock(pid)
    back = [("⬅️ Назад", f"cat:{p['category']}")]
    if s == 0:
        await screen(c, f"<b>{p['name']}</b>\n❌ Нет в наличии.", kb(back))
        return
    opts = [n for n in (1, 5, 10, 25, 50, 100) if n <= s]
    rows = [[(f"{n} шт — {p['price'] * n:g}{CURRENCY}", f"qty:{pid}:{n}") for n in opts[i:i + 2]]
            for i in range(0, len(opts), 2)]
    rows.append(back)
    await screen(c, f"<b>{p['category']} · {p['name']}</b>\n"
                    f"Цена: {p['price']:g}{CURRENCY} за ключ\nВ наличии: {s}\n\n"
                    f"Сколько ключей нужно?", kb(*rows))


@router.callback_query(F.data.startswith("qty:"))
async def cb_qty(c: CallbackQuery):
    if not await guard(c):
        return
    _, pid, n = c.data.split(":")
    pid, n = int(pid), int(n)
    p = q("SELECT * FROM products WHERE id=?", (pid,), one=True)
    if stock(pid) < n:
        await c.answer("Столько ключей уже нет", show_alert=True)
        return
    total = round(p["price"] * n, 2)
    oid = q("INSERT INTO orders(user_id,product_id,qty,total) VALUES(?,?,?,?)",
            (c.from_user.id, pid, n, total)) or None
    oid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    url = PAYMENT_URL.format(order_id=oid, amount=total)
    rows = []
    if url.startswith("http"):
        rows.append([("💳 Оплатить", url)])
    rows.append([("✅ Я оплатил", f"paid:{oid}")])
    rows.append([("⬅️ Назад", f"prod:{pid}")])
    await screen(c, f"🧾 <b>Заказ #{oid}</b>\n{p['category']} · {p['name']}\n"
                    f"Ключей: {n}\nИтого: <b>{total:g}{CURRENCY}</b>\n\n"
                    f"Оплати по ссылке и нажми «Я оплатил».", kb(*rows))


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(c: CallbackQuery, bot: Bot):
    if not await guard(c):
        return
    oid = int(c.data[5:])
    o = q("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, c.from_user.id), one=True)
    if not o or o["status"] != "new":
        await c.answer("Заказ уже обработан", show_alert=True)
        return
    q("UPDATE orders SET status='waiting' WHERE id=?", (oid,))
    p = q("SELECT * FROM products WHERE id=?", (o["product_id"],), one=True)
    for a in ADMIN_IDS:
        try:
            await bot.send_message(
                a, f"💰 Заказ #{oid} от <code>{o['user_id']}</code> (@{c.from_user.username})\n"
                   f"{p['category']} · {p['name']} × {o['qty']} = {o['total']:g}{CURRENCY}",
                reply_markup=kb([("✅ Выдать ключи", f"ok:{oid}"), ("❌ Отклонить", f"no:{oid}")]))
        except Exception:
            pass
    await screen(c, "⏳ Заявка отправлена. Ключи придут после проверки оплаты.",
                 kb([("⬅️ В меню", "menu")]))


@router.callback_query(F.data.startswith("ok:"), F.from_user.id.in_(ADMIN_IDS))
async def cb_ok(c: CallbackQuery, bot: Bot):
    oid = int(c.data[3:])
    o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not o or o["status"] != "waiting":
        await c.answer("Уже обработан", show_alert=True)
        return
    ks = q("SELECT * FROM keys WHERE product_id=? AND used=0 LIMIT ?", (o["product_id"], o["qty"]))
    if len(ks) < o["qty"]:
        await c.answer("Не хватает ключей на складе!", show_alert=True)
        return
    for k in ks:
        q("UPDATE keys SET used=1, order_id=? WHERE id=?", (oid, k["id"]))
    q("UPDATE orders SET status='done' WHERE id=?", (oid,))
    text = "\n".join(f"<code>{k['key']}</code>" for k in ks)
    await bot.send_message(o["user_id"], f"✅ Заказ #{oid} оплачен. Твои ключи:\n\n{text}")
    await c.message.edit_text(c.message.text + "\n\n✅ Ключи выданы")
    await c.answer()


@router.callback_query(F.data.startswith("no:"), F.from_user.id.in_(ADMIN_IDS))
async def cb_no(c: CallbackQuery, bot: Bot):
    oid = int(c.data[3:])
    o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if o and o["status"] == "waiting":
        q("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
        await bot.send_message(o["user_id"], f"❌ Заказ #{oid} отклонён. Оплата не найдена.")
    await c.message.edit_text(c.message.text + "\n\n❌ Отклонён")
    await c.answer()


# ---------------------- АДМИН-ПАНЕЛЬ ----------------------
ADM = F.from_user.id.in_(ADMIN_IDS)
ADM_KB = kb(
    [("➕ Добавить ключи", "a_keys")],
    [("📢 Объявление", "a_bc")],
    [("💰 Изменить цену", "a_price")],
    [("🆕 Добавить товар", "a_prod")],
    [("🔑 Создать пароль юзеру", "a_user")],
    [("🔓 Разблокировать юзера", "a_unblock")],
    [("📊 Склад", "a_stat")],
    [("⬅️ Назад", "menu")],
)


def products_kb(prefix):
    rows = [[(f"{p['category']} · {p['name']} ({p['price']:g}{CURRENCY}) [{stock(p['id'])}]",
              f"{prefix}:{p['id']}")] for p in q("SELECT * FROM products")]
    rows.append([("⬅️ Назад", "adm")])
    return kb(*rows)


@router.message(Command("admin"), ADM)
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ Админ-панель", reply_markup=ADM_KB)


@router.callback_query(F.data == "adm", ADM)
async def cb_adm(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen(c, "⚙️ Админ-панель", ADM_KB)


@router.callback_query(F.data == "a_stat", ADM)
async def cb_stat(c: CallbackQuery):
    lines = [f"{p['category']} · {p['name']}: {stock(p['id'])} ключей"
             for p in q("SELECT * FROM products")]
    users = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    await screen(c, "📊 <b>Склад</b>\n" + "\n".join(lines) + f"\n\nПользователей: {users}",
                 kb([("⬅️ Назад", "adm")]))


# --- добавить ключи
@router.callback_query(F.data == "a_keys", ADM)
async def a_keys(c: CallbackQuery):
    await screen(c, "Для какого товара добавить ключи?", products_kb("ak"))


@router.callback_query(F.data.startswith("ak:"), ADM)
async def ak(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.keys)
    await state.update_data(pid=int(c.data[3:]))
    await screen(c, "Отправь ключи — каждый с новой строки:")


@router.message(A.keys, ADM)
async def keys_in(m: Message, state: FSMContext):
    pid = (await state.get_data())["pid"]
    ks = [k.strip() for k in (m.text or "").splitlines() if k.strip()]
    for k in ks:
        q("INSERT INTO keys(product_id,key) VALUES(?,?)", (pid, k))
    await state.clear()
    await m.answer(f"✅ Добавлено ключей: {len(ks)}. Всего в наличии: {stock(pid)}",
                   reply_markup=ADM_KB)


# --- изменить цену
@router.callback_query(F.data == "a_price", ADM)
async def a_price(c: CallbackQuery):
    await screen(c, "У какого товара изменить цену?", products_kb("ap"))


@router.callback_query(F.data.startswith("ap:"), ADM)
async def ap(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.price)
    await state.update_data(pid=int(c.data[3:]))
    await screen(c, "Введи новую цену (число):")


@router.message(A.price, ADM)
async def price_in(m: Message, state: FSMContext):
    try:
        price = float((m.text or "").replace(",", "."))
    except ValueError:
        await m.answer("Нужно число, например 12.5")
        return
    pid = (await state.get_data())["pid"]
    q("UPDATE products SET price=? WHERE id=?", (price, pid))
    await state.clear()
    await m.answer(f"✅ Цена изменена: {price:g}{CURRENCY}", reply_markup=ADM_KB)


# --- добавить товар
@router.callback_query(F.data == "a_prod", ADM)
async def a_prod(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.prod_name)
    await screen(c, f"Название товара (например «90 дней»).\nКатегория: {DEFAULT_CATEGORY}")


@router.message(A.prod_name, ADM)
async def prod_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(A.prod_price)
    await m.answer("Цена (число):")


@router.message(A.prod_price, ADM)
async def prod_price(m: Message, state: FSMContext):
    try:
        price = float((m.text or "").replace(",", "."))
    except ValueError:
        await m.answer("Нужно число, например 12.5")
        return
    name = (await state.get_data())["name"]
    q("INSERT INTO products(category,name,price) VALUES(?,?,?)", (DEFAULT_CATEGORY, name, price))
    await state.clear()
    await m.answer(f"✅ Товар «{name}» добавлен.", reply_markup=ADM_KB)


# --- пароль для пользователя
@router.callback_query(F.data == "a_user", ADM)
async def a_user(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.user_id)
    await screen(c, "Введи Telegram ID пользователя (числом):")


@router.message(A.user_id, ADM)
async def user_id_in(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        await m.answer("ID — только цифры.")
        return
    await state.update_data(uid=int(m.text.strip()))
    await state.set_state(A.user_pw)
    await m.answer("Введи пароль для него или «-» чтобы сгенерировать:")


@router.message(A.user_pw, ADM)
async def user_pw_in(m: Message, state: FSMContext):
    uid = (await state.get_data())["uid"]
    pw = (m.text or "").strip()
    if pw == "-":
        pw = secrets.token_urlsafe(6)
    q("""INSERT INTO users(id,pw,attempts,blocked) VALUES(?,?,0,0)
         ON CONFLICT(id) DO UPDATE SET pw=excluded.pw, attempts=0, blocked=0""",
      (uid, hash_pw(pw)))
    await state.clear()
    await m.answer(f"✅ Пароль для <code>{uid}</code>: <code>{pw}</code>\nПередай ему.",
                   reply_markup=ADM_KB)


# --- разблокировка
@router.callback_query(F.data == "a_unblock", ADM)
async def a_unblock(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.unblock)
    blocked = q("SELECT id FROM users WHERE blocked=1")
    txt = ", ".join(str(b["id"]) for b in blocked) or "нет"
    await screen(c, f"Заблокированы: {txt}\n\nВведи ID для разблокировки:")


@router.message(A.unblock, ADM)
async def unblock_in(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        await m.answer("ID — только цифры.")
        return
    q("UPDATE users SET blocked=0, attempts=0 WHERE id=?", (int(m.text),))
    await state.clear()
    await m.answer("✅ Разблокирован.", reply_markup=ADM_KB)


# --- объявление
@router.callback_query(F.data == "a_bc", ADM)
async def a_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.broadcast)
    await screen(c, "Отправь текст объявления — получат все пользователи:")


@router.message(A.broadcast, ADM)
async def bc_in(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    sent = 0
    for u in q("SELECT id FROM users WHERE blocked=0"):
        try:
            await bot.send_message(u["id"], f"📢 <b>Объявление</b>\n\n{m.html_text}")
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Отправлено: {sent}", reply_markup=ADM_KB)


# ---------------------- ЗАПУСК ----------------------
async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
