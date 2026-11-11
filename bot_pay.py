"""
Telegram-бот: Resellers -> Counter Flame -> цены -> кол-во ключей -> оплата.
Админ-панель, персональные пароли, блокировка после 3 неверных попыток.

Установка:  pip install aiogram rollypay
Запуск:     просто python bot.py (токен и ID вписаны в код ниже)
"""
import asyncio
import hashlib
import html
import logging
import os
import secrets
import sqlite3
import time

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
try:
    from rollypay import RollyPayClient
except ImportError:  # хостинг не поставил библиотеку — ставим сами
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rollypay"])
    from rollypay import RollyPayClient

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "9250971448:AAGh7kpIYv4C4iY9yR8YTlg94OGFQg8aVQQ"  # между кавычек
ADMIN_IDS = {6780071717}  # ВСТАВЬ СВОЙ TELEGRAM ID вместо 123456789
CURRENCY = "₽"
MAX_ATTEMPTS = 3
DEFAULT_CATEGORY = "Counter Flame"
# --- RollyPay ---
ROLLY_API_KEY = ""  # между кавычек
# --- CryptoBot (@CryptoBot -> Crypto Pay -> Create App -> API Token) ---
CRYPTO_TOKEN = "674421:AAjehbhOX3MyQPTFfAL4sLg0PzfSuQxAOaI"  # между кавычек
CRYPTO_ASSETS = None    # None = любые монеты; или "USDT,TON" чтобы ограничить
PAYMENT_METHOD = None   # None = клиент выберет сам; или "sbp" / "card" / "usdt"
PRIVACY_URL = "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99"
OFFER_URL = "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15"
MIN_TOPUP = 50          # минимальное пополнение баланса, ₽
PAY_TIMEOUT = 3600      # сколько секунд ждём оплату (1 час)
# ===================================================

logging.basicConfig(level=logging.INFO)
router = Router()
AUTHED: set[int] = set()  # кто сейчас ввёл верный пароль
TASKS: set = set()        # фоновые проверки оплаты
rolly = RollyPayClient(api_key=ROLLY_API_KEY)
ROLLY_ON = not ROLLY_API_KEY.startswith("ВСТАВЬ")
CRYPTO_ON = not CRYPTO_TOKEN.startswith("ВСТАВЬ")

# ---------------------- БАЗА ----------------------
db = sqlite3.connect("shop.db")
db.row_factory = sqlite3.Row


def q(sql, args=(), one=False):
    cur = db.execute(sql, args)
    db.commit()
    return cur.fetchone() if one else cur.fetchall()


def ins(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, pw TEXT, attempts INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, name TEXT, price REAL);
    CREATE TABLE IF NOT EXISTS keys(
        id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, key TEXT,
        used INTEGER DEFAULT 0, order_id INTEGER);
    CREATE TABLE IF NOT EXISTS seen(
        id INTEGER PRIMARY KEY, ts INTEGER);
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
        qty INTEGER, total REAL, status TEXT DEFAULT 'new');
    """)
    if not q("SELECT 1 FROM products LIMIT 1"):
        for name, price in (("1 день", 150), ("7 дней", 450), ("30 дней", 1200)):
            q("INSERT INTO products(category,name,price) VALUES(?,?,?)",
              (DEFAULT_CATEGORY, name, price))


def migrate():
    for col in ("verified INTEGER DEFAULT 0", "balance REAL DEFAULT 0"):
        try:
            db.execute(f"ALTER TABLE users ADD COLUMN {col}")
            db.commit()
        except sqlite3.OperationalError:
            pass
    for col in ("payment_id TEXT", "created INTEGER", "method TEXT", "kind TEXT DEFAULT 'buy'"):
        try:
            db.execute(f"ALTER TABLE orders ADD COLUMN {col}")
            db.commit()
        except sqlite3.OperationalError:
            pass


def hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.sha256((salt + pw).encode()).hexdigest()}"


def check_pw(pw: str, stored: str) -> bool:
    salt = stored.split("$")[0]
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def stock(pid: int) -> int:
    return q("SELECT COUNT(*) c FROM keys WHERE product_id=? AND used=0", (pid,), one=True)["c"]


def get_balance(uid: int) -> float:
    if is_admin(uid):
        q("INSERT OR IGNORE INTO users(id,attempts,blocked,verified,balance) VALUES(?,0,0,1,0)", (uid,))
    r = q("SELECT balance FROM users WHERE id=?", (uid,), one=True)
    return round(r["balance"] or 0, 2) if r else 0.0


def who(u) -> str:
    un = f"@{u.username}" if u.username else "без username"
    return f"{html.escape(u.full_name)} ({un}, ID <code>{u.id}</code>)"


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
    uid = c.from_user.id
    if is_admin(uid):
        return True
    u = q("SELECT verified, blocked FROM users WHERE id=?", (uid,), one=True)
    if u and u["verified"] and not u["blocked"]:
        return True
    await c.answer("Нажми /start и введи пароль", show_alert=True)
    return False


def main_menu(uid):
    rows = [[("🛒 Resellers", "res")], [("👤 Профиль", "profile")]]
    if is_admin(uid):
        rows.append([("⚙️ Админ-панель", "adm")])
    return kb(*rows)


# ---------------------- СОСТОЯНИЯ ----------------------
class Auth(StatesGroup):
    password = State()


class Buy(StatesGroup):
    qty = State()
    topup = State()


class A(StatesGroup):
    keys = State()
    price = State()
    prod_name = State()
    prod_price = State()
    user_id = State()
    user_pw = State()
    unblock = State()
    broadcast = State()
    bal_id = State()
    bal_amt = State()


# ---------------------- /start И ПАРОЛЬ ----------------------
@router.message(CommandStart())
async def start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uid = m.from_user.id

    if is_admin(uid):
        AUTHED.add(uid)
        await m.answer("👋 Главное меню", reply_markup=main_menu(uid))
        return

    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    has_access = bool(u and u["pw"])

    # уведомление админам о новом пользователе (один раз)
    cur = db.execute("INSERT OR IGNORE INTO seen(id,ts) VALUES(?,?)", (uid, int(time.time())))
    db.commit()
    if cur.rowcount:
        await notify_admins(
            bot, f"🆕 <b>Новый пользователь в боте</b>\n{who(m.from_user)}\n"
                 f"Доступ: {'есть' if has_access else 'нет'}",
            None if has_access else kb([("🔑 Выдать доступ", f"grant:{uid}")]))

    if not has_access:
        await m.answer("⛔ У тебя нет доступа. Обратись к администратору.")
    elif u["blocked"]:
        await m.answer("🚫 Вход заблокирован. Обратись к администратору.")
    elif u["verified"]:
        AUTHED.add(uid)
        await m.answer("👋 Главное меню", reply_markup=main_menu(uid))
    else:
        await state.set_state(Auth.password)
        await m.answer("🔐 Введи пароль:")


@router.message(Auth.password)
async def auth_password(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    try:
        await m.delete()  # убираем пароль из чата
    except Exception:
        pass
    if not u or u["blocked"] or not u["pw"]:
        await state.clear()
        await m.answer("🚫 Вход заблокирован.")
        return

    if check_pw((m.text or "").strip(), u["pw"]):
        q("UPDATE users SET attempts=0, verified=1 WHERE id=?", (uid,))
        AUTHED.add(uid)
        await state.clear()
        await m.answer("✅ Добро пожаловать!", reply_markup=main_menu(uid))
        await notify_admins(bot, f"✅ <b>Вошёл в бота</b>\n{who(m.from_user)}")
        return

    attempts = u["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        q("UPDATE users SET attempts=?, blocked=1, verified=0 WHERE id=?", (attempts, uid))
        await state.clear()
        await m.answer("🚫 3 неверные попытки. Вход заблокирован.")
        await notify_admins(bot, f"🚫 <b>Заблокирован за неверный пароль</b>\n{who(m.from_user)}",
                            kb([("🔓 Разблокировать", f"unb:{uid}")]))
    else:
        q("UPDATE users SET attempts=? WHERE id=?", (attempts, uid))
        await m.answer(f"❌ Неверный пароль. Осталось попыток: {MAX_ATTEMPTS - attempts}")
        await notify_admins(bot, f"⚠️ Неверный пароль ({attempts}/{MAX_ATTEMPTS})\n{who(m.from_user)}")


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
    rows.append([("✏️ Своё количество", f"custom:{pid}")])
    rows.append(back)
    await screen(c, f"<b>{p['category']} · {p['name']}</b>\n"
                    f"Цена: {p['price']:g}{CURRENCY} за ключ\nВ наличии: {s}\n\n"
                    f"Сколько ключей нужно?", kb(*rows))


def create_payment(oid, total, uid):
    kw = dict(amount=f"{total:.2f}", order_id=f"order_{oid}",
              description=f"Заказ #{oid}", customer_id=str(uid))
    if PAYMENT_METHOD:
        kw["payment_method"] = PAYMENT_METHOD
    return rolly.payments.create(**kw)


def fetch_status(payment_id):
    return str(rolly.payments.get(payment_id)["status"]).lower()


async def crypto_call(method: str, params: dict):
    async with aiohttp.ClientSession() as sess:
        async with sess.post("https://pay.crypt.bot/api/" + method, json=params,
                             headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN},
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error")))
    return data["result"]


async def crypto_create(oid: int, total: float):
    params = {"currency_type": "fiat", "fiat": "RUB", "amount": f"{total:.2f}",
              "description": f"Заказ #{oid}", "payload": f"order_{oid}",
              "expires_in": PAY_TIMEOUT}
    if CRYPTO_ASSETS:
        params["accepted_assets"] = CRYPTO_ASSETS
    inv = await crypto_call("createInvoice", params)
    return str(inv["invoice_id"]), inv.get("bot_invoice_url") or inv.get("pay_url")


async def send_crypto_receipt(bot: Bot, o, p):
    """Чек об оплате через CryptoBot (данные по монете берём из счёта, если есть)."""
    paid = ""
    try:
        res = await crypto_call("getInvoices", {"invoice_ids": str(o["payment_id"])})
        inv = res["items"][0]
        if inv.get("paid_asset") and inv.get("paid_amount"):
            paid = f"\nОплачено: <b>{inv['paid_amount']} {inv['paid_asset']}</b>"
    except Exception:
        logging.exception("receipt info failed")
    when = time.strftime("%d.%m.%Y %H:%M", time.gmtime())
    await bot.send_message(
        o["user_id"],
        f"🧾 <b>Чек об оплате</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Заказ: #{o['id']}\n"
        f"Товар: {'Пополнение баланса' if o['kind'] == 'topup' else p['category'] + ' · ' + p['name'] + ' × ' + str(o['qty'])}\n"
        f"Сумма: <b>{o['total']:g}{CURRENCY}</b>{paid}\n"
        f"Способ: CryptoBot\n"
        f"Дата: {when} (UTC)\n"
        f"Статус: ✅ Оплачено")


async def get_status(o) -> str:
    if o["method"] == "crypto":
        res = await crypto_call("getInvoices", {"invoice_ids": str(o["payment_id"])})
        return str(res["items"][0]["status"]).lower()
    return await asyncio.to_thread(fetch_status, o["payment_id"])


OK_STATUSES = {"succeeded", "success", "paid", "completed", "complete"}
BAD_STATUSES = {"failed", "expired", "canceled", "cancelled", "declined", "rejected"}


async def notify_admins(bot: Bot, text: str, markup=None):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text, reply_markup=markup)
        except Exception:
            pass


async def fulfill(bot: Bot, oid: int, from_status: str = "new") -> bool:
    """Выдать ключи за оплаченный заказ (один раз)."""
    cur = db.execute("UPDATE orders SET status='paid' WHERE id=? AND status=?", (oid, from_status))
    db.commit()
    if cur.rowcount == 0:
        return False
    o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if o["kind"] == "topup":
        get_balance(o["user_id"])
        q("UPDATE users SET balance=balance+? WHERE id=?", (o["total"], o["user_id"]))
        q("UPDATE orders SET status='done' WHERE id=?", (oid,))
        if o["method"] == "crypto":
            await send_crypto_receipt(bot, o, None)
        await bot.send_message(o["user_id"], f"✅ Баланс пополнен на <b>{o['total']:g}{CURRENCY}</b>.\n"
                                             f"Текущий баланс: <b>{get_balance(o['user_id']):g}{CURRENCY}</b>")
        await notify_admins(bot, f"➕ Пополнение баланса: {o['total']:g}{CURRENCY} "
                                 f"({o['method']}, юзер <code>{o['user_id']}</code>)")
        return True
    p = q("SELECT * FROM products WHERE id=?", (o["product_id"],), one=True)
    if o["method"] == "crypto":
        await send_crypto_receipt(bot, o, p)
    ks = q("SELECT * FROM keys WHERE product_id=? AND used=0 LIMIT ?", (o["product_id"], o["qty"]))
    if len(ks) < o["qty"]:
        q("UPDATE orders SET status='paid_nostock' WHERE id=?", (oid,))
        await bot.send_message(o["user_id"], f"✅ Заказ #{oid} оплачен, но ключи закончились. "
                                             f"Админ уже уведомлён — ключи придут сюда, как только появятся.")
        await notify_admins(bot, f"⚠️ Заказ #{oid} ОПЛАЧЕН, но не хватает ключей "
                                 f"({p['name']} × {o['qty']}). Добавь ключи — выдам автоматически.")
        return True
    for k in ks:
        q("UPDATE keys SET used=1, order_id=? WHERE id=?", (oid, k["id"]))
    q("UPDATE orders SET status='done' WHERE id=?", (oid,))
    text = "\n".join(f"<code>{k['key']}</code>" for k in ks)
    await bot.send_message(o["user_id"], f"✅ Заказ #{oid} оплачен. Твои ключи:\n\n{text}")
    await notify_admins(bot, f"💰 Продажа: заказ #{oid}, {p['category']} · {p['name']} × {o['qty']} "
                             f"= {o['total']:g}{CURRENCY} ({o['method']}, юзер <code>{o['user_id']}</code>)")
    return True


async def watch_payment(bot: Bot, oid: int):
    waited = 0
    while waited < PAY_TIMEOUT:
        await asyncio.sleep(10)
        waited += 10
        o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
        if not o or o["status"] != "new":
            return
        try:
            st = await get_status(o)
        except Exception:
            logging.exception("status check failed")
            continue
        if st in OK_STATUSES:
            await fulfill(bot, oid)
            return
        if st in BAD_STATUSES:
            q("UPDATE orders SET status='failed' WHERE id=? AND status='new'", (oid,))
            await bot.send_message(o["user_id"], f"❌ Заказ #{oid}: платёж не прошёл или истёк.")
            return
    q("UPDATE orders SET status='expired' WHERE id=? AND status='new'", (oid,))


def start_watch(bot: Bot, oid: int):
    t = asyncio.create_task(watch_payment(bot, oid))
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)


async def make_order(ev, bot: Bot, uid: int, pid: int, n: int):
    p = q("SELECT * FROM products WHERE id=?", (pid,), one=True)
    if n < 1 or stock(pid) < n:
        await screen(ev, f"❌ Столько ключей нет. В наличии: {stock(pid)}",
                     kb([("⬅️ Назад", f"prod:{pid}")]))
        return
    total = round(p["price"] * n, 2)
    oid = ins("INSERT INTO orders(user_id,product_id,qty,total,created) VALUES(?,?,?,?,?)",
              (uid, pid, n, total, int(time.time())))
    rows = []
    bal = get_balance(uid)
    if bal >= total:
        rows.append([(f"👛 С баланса ({bal:g}{CURRENCY})", f"pay:{oid}:bal")])
    if ROLLY_ON:
        rows.append([("💳 Карта / СБП", f"pay:{oid}:rolly")])
    if CRYPTO_ON:
        rows.append([("🪙 Криптой (CryptoBot)", f"pay:{oid}:crypto")])
    rows.append([("⬅️ Назад", f"prod:{pid}")])
    await screen(ev, f"🧾 <b>Заказ #{oid}</b>\n{p['category']} · {p['name']}\n"
                     f"Ключей: {n}\nИтого: <b>{total:g}{CURRENCY}</b>\n\n"
                     f"Выбери способ оплаты:", kb(*rows))


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(c: CallbackQuery, bot: Bot):
    if not await guard(c):
        return
    _, oid, method = c.data.split(":")
    oid = int(oid)
    o = q("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, c.from_user.id), one=True)
    if not o or o["status"] != "new" or o["payment_id"]:
        await c.answer("Платёж по этому заказу уже создан или заказ неактивен", show_alert=True)
        return
    if method == "bal":
        if o["kind"] == "topup":
            await c.answer("Баланс нельзя пополнить с баланса", show_alert=True)
            return
        get_balance(c.from_user.id)
        cur = db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                         (o["total"], c.from_user.id, o["total"]))
        db.commit()
        if cur.rowcount == 0:
            await c.answer("Недостаточно средств на балансе", show_alert=True)
            return
        q("UPDATE orders SET payment_id='balance', method='balance' WHERE id=?", (oid,))
        await screen(c, f"✅ Заказ #{oid} оплачен с баланса.", kb([("⬅️ В меню", "menu")]))
        await fulfill(bot, oid)
        return
    try:
        if method == "crypto":
            payment_id, url = await crypto_create(oid, o["total"])
        else:
            pay = await asyncio.to_thread(create_payment, oid, o["total"], c.from_user.id)
            payment_id, url = pay["payment_id"], pay["pay_url"]
    except Exception as e:
        logging.exception("create payment failed")
        q("UPDATE orders SET status='error' WHERE id=?", (oid,))
        await notify_admins(bot, f"⚠️ Ошибка оплаты ({method}) при создании платежа (заказ #{oid}): {e}")
        await screen(c, "❌ Не удалось создать платёж. Попробуй позже или выбери другой способ.",
                     kb([("⬅️ В меню", "menu")]))
        return
    q("UPDATE orders SET payment_id=?, method=? WHERE id=?", (payment_id, method, oid))
    start_watch(bot, oid)
    await screen(c, f"🧾 <b>Заказ #{oid}</b>\nИтого: <b>{o['total']:g}{CURRENCY}</b>\n\n"
                    f"{'Оплати по ссылке — баланс пополнится автоматически сразу после оплаты.' if o['kind'] == 'topup' else 'Оплати по ссылке — ключи придут сюда автоматически сразу после оплаты.'}",
                 kb([("💳 Оплатить", url)],
                    [("🔄 Проверить оплату", f"chk:{oid}")],
                    [("⬅️ В меню", "menu")]))


# ---------------------- ПРОФИЛЬ И БАЛАНС ----------------------
@router.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    if not await guard(c):
        return
    uid = c.from_user.id
    bal = get_balance(uid)
    cnt = q("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='done' "
            "AND COALESCE(kind,'buy')='buy'", (uid,), one=True)["c"]
    un = f"@{c.from_user.username}" if c.from_user.username else "—"
    await screen(c, f"👤 <b>Профиль</b>\n\n"
                    f"ID: <code>{uid}</code>\n"
                    f"Username: {un}\n"
                    f"💰 Баланс: <b>{bal:g}{CURRENCY}</b>\n"
                    f"🛒 Покупок: {cnt}",
                 kb([("➕ Пополнить баланс", "topup")],
                    [("📄 Политика конфиденциальности", PRIVACY_URL)],
                    [("📜 Пользовательское соглашение", OFFER_URL)],
                    [("⬅️ Назад", "menu")]))


@router.callback_query(F.data == "topup")
async def cb_topup(c: CallbackQuery):
    if not await guard(c):
        return
    amounts = [100, 300, 500, 1000, 2000, 5000]
    rows = [[(f"{a}{CURRENCY}", f"tu:{a}") for a in amounts[i:i + 3]] for i in range(0, len(amounts), 3)]
    rows.append([("✏️ Другая сумма", "tu_c")])
    rows.append([("⬅️ Назад", "profile")])
    await screen(c, f"➕ <b>Пополнение баланса</b>\nВыбери сумму (минимум {MIN_TOPUP}{CURRENCY}):", kb(*rows))


async def make_topup(ev, uid: int, amount: float):
    oid = ins("INSERT INTO orders(user_id,product_id,qty,total,created,kind) VALUES(?,0,0,?,?,'topup')",
              (uid, round(amount, 2), int(time.time())))
    rows = []
    if ROLLY_ON:
        rows.append([("💳 Карта / СБП", f"pay:{oid}:rolly")])
    if CRYPTO_ON:
        rows.append([("🪙 Криптой (CryptoBot)", f"pay:{oid}:crypto")])
    rows.append([("⬅️ Назад", "topup")])
    await screen(ev, f"➕ <b>Пополнение #{oid}</b>\nСумма: <b>{amount:g}{CURRENCY}</b>\n\n"
                     f"Выбери способ оплаты:", kb(*rows))


@router.callback_query(F.data.startswith("tu:"))
async def cb_tu(c: CallbackQuery):
    if await guard(c):
        await make_topup(c, c.from_user.id, float(c.data[3:]))


@router.callback_query(F.data == "tu_c")
async def cb_tu_c(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    await state.set_state(Buy.topup)
    await screen(c, f"✏️ Введи сумму пополнения числом (от {MIN_TOPUP}{CURRENCY}):",
                 kb([("⬅️ Назад", "topup")]))


@router.message(Buy.topup, ~F.text.startswith("/"))
async def buy_topup(m: Message, state: FSMContext):
    uid = m.from_user.id
    u = q("SELECT verified, blocked FROM users WHERE id=?", (uid,), one=True)
    if not is_admin(uid) and not (u and u["verified"] and not u["blocked"]):
        await state.clear()
        await m.answer("Нажми /start")
        return
    try:
        amount = float((m.text or "").replace(",", "."))
    except ValueError:
        await m.answer("Нужно число, например 500")
        return
    if amount < MIN_TOPUP or amount > 100000:
        await m.answer(f"Сумма от {MIN_TOPUP} до 100000{CURRENCY}")
        return
    await state.clear()
    await make_topup(m, uid, amount)


@router.callback_query(F.data.startswith("qty:"))
async def cb_qty(c: CallbackQuery, bot: Bot):
    if not await guard(c):
        return
    _, pid, n = c.data.split(":")
    await make_order(c, bot, c.from_user.id, int(pid), int(n))


@router.callback_query(F.data.startswith("custom:"))
async def cb_custom(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    pid = int(c.data[7:])
    await state.set_state(Buy.qty)
    await state.update_data(pid=pid)
    await screen(c, f"✏️ Введи количество ключей числом (от 1 до {stock(pid)}):",
                 kb([("⬅️ Назад", f"prod:{pid}")]))


@router.message(Buy.qty, ~F.text.startswith("/"))
async def buy_qty(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    u = q("SELECT verified, blocked FROM users WHERE id=?", (uid,), one=True)
    if not is_admin(uid) and not (u and u["verified"] and not u["blocked"]):
        await state.clear()
        await m.answer("Нажми /start")
        return
    txt = (m.text or "").strip()
    if not txt.isdigit() or int(txt) < 1:
        await m.answer("Нужно целое число, например 7")
        return
    pid = (await state.get_data())["pid"]
    await state.clear()
    await make_order(m, bot, uid, pid, int(txt))


@router.callback_query(F.data.startswith("chk:"))
async def cb_chk(c: CallbackQuery, bot: Bot):
    if not await guard(c):
        return
    oid = int(c.data[4:])
    o = q("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, c.from_user.id), one=True)
    if not o:
        await c.answer("Заказ не найден", show_alert=True)
        return
    if o["status"] in ("done", "paid", "paid_nostock"):
        await c.answer("Заказ уже оплачен ✅", show_alert=True)
        return
    if o["status"] != "new":
        await c.answer("Заказ неактивен", show_alert=True)
        return
    try:
        st = await get_status(o)
    except Exception:
        await c.answer("Не удалось проверить, попробуй ещё раз", show_alert=True)
        return
    if st in OK_STATUSES:
        await fulfill(bot, oid)
        await c.answer("Оплата получена ✅")
    else:
        await c.answer("Оплата ещё не поступила ⏳", show_alert=True)


# ---------------------- АДМИН-ПАНЕЛЬ ----------------------
ADM = F.from_user.id.in_(ADMIN_IDS)
ADM_KB = kb(
    [("➕ Добавить ключи", "a_keys")],
    [("📢 Объявление", "a_bc")],
    [("💰 Изменить цену", "a_price")],
    [("🆕 Добавить товар", "a_prod")],
    [("🔑 Создать пароль юзеру", "a_user")],
    [("🔓 Разблокировать юзера", "a_unblock")],
    [("💵 Выдать баланс", "a_bal")],
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
async def keys_in(m: Message, state: FSMContext, bot: Bot):
    pid = (await state.get_data())["pid"]
    ks = [k.strip() for k in (m.text or "").splitlines() if k.strip()]
    for k in ks:
        q("INSERT INTO keys(product_id,key) VALUES(?,?)", (pid, k))
    await state.clear()
    for o in q("SELECT id FROM orders WHERE status='paid_nostock' AND product_id=?", (pid,)):
        await fulfill(bot, o["id"], "paid_nostock")
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
    q("""INSERT INTO users(id,pw,attempts,blocked,verified) VALUES(?,?,0,0,0)
         ON CONFLICT(id) DO UPDATE SET pw=excluded.pw, attempts=0, blocked=0, verified=0""",
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


# --- выдать баланс
@router.callback_query(F.data == "a_bal", ADM)
async def a_bal(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.bal_id)
    await screen(c, "💵 Введи Telegram ID пользователя:")


@router.message(A.bal_id, ADM)
async def bal_id_in(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt.isdigit():
        await m.answer("ID — только цифры.")
        return
    uid = int(txt)
    if not is_admin(uid) and not q("SELECT 1 FROM users WHERE id=?", (uid,), one=True):
        await m.answer("Такого пользователя нет. Сначала выдай ему доступ (пароль).")
        return
    await state.update_data(uid=uid)
    await state.set_state(A.bal_amt)
    await m.answer(f"Баланс сейчас: {get_balance(uid):g}{CURRENCY}\n"
                   f"Сколько начислить? Число, например 500. Минус — списать (-200).")


@router.message(A.bal_amt, ADM)
async def bal_amt_in(m: Message, state: FSMContext, bot: Bot):
    try:
        amount = float((m.text or "").replace(",", "."))
    except ValueError:
        await m.answer("Нужно число, например 500 или -200")
        return
    uid = (await state.get_data())["uid"]
    get_balance(uid)
    q("UPDATE users SET balance=MAX(balance+?,0) WHERE id=?", (amount, uid))
    new = get_balance(uid)
    await state.clear()
    try:
        if amount >= 0:
            await bot.send_message(uid, f"💵 Тебе начислено <b>{amount:g}{CURRENCY}</b>.\n"
                                        f"Баланс: <b>{new:g}{CURRENCY}</b>")
        else:
            await bot.send_message(uid, f"💵 С баланса списано <b>{-amount:g}{CURRENCY}</b>.\n"
                                        f"Баланс: <b>{new:g}{CURRENCY}</b>")
    except Exception:
        pass
    await m.answer(f"✅ Готово. Баланс <code>{uid}</code>: {new:g}{CURRENCY}", reply_markup=ADM_KB)


# --- кнопки из уведомлений
@router.callback_query(F.data.startswith("grant:"), ADM)
async def cb_grant(c: CallbackQuery, bot: Bot):
    uid = int(c.data[6:])
    pw = secrets.token_urlsafe(6)
    q("""INSERT INTO users(id,pw,attempts,blocked,verified) VALUES(?,?,0,0,0)
         ON CONFLICT(id) DO UPDATE SET pw=excluded.pw, attempts=0, blocked=0, verified=0""",
      (uid, hash_pw(pw)))
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await c.message.answer(f"✅ Доступ выдан <code>{uid}</code>\nПароль: <code>{pw}</code>\nПередай ему.")
    try:
        await bot.send_message(uid, "✅ Тебе выдан доступ. Админ пришлёт пароль — потом нажми /start.")
    except Exception:
        pass
    await c.answer()


@router.callback_query(F.data.startswith("unb:"), ADM)
async def cb_unb(c: CallbackQuery):
    q("UPDATE users SET blocked=0, attempts=0 WHERE id=?", (int(c.data[4:]),))
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await c.answer("Разблокирован ✅", show_alert=True)


# ---------------------- ЗАПУСК ----------------------
async def main():
    init_db()
    migrate()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    for o in q("SELECT id FROM orders WHERE status='new' AND payment_id IS NOT NULL AND created>?",
               (int(time.time()) - PAY_TIMEOUT,)):
        start_watch(bot, o["id"])
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
