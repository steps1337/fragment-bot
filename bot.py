import asyncio
import logging
import os
import sqlite3
import secrets
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message, PreCheckoutQuery, SuccessfulPayment,
    WebAppInfo, CallbackQuery, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "9227078546:AAFH8rNb7MDy8EqIQkSv8qZ4UhmdMKzewh0"
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://steps1337.github.io/mini-app-fragment/")
TIMEOUT_SECONDS = 60
API_PORT = int(os.getenv("PORT", 8000))
DB_PATH = os.path.join(os.path.dirname(__file__), "deals.db")

# ВАШ TELEGRAM ID (ЕДИНСТВЕННЫЙ АДМИН)
ADMIN_ID = 7849089814

# --- ПРОКСИ ---
RAW_PROXY = os.getenv("PROXY_URL", "168.196.238.49:9316:r2nEE7:NA4P02")

def format_proxy(proxy_str):
    if not proxy_str:
        return None
    if "://" in proxy_str:
        return proxy_str
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, login, password = parts
        return f"http://{login}:{password}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{proxy_str}"
    return proxy_str

PROXY_URL = format_proxy(RAW_PROXY)

# Курсы валют к рублю
RATES_TO_RUB = {
    "RUB": 1.0,
    "UAH": 2.5,
    "USDT": 90.0,
    "GRAM": 95.0,
    "STARS": 1.5
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fragment-bot")

# =========================================================
#                  НАСТРОЙКА БОТА И СЕССИИ
# =========================================================
try:
    if PROXY_URL:
        print(f"🔄 Инициализация сессии через прокси: {PROXY_URL}")
        session = AiohttpSession(proxy=PROXY_URL, timeout=TIMEOUT_SECONDS)
    else:
        print("✅ Инициализация сессии без прокси (прямое соединение).")
        session = AiohttpSession(timeout=TIMEOUT_SECONDS)

    bot = Bot(token=BOT_TOKEN, session=session)
    print("✅ Бот успешно инициализирован.")
except Exception as e:
    print(f"❌ Критическая ошибка при создании сессии: {e}")
    raise e

dp = Dispatcher()

# =========================================================
#                  БАЗА ДАННЫХ (SQLite)
# =========================================================
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            is_banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS deals (
            id           TEXT PRIMARY KEY,
            owner_id     INTEGER NOT NULL,
            owner_name   TEXT NOT NULL,
            partner_id   INTEGER,
            partner_name TEXT,
            buyer_id     INTEGER NOT NULL,
            seller_id    INTEGER NOT NULL,
            role         TEXT NOT NULL,
            amount       REAL NOT NULL,
            currency     TEXT NOT NULL DEFAULT 'RUB',
            description  TEXT,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            amount       REAL NOT NULL,
            method       TEXT NOT NULL,
            requisites   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL
        );
    """)

    cursor = con.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'is_banned' not in columns:
        print("🔧 Миграция БД: добавляю столбец 'is_banned'...")
        con.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")

    con.commit()
    con.close()

def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def ensure_user(user_id: int, name: str = "", username: str = ""):
    con = db_conn()
    con.execute(
        "INSERT OR IGNORE INTO users (user_id, balance, name, username, is_banned) VALUES (?, 0.0, ?, ?, 0)",
        (user_id, name, username)
    )
    if name:
        con.execute("UPDATE users SET name = ? WHERE user_id = ?", (name, user_id))
    if username:
        con.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
    con.commit()
    con.close()

def get_balance(user_id: int) -> float:
    ensure_user(user_id)
    con = db_conn()
    row = con.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
    con.close()
    return row["balance"] if row else 0.0

def add_balance(user_id: int, amount: float):
    ensure_user(user_id)
    con = db_conn()
    con.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    con.commit()
    con.close()

def freeze_balance(user_id: int, amount: float) -> bool:
    con = db_conn()
    row = con.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row or row["balance"] < amount:
        con.close()
        return False
    con.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    con.commit()
    con.close()
    return True

def unfreeze_balance(user_id: int, amount: float):
    ensure_user(user_id)
    con = db_conn()
    con.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    con.commit()
    con.close()

# =========================================================
#                  FASTAPI МОДЕЛИ И ЭНДПОИНТЫ
# =========================================================
class NewDealRequest(BaseModel):
    creator_id: int
    creator_name: str
    partner_id: int
    amount: float
    description: str = ""
    role: str 
    currency: str = "RUB"

class DealActionIn(BaseModel):
    user_id: int
    action: str

class WithdrawRequest(BaseModel):
    user_id: int
    amount: float
    method: str
    requisites: str

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    yield
    await bot.session.close()

api = FastAPI(lifespan=lifespan)
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@api.get("/resolve-username")
async def resolve_username(username: str):
    clean_username = username.strip().lower()
    if clean_username.startswith('@'):
        clean_username = clean_username[1:]

    con = db_conn()
    row = con.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (clean_username,)).fetchone()
    con.close()

    if row:
        return {"user_id": row["user_id"]}
    else:
        raise HTTPException(status_code=404, detail=f"Пользователь @{clean_username} не найден. Он должен сначала запустить бота.")

@api.get("/user")
async def get_user(user_id: int):
    con = db_conn()
    row = con.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    con.close()
    
    if row and row["is_banned"]:
        raise HTTPException(status_code=403, detail="Вы забанены в сервисе Fragment")
        
    return {"user_id": user_id, "balance": get_balance(user_id)}

@api.get("/deals")
async def list_deals(user_id: int):
    ensure_user(user_id)
    con = db_conn()
    rows = con.execute(
        "SELECT * FROM deals WHERE buyer_id = ? OR seller_id = ? ORDER BY created_at DESC",
        (user_id, user_id),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

@api.get("/withdrawals")
async def list_withdrawals(user_id: int):
    ensure_user(user_id)
    con = db_conn()
    rows = con.execute(
        "SELECT * FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

@api.post("/api/create-deal")
async def create_deal_from_app(req: NewDealRequest):
    ensure_user(req.creator_id, req.creator_name)
    
    con = db_conn()
    partner_row = con.execute("SELECT name, username FROM users WHERE user_id = ?", (req.partner_id,)).fetchone()
    partner_name = partner_row["name"] if partner_row else "Unknown"
    partner_username = partner_row["username"] if partner_row else ""
    con.close()
    
    ensure_user(req.partner_id, partner_name)
    
    if req.role == 'buyer':
        buyer_id, buyer_name = req.creator_id, req.creator_name
        seller_id, seller_name = req.partner_id, partner_name
    else:
        seller_id, seller_name = req.creator_id, req.creator_name
        buyer_id, buyer_name = req.partner_id, partner_name

    amount_in_rub = req.amount * RATES_TO_RUB.get(req.currency, 1.0)
    buyer_balance = get_balance(buyer_id)
    if buyer_balance < amount_in_rub:
        raise HTTPException(
            400, 
            f"У покупателя недостаточно средств для этой сделки. "
            f"Нужно: {amount_in_rub:.2f} ₽, у него: {buyer_balance:.2f} ₽"
        )

    deal_id = secrets.token_hex(3).upper()
    now = datetime.utcnow().isoformat()

    con = db_conn()
    try:
        con.execute(
            """INSERT INTO deals (id, owner_id, owner_name, partner_id, partner_name, buyer_id, seller_id, role, amount, currency, description, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'buyer', ?, ?, ?, 'pending', ?)""",
            (deal_id, req.creator_id, req.creator_name, req.partner_id, partner_name, buyer_id, seller_id, req.amount, req.currency, req.description, now)
        )
        con.commit()
        
        kb = InlineKeyboardBuilder()
        kb.button(text=f"✅ Принять ({int(req.amount)} {req.currency})", callback_data=f"accept_{deal_id}")
        kb.button(text="❌ Отклонить", callback_data=f"reject_{deal_id}")
        kb.adjust(2)
        
        role_text = "покупатель" if req.role == 'buyer' else "продавец"
        await bot.send_message(
            req.partner_id,
            f"💰 *Новая сделка!*\n\n"
            f"Создатель: {req.creator_name} ({role_text})\n"
            f"Сумма: {req.amount} {req.currency}\n"
            f"Описание: {req.description}\n"
            f"ID: `{deal_id}`\n\n"
            f"⚠️ Для принятия сделки у покупателя должно быть достаточно средств (гарант).\n"
            f"Нажмите кнопку ниже, чтобы принять.",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        
        con = db_conn()
        creator_row = con.execute("SELECT username, name FROM users WHERE user_id = ?", (req.creator_id,)).fetchone()
        con.close()
        
        creator_username = creator_row["username"] if creator_row else ""
        creator_tag = f"@{creator_username}" if creator_username else req.creator_name
        
        partner_tag = f"@{partner_username}" if partner_username else partner_name
        
        if req.role == 'buyer':
            buyer_log = f"{creator_tag} (ID: {req.creator_id})"
            seller_log = f"{partner_tag} (ID: {req.partner_id})"
        else:
            buyer_log = f"{partner_tag} (ID: {req.partner_id})"
            seller_log = f"{creator_tag} (ID: {req.creator_id})"

        await bot.send_message(
            ADMIN_ID,
            f"🔔 *Новая сделка в системе!*\n\n"
            f"📦 Товар: {req.description}\n"
            f"💰 Сумма: {req.amount} {req.currency}\n"
            f"🛒 Покупатель: {buyer_log}\n"
            f"📤 Продавец: {seller_log}\n"
            f"🆔 ID сделки: `{deal_id}`",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        con.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()
        
    return {"status": "ok", "deal_id": deal_id}

@api.post("/api/withdraw")
async def create_withdrawal(req: WithdrawRequest):
    if req.amount < 500:
        raise HTTPException(400, "Минимальная сумма вывода — 500 ₽")
    if req.method not in ("sbp", "card", "crypto"):
        raise HTTPException(400, "Неизвестный способ вывода")
    if not req.requisites.strip():
        raise HTTPException(400, "Не указаны реквизиты")

    con = db_conn()
    row = con.execute("SELECT balance FROM users WHERE user_id = ?", (req.user_id,)).fetchone()
    if not row or row["balance"] < req.amount:
        con.close()
        raise HTTPException(400, "Недостаточно средств")

    now = datetime.utcnow().isoformat()
    con.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (req.amount, req.user_id))
    con.execute(
        "INSERT INTO withdrawals (user_id, amount, method, requisites, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (req.user_id, req.amount, req.method, req.requisites, now)
    )
    con.commit()
    wid = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    con.close()

    method_labels = {"sbp": "СБП", "card": "Карта", "crypto": "Крипто"}
    if ADMIN_ID:
        try:
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Подтвердить", callback_data=f"wdone_{wid}")
            kb.button(text="❌ Отклонить", callback_data=f"wreject_{wid}")
            kb.adjust(2)
            await bot.send_message(
                ADMIN_ID,
                f"💸 *Новая заявка на вывод #{wid}*\n\n"
                f"Пользователь: `{req.user_id}`\n"
                f"Сумма: *{req.amount} ₽*\n"
                f"Способ: {method_labels[req.method]}\n"
                f"Реквизиты: `{req.requisites}`\n\n"
                f"Подтвердите или отклоните заявку.",
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
        except Exception as e:
            log.warning(f"Не удалось отправить уведомление админу: {e}")

    return {"status": "ok", "withdrawal_id": wid}

@api.post("/deal/{deal_id}/action")
async def handle_deal_action(deal_id: str, payload: DealActionIn):
    con = db_conn()
    deal = con.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()

    if not deal:
        con.close()
        raise HTTPException(404, "Сделка не найдена")

    is_buyer = (deal["buyer_id"] == payload.user_id)
    is_seller = (deal["seller_id"] == payload.user_id)

    if not is_buyer and not is_seller:
        con.close()
        raise HTTPException(403, "У вас нет доступа к этой сделке")

    new_status = deal["status"]
    notify_text = ""

    if payload.action == 'delivered':
        if not is_seller:
            con.close()
            raise HTTPException(400, "Только продавец может подтвердить передачу")
        if deal["status"] != 'active':
            con.close()
            raise HTTPException(400, "Невозможно выполнить действие (статус не active)")

        new_status = 'delivered'
        notify_text = f"📦 Продавец подтвердил передачу товара по сделке #{deal_id}!\nПроверьте товар и нажмите 'Я получил' в приложении."

    elif payload.action == 'received':
        if not is_buyer:
            con.close()
            raise HTTPException(400, "Только покупатель может подтвердить получение")
        if deal["status"] not in ['active', 'delivered']:
            con.close()
            raise HTTPException(400, "Сделка не в том статусе для подтверждения")

        new_status = 'completed'
        
        amount_in_rub = deal['amount'] * RATES_TO_RUB.get(deal['currency'], 1.0)
        add_balance(deal['seller_id'], amount_in_rub)
        
        notify_text = f"✅ Сделка #{deal_id} завершена!\nПокупатель подтвердил получение. Средства переведены продавцу."

    else:
        con.close()
        raise HTTPException(400, "Неизвестное действие")

    con.execute("UPDATE deals SET status = ? WHERE id = ?", (new_status, deal_id))
    con.commit()
    con.close()

    target_id = deal["seller_id"] if is_buyer else deal["buyer_id"]
    try:
        await bot.send_message(target_id, notify_text)
    except:
        pass

    return {"status": "ok", "new_status": new_status}

# =========================================================
#                  AIOGRAM HANDLERS (БОТ)
# =========================================================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    username = message.from_user.username or ""
    ensure_user(message.from_user.id, message.from_user.first_name, username)
    
    con = db_conn()
    row = con.execute("SELECT is_banned FROM users WHERE user_id = ?", (message.from_user.id,)).fetchone()
    con.close()
    if row and row["is_banned"]:
        await message.answer("⛔ Вы забанены в сервисе Fragment.")
        return

    IMAGE_PATH = "start_image.jpg"

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Открыть Fragment", web_app=WebAppInfo(url=MINI_APP_URL))
    kb.button(text="💳 Кошелёк", callback_data="menu_wallet")
    kb.button(text="👤 Профиль", callback_data="menu_profile")
    kb.button(text="💼 Сделки", callback_data="menu_deals")
    kb.button(text="❓ Как это работает", callback_data="menu_how")
    kb.button(text="🌐 Сайт", url="https://fragment.com") 
    kb.adjust(1, 2, 2, 1)

    text = (
        f"💎 *FRAGMENT · безопасная биржа*\n\n"
        f"✨ Привет, {message.from_user.first_name}! Сделки — прямо в Telegram.\n\n"
        f"🛡 *Гарант* — деньги у сервиса, пока сделка не закрыта\n"
        f"💳 *Кошелёк* — пополнение и вывод в пару тапов\n"
        f"💬 *Сделки и чат* — во встроенном приложении\n"
        f"⚡ *Автовыплата* — сразу после подтверждения"
    )

    try:
        photo = FSInputFile(IMAGE_PATH)
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=photo,
            caption=text,
            reply_markup=kb.as_markup(),
            parse_mode="Markdown"
        )
    except FileNotFoundError:
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("accept_"))
async def cb_accept(callback: CallbackQuery):
    deal_id = callback.data.split("_")[1]
    con = db_conn()
    deal = con.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()

    if not deal:
        await callback.answer("Сделка не найдена", show_alert=True)
        con.close()
        return

    if deal["status"] != "pending":
        await callback.answer("Сделка уже обработана", show_alert=True)
        con.close()
        return

    user_id = callback.from_user.id
    amount = deal["amount"]
    currency = deal["currency"]
    buyer_id = deal["buyer_id"]
    seller_id = deal["seller_id"]
    owner_id = deal["owner_id"]
    
    amount_in_rub = amount * RATES_TO_RUB.get(currency, 1.0)

    if user_id == owner_id:
        await callback.answer("Вы создатель этой сделки. Ждите, пока вторая сторона её примет.", show_alert=True)
        con.close()
        return

    if not freeze_balance(buyer_id, amount_in_rub):
        bal = get_balance(buyer_id)
        await callback.answer(f"❌ У покупателя недостаточно средств!\nНужно: {amount} {currency} ({amount_in_rub:.2f} ₽)\nУ него: {bal:.2f} ₽", show_alert=True)
        con.close()
        return

    con.execute("UPDATE deals SET status = 'active' WHERE id = ?", (deal_id,))
    con.commit()
    con.close()

    await callback.answer("Сделка принята! Средства покупателя заморожены.")
    await callback.message.edit_text(f"✅ Сделка {deal_id} принята!\nСтатус: В работе.\nГарант: {amount} {currency} заморожен у покупателя.")
    
    if user_id == buyer_id:
        await callback.message.answer(f"Вы приняли сделку! Ваши средства заморожены. Ожидайте передачу товара от продавца.")
        await bot.send_message(seller_id, f"🎉 Покупатель принял вашу сделку {deal_id}!\nЕго средства заморожены. Можете передавать товар.")
        
    elif user_id == seller_id:
        await callback.message.answer(f"Вы приняли сделку! Передайте товар покупателю и нажмите кнопку «Я передал товар» в приложении.")
        await bot.send_message(buyer_id, f"🎉 Продавец принял вашу сделку {deal_id}!\nВаши средства заморожены. Ожидайте передачу товара от продавца.")

@dp.callback_query(F.data.startswith("reject_"))
async def cb_reject(callback: CallbackQuery):
    deal_id = callback.data.split("_")[1]
    con = db_conn()
    deal = con.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()

    if deal and deal["status"] == "pending":
        con.execute("UPDATE deals SET status = 'cancelled' WHERE id = ?", (deal_id,))
        con.commit()
        await callback.answer("Сделка отклонена")
        await callback.message.edit_text(f"❌ Сделка {deal_id} отклонена.")
    con.close()

@dp.callback_query(F.data.startswith("wdone_"))
async def cb_withdraw_done(callback: CallbackQuery):
    wid = int(callback.data.split("_")[1])
    con = db_conn()
    row = con.execute("SELECT * FROM withdrawals WHERE id = ?", (wid,)).fetchone()
    if not row or row["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        con.close()
        return
    con.execute("UPDATE withdrawals SET status = 'done' WHERE id = ?", (wid,))
    con.commit()
    con.close()

    await callback.answer("Выплата подтверждена")
    await callback.message.edit_text(callback.message.text + "\n\n✅ *Выплачено*", parse_mode="Markdown")
    try:
        await bot.send_message(row["user_id"], f"✅ Ваша заявка на вывод #{wid} на сумму {row['amount']} ₽ выполнена!")
    except:
        pass

@dp.callback_query(F.data.startswith("wreject_"))
async def cb_withdraw_reject(callback: CallbackQuery):
    wid = int(callback.data.split("_")[1])
    con = db_conn()
    row = con.execute("SELECT * FROM withdrawals WHERE id = ?", (wid,)).fetchone()
    if not row or row["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        con.close()
        return
    con.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (row["amount"], row["user_id"]))
    con.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (wid,))
    con.commit()
    con.close()

    await callback.answer("Заявка отклонена, средства возвращены")
    await callback.message.edit_text(callback.message.text + "\n\n❌ *Отклонено (средства возвращены)*", parse_mode="Markdown")
    try:
        await bot.send_message(row["user_id"], f"❌ Ваша заявка на вывод #{wid} отклонена. Средства возвращены на баланс.")
    except:
        pass

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
        
    text = (
        "👑 *Админ-панель Fragment*\n\n"
        "Доступные команды:\n"
        "`/stats` — сводная статистика сервиса\n"
        "`/users` — список всех пользователей (✅/❌)\n"
        "`/bal <ID>` — баланс и статус пользователя\n"
        "`/deals <@username|ID>` — все сделки пользователя\n"
        "`/addbal <ID> <сумма> <валюта>` — выдать баланс\n"
        "`/backbal <ID> <сумма> <валюта>` — списать баланс\n"
        "`/ban <ID>` — забанить пользователя\n"
        "`/unban <ID>` — разбанить пользователя\n\n"
        "*Примеры:*\n"
        "`/bal 123456789`\n"
        "`/deals @danya_102b`\n"
        "`/addbal 123456789 1000 rub`\n"
        "`/ban 123456789`"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("addbal"))
async def cmd_addbal(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
        
    if not command.args:
        await message.answer("Использование: `/addbal <ID> <сумма> <валюта>`\nВалюты: rub, uah, usdt, gram, star", parse_mode="Markdown")
        return

    parts = command.args.strip().split()
    if len(parts) != 3:
        await message.answer("Неверный формат. Пример: `/addbal 123456789 1000 rub`", parse_mode="Markdown")
        return

    try:
        target_id = int(parts[0])
        amount = float(parts[1].replace(",", "."))
        currency = parts[2].upper()
    except ValueError:
        await message.answer("❌ Неверные данные (ID или сумма).")
        return

    if currency not in RATES_TO_RUB:
        await message.answer(f"❌ Неизвестная валюта. Доступны: {', '.join(RATES_TO_RUB.keys())}")
        return

    rub_amount = amount * RATES_TO_RUB[currency]
    add_balance(target_id, rub_amount)
    new_bal = get_balance(target_id)
    
    await message.answer(
        f"✅ Начислено: *{amount} {currency}* (эквивалент {rub_amount:.2f} ₽).\n"
        f"Пользователю `{target_id}`.\n"
        f"Его текущий баланс: *{new_bal:.2f} ₽*", 
        parse_mode="Markdown"
    )
    
    try:
        await bot.send_message(target_id, f"💰 Ваш баланс пополнен на {amount} {currency} ({rub_amount:.2f} ₽)!")
    except:
        pass

@dp.message(Command("backbal"))
async def cmd_backbal(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
        
    if not command.args:
        await message.answer("Использование: `/backbal <ID> <сумма> <валюта>`\nВалюты: rub, uah, usdt, gram, star", parse_mode="Markdown")
        return

    parts = command.args.strip().split()
    if len(parts) != 3:
        await message.answer("Неверный формат. Пример: `/backbal 123456789 500 rub`", parse_mode="Markdown")
        return

    try:
        target_id = int(parts[0])
        amount = float(parts[1].replace(",", "."))
        currency = parts[2].upper()
    except ValueError:
        await message.answer("❌ Неверные данные (ID или сумма).")
        return

    if currency not in RATES_TO_RUB:
        await message.answer(f"❌ Неизвестная валюта. Доступны: {', '.join(RATES_TO_RUB.keys())}")
        return

    rub_amount = amount * RATES_TO_RUB[currency]
    
    current_bal = get_balance(target_id)
    if current_bal < rub_amount:
        await message.answer(f"❌ У пользователя недостаточно средств для списания!\nНужно: {rub_amount:.2f} ₽\nУ него: {current_bal:.2f} ₽")
        return

    add_balance(target_id, -rub_amount)
    new_bal = get_balance(target_id)
    
    await message.answer(
        f"✅ Списано: *{amount} {currency}* (эквивалент {rub_amount:.2f} ₽).\n"
        f"У пользователя `{target_id}`.\n"
        f"Его текущий баланс: *{new_bal:.2f} ₽*", 
        parse_mode="Markdown"
    )
    
    try:
        await bot.send_message(target_id, f"⚠️ С вашего баланса списано {amount} {currency} ({rub_amount:.2f} ₽).")
    except:
        pass

@dp.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
        
    if not command.args:
        await message.answer("Использование: `/ban <ID>`", parse_mode="Markdown")
        return
        
    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Неверный ID.")
        return
        
    con = db_conn()
    con.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,))
    con.commit()
    con.close()
    
    await message.answer(f"⛔ Пользователь `{target_id}` забанен.", parse_mode="Markdown")
    try:
        await bot.send_message(target_id, "⛔ Вы забанены в сервисе Fragment.")
    except:
        pass

@dp.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
        
    if not command.args:
        await message.answer("Использование: `/unban <ID>`", parse_mode="Markdown")
        return
        
    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Неверный ID.")
        return
        
    con = db_conn()
    con.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,))
    con.commit()
    con.close()
    
    await message.answer(f"✅ Пользователь `{target_id}` разбанен.", parse_mode="Markdown")
    try:
        await bot.send_message(target_id, "✅ Вы были разбанены в сервисе Fragment. Доступ восстановлен.")
    except:
        pass

@dp.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    con = db_conn()
    rows = con.execute(
        "SELECT user_id, name, username, balance, is_banned FROM users ORDER BY user_id"
    ).fetchall()
    con.close()

    if not rows:
        await message.answer("📭 В базе пока нет пользователей.")
        return

    lines = []
    for row in rows:
        username = row["username"]
        if username:
            tag = f"@{username}"
        elif row["name"]:
            tag = f"{row['name']} (ID: {row['user_id']})"
        else:
            tag = f"ID: {row['user_id']}"

        balance = row["balance"] or 0.0
        ban_icon = "❌" if row["is_banned"] else "✅"

        lines.append(f"{tag} — {balance:.2f} ₽ — {ban_icon}")

    header = f"👥 *Пользователи ({len(rows)})*\n\n"

    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await message.answer(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += line + "\n"

    if chunk.strip():
        await message.answer(chunk, parse_mode="Markdown")

@dp.message(Command("bal"))
async def cmd_bal(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return

    if not command.args:
        await message.answer(
            "Использование: `/bal <ID>`\nПример: `/bal 123456789`",
            parse_mode="Markdown"
        )
        return

    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Неверный ID. Укажите числовой Telegram ID.")
        return

    con = db_conn()
    row = con.execute(
        "SELECT name, username, balance, is_banned FROM users WHERE user_id = ?",
        (target_id,)
    ).fetchone()
    con.close()

    if not row:
        await message.answer(f"❌ Пользователь с ID `{target_id}` не найден в базе.", parse_mode="Markdown")
        return

    username = row["username"]
    name = row["name"] or "—"
    balance = row["balance"] or 0.0
    ban_status = "❌ забанен" if row["is_banned"] else "✅ активен"

    tag = f"@{username}" if username else "—"

    text = (
        f"👤 *Пользователь*\n\n"
        f"🆔 ID: `{target_id}`\n"
        f"📛 Имя: {name}\n"
        f"🔗 Username: {tag}\n"
        f"💰 Баланс: *{balance:.2f} ₽*\n"
        f"🚦 Статус: {ban_status}"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    con = db_conn()

    users_total = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    users_banned = con.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned = 1").fetchone()["c"]
    total_balance = con.execute("SELECT COALESCE(SUM(balance), 0) AS s FROM users").fetchone()["s"]

    deals_total = con.execute("SELECT COUNT(*) AS c FROM deals").fetchone()["c"]
    deals_pending = con.execute("SELECT COUNT(*) AS c FROM deals WHERE status = 'pending'").fetchone()["c"]
    deals_active = con.execute("SELECT COUNT(*) AS c FROM deals WHERE status = 'active'").fetchone()["c"]
    deals_delivered = con.execute("SELECT COUNT(*) AS c FROM deals WHERE status = 'delivered'").fetchone()["c"]
    deals_completed = con.execute("SELECT COUNT(*) AS c FROM deals WHERE status = 'completed'").fetchone()["c"]
    deals_cancelled = con.execute("SELECT COUNT(*) AS c FROM deals WHERE status = 'cancelled'").fetchone()["c"]

    # Оборот считаем по завершённым сделкам, пересчитывая каждую в рубли
    completed_rows = con.execute("SELECT amount, currency FROM deals WHERE status = 'completed'").fetchall()
    turnover_rub = 0.0
    for r in completed_rows:
        turnover_rub += (r["amount"] or 0.0) * RATES_TO_RUB.get(r["currency"] or "RUB", 1.0)

    con.close()

    text = (
        f"📊 *Статистика Fragment*\n\n"
        f"👥 Пользователей: {users_total} (забанено: {users_banned})\n"
        f"💼 Сделок: {deals_total} (активных: {deals_active}, завершено: {deals_completed}, отменено: {deals_cancelled})\n"
        f"💰 Оборот: {turnover_rub:,.2f} ₽\n"
        f"💸 На балансах: {total_balance:,.2f} ₽\n\n"
        f"⏳ Ожидают принятия: {deals_pending}\n"
        f"📦 Товар передан (ждём подтверждения): {deals_delivered}"
    ).replace(",", " ")

    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("deals"))
async def cmd_deals(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return

    if not command.args:
        await message.answer(
            "Использование: `/deals <@username|ID>`\n"
            "Примеры:\n"
            "`/deals @danya_102b`\n"
            "`/deals 123456789`",
            parse_mode="Markdown"
        )
        return

    raw = command.args.strip().lstrip("@")

    con = db_conn()

    # Ищем пользователя: сначала по ID (если число), иначе по username
    target_row = None
    if raw.isdigit():
        target_row = con.execute(
            "SELECT user_id, name, username FROM users WHERE user_id = ?",
            (int(raw),)
        ).fetchone()
    if not target_row:
        target_row = con.execute(
            "SELECT user_id, name, username FROM users WHERE LOWER(username) = ?",
            (raw.lower(),)
        ).fetchone()

    if not target_row:
        con.close()
        await message.answer(f"❌ Пользователь `{raw}` не найден в базе.", parse_mode="Markdown")
        return

    target_id = target_row["user_id"]
    target_username = target_row["username"] or ""
    target_name = target_row["name"] or "—"
    tag = f"@{target_username}" if target_username else f"ID {target_id}"

    rows = con.execute(
        "SELECT * FROM deals WHERE buyer_id = ? OR seller_id = ? ORDER BY created_at DESC LIMIT 30",
        (target_id, target_id)
    ).fetchall()
    con.close()

    if not rows:
        await message.answer(f"📭 У пользователя {tag} пока нет сделок.", parse_mode="Markdown")
        return

    status_labels = {
        "pending": "⏳ ожидание",
        "active": "🔄 в работе",
        "delivered": "📦 передан",
        "completed": "✅ завершена",
        "cancelled": "❌ отменена",
    }

    header = (
        f"💼 *Сделки пользователя* {tag}\n"
        f"📛 Имя: {target_name} · ID: `{target_id}`\n"
        f"Всего показано: {len(rows)}\n\n"
    )

    lines = []
    for r in rows:
        curr = r["currency"] or "RUB"
        amount = r["amount"] or 0.0
        status = status_labels.get(r["status"], r["status"])
        created = (r["created_at"] or "")[:10]
        role = "🛒 покупатель" if r["buyer_id"] == target_id else "📤 продавец"

        # Контрагент
        if r["buyer_id"] == target_id:
            cp_id = r["seller_id"]
        else:
            cp_id = r["buyer_id"]

        # Описание: первая строка без [CURRENCY]
        desc_raw = (r["description"] or "").replace("\n", " ")
        desc_raw = desc_raw.split("[", 1)[-1]
        if "]" in desc_raw:
            desc_raw = desc_raw.split("]", 1)[1].strip()
        desc_short = desc_raw[:40] + ("…" if len(desc_raw) > 40 else "")

        lines.append(
            f"`{r['id']}` · {status} · {amount:g} {curr} · {role}\n"
            f"   👤 контрагент: `{cp_id}` · 📅 {created}\n"
            f"   📝 {desc_short or '—'}"
        )

    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 2 > 4000:
            await message.answer(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += line + "\n\n"

    if chunk.strip():
        await message.answer(chunk, parse_mode="Markdown")

# --- ОБРАБОТЧИКИ КНОПОК МЕНЮ ---

@dp.callback_query(F.data == "menu_how")
async def cb_menu_how(callback: CallbackQuery):
    text = (
        "📘 *Как работает Fragment*\n"
        "――――――――――――――――――――――――――――\n"
        "1. 🤝 Продавец или покупатель создаёт сделку\n"
        "2. 💳 Покупатель платит — деньги замораживаются у сервиса\n"
        "3. 📦 Продавец передаёт товар в чате сделки\n"
        "4. ✅ Покупатель проверяет и подтверждает сделку\n"
        "――――――――――――――――――――――――――――\n"
        "🔒 Спор в любой момент — подключится модератор.\n"
        "💰 Комиссия сервиса — 12,5%, платит покупатель сверху.\n"
        "⚠️ Никогда не переводите деньги «напрямую» мимо сделки."
    )
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_wallet")
async def cb_menu_wallet(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Открыть кошелёк", web_app=WebAppInfo(url=MINI_APP_URL))
    await callback.message.answer("💳 *Ваш кошелёк*\n\nПополнение, вывод и баланс доступны в мини-приложении.", parse_mode="Markdown", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    balance = get_balance(user_id)
    text = (
        f"👤 *Ваш профиль*\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"💰 Баланс: *{balance:.2f} ₽*\n"
    )
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_deals")
async def cb_menu_deals(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Мои сделки", web_app=WebAppInfo(url=MINI_APP_URL))
    await callback.message.answer("💼 *Ваши сделки*\n\nВсе активные и завершенные сделки доступны в мини-приложении.", parse_mode="Markdown", reply_markup=kb.as_markup())
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message, successful_payment: SuccessfulPayment):
    await message.answer(f"✅ *Оплата прошла!* Сумма: {successful_payment.total_amount} {successful_payment.currency}", parse_mode="Markdown")

# =========================================================
#                     ЗАПУСК
# =========================================================
async def run_api():
    config = uvicorn.Config(api, host="0.0.0.0", port=API_PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    try:
        print("🚀 Запуск бота...")
        await bot.delete_webhook(drop_pending_updates=True)
        print("✅ Вебхук удален, запускаем polling...")
        await asyncio.gather(run_api(), dp.start_polling(bot))
    except Exception as e:
        print(f"💥 Ошибка запуска: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())