import asyncio
import logging
import os
import sqlite3
import secrets
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    WebAppInfo,
    CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9237220562:AAHS9DEADCLpupc3CkkyeYXMl4F-J4Z0jLs")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://steps1337.github.io/mini-app-fragment/")
PROXY_URL = "http://r2nEE7:NA4P02@168.196.238.49:9316"

API_HOST = "0.0.0.0"
API_PORT = int(os.getenv("PORT", 8000)) # Render использует PORT, по дефолту 8000
DB_PATH = os.path.join(os.path.dirname(__file__), "deals.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fragment-bot")

# =========================================================
#                  НАСТРОЙКА БОТА И ПРОКСИ
# =========================================================
print(f"🔄 Инициализация сессии через прокси: {PROXY_URL}")
TIMEOUT_SECONDS = 60 

try:
    session = AiohttpSession(proxy=PROXY_URL, timeout=TIMEOUT_SECONDS)
    bot = Bot(token=BOT_TOKEN, session=session)
    print("✅ Бот успешно инициализирован с прокси.")
except Exception as e:
    print(f"❌ Критическая ошибка при создании сессии: {e}")
    raise e

dp = Dispatcher()

# =========================================================
#                  БАЗА ДАННЫХ (SQLite) С МИГРАЦИЕЙ
# =========================================================
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            name TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS deals (
            id           TEXT PRIMARY KEY,
            owner_id     INTEGER NOT NULL,
            owner_name   TEXT NOT NULL,
            partner_id   INTEGER,
            partner_name TEXT,
            role         TEXT NOT NULL,
            amount       REAL NOT NULL,
            description  TEXT,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL
        );
    """)
    
    # Миграция для старых баз данных (добавление столбца name)
    cursor = con.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'name' not in columns:
        print("🔧 Обнаружена старая БД. Добавляю столбец 'name'...")
        con.execute("ALTER TABLE users ADD COLUMN name TEXT DEFAULT ''")
        
    con.commit()
    con.close()

def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def ensure_user(user_id: int, name: str = ""):
    con = db_conn()
    con.execute("INSERT OR IGNORE INTO users (user_id, balance, name) VALUES (?, 0.0, ?)", (user_id, name))
    if name:
        con.execute("UPDATE users SET name = ? WHERE user_id = ?", (name, user_id))
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

# =========================================================
#                  FastAPI (backend)
# =========================================================
class CreateDealIn(BaseModel):
    owner_id: int
    owner_name: str
    role: str
    amount: float
    description: str = ""

class AcceptDealIn(BaseModel):
    partner_id: int
    partner_name: str

# Модель для новой логики создания сделки через "+" в Mini App
class NewDealRequest(BaseModel):
    buyer_id: int
    buyer_name: str
    seller_id: int
    amount: float
    description: str = ""

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    yield
    await bot.session.close()

api = FastAPI(lifespan=lifespan)
api.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

@api.get("/")
async def root():
    return {"status": "ok"}

@api.get("/user")
async def get_user(user_id: int):
    return {"user_id": user_id, "balance": get_balance(user_id)}

@api.get("/deals")
async def list_deals(user_id: int):
    ensure_user(user_id)
    con = db_conn()
    rows = con.execute(
        "SELECT * FROM deals WHERE owner_id = ? OR partner_id = ? ORDER BY created_at DESC",
        (user_id, user_id),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

# НОВЫЙ ЭНДПОИНТ ДЛЯ СОЗДАНИЯ СДЕЛКИ ЧЕРЕЗ MINI APP (+)
@api.post("/api/create-deal")
async def create_deal_from_app(req: NewDealRequest):
    """Создает сделку и отправляет уведомление продавцу"""
    ensure_user(req.buyer_id, req.buyer_name)
    ensure_user(req.seller_id, "Unknown")
    
    deal_id = secrets.token_hex(3).upper()
    now = datetime.utcnow().isoformat()
    
    con = db_conn()
    try:
        # Сохраняем сделку в БД (role='buyer' т.к. создает покупатель)
        con.execute(
            """INSERT INTO deals (id, owner_id, owner_name, partner_id, role, amount, description, status, created_at)
               VALUES (?, ?, ?, ?, 'buyer', ?, ?, 'pending', ?)""",
            (deal_id, req.buyer_id, req.buyer_name, req.seller_id, req.amount, req.description, now)
        )
        con.commit()
        
        # Отправляем уведомление ПРОДАВЦУ в Telegram
        kb = InlineKeyboardBuilder()
        kb.button(text=f"✅ Принять сделку {deal_id}", callback_data=f"accept_{deal_id}")
        kb.button(text="❌ Отклонить", callback_data=f"reject_{deal_id}")
        kb.adjust(2)
        
        await bot.send_message(
            req.seller_id,
            f"💰 *Новая сделка!*\n\n"
            f"Покупатель: {req.buyer_name}\n"
            f"Сумма: {req.amount} ₽\n"
            f"Описание: {req.description}\n"
            f"ID: `{deal_id}`\n\n"
            f"Нажмите кнопку ниже, чтобы принять.",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        con.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()
        
    return {"status": "ok", "deal_id": deal_id}

@api.post("/deal")
async def create_deal(payload: CreateDealIn):
    ensure_user(payload.owner_id)
    if payload.amount <= 0:
        raise HTTPException(400, "Сумма должна быть больше 0")

    if payload.role == 'buyer' and get_balance(payload.owner_id) < payload.amount:
        raise HTTPException(400, "Недостаточно средств на балансе")

    deal_id = secrets.token_hex(3).upper()
    con = db_conn()
    con.execute(
        """INSERT INTO deals (id, owner_id, owner_name, role, amount, description, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (deal_id, payload.owner_id, payload.owner_name, payload.role, payload.amount,
         payload.description, datetime.utcnow().isoformat()),
    )
    con.commit()
    con.close()

    try:
        await bot.send_message(payload.owner_id, f"✅ *Сделка создана*\nID: `{deal_id}`\nСумма: {payload.amount} ₽", parse_mode="Markdown")
    except Exception:
        pass

    return {"id": deal_id, "status": "pending"}

@api.post("/deal/{deal_id}/accept")
async def accept_deal(deal_id: str, payload: AcceptDealIn):
    ensure_user(payload.partner_id)
    con = db_conn()
    row = con.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "Сделка не найдена")
    if row["owner_id"] == payload.partner_id:
        con.close()
        raise HTTPException(400, "Нельзя принять свою сделку")
    if row["status"] != "pending":
        con.close()
        raise HTTPException(400, "Сделка уже принята")

    con.execute("UPDATE deals SET partner_id = ?, partner_name = ?, status = 'active' WHERE id = ?", (payload.partner_id, payload.partner_name, deal_id))
    con.commit()
    con.close()

    try:
        await bot.send_message(row["owner_id"], f"🤝 *Сделка {deal_id} принята!*", parse_mode="Markdown")
    except Exception:
        pass

    return {"id": deal_id, "status": "active"}

# =========================================================
#                        БОТ (ОБНОВЛЕННОЕ МЕНЮ)
# =========================================================

def get_main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    builder.button(text="🚀 Открыть Fragment", web_app=WebAppInfo(url=MINI_APP_URL))
    builder.button(text="💳 Кошелёк", callback_data="wallet")
    builder.button(text="👤 Профиль", callback_data="profile_info")
    builder.button(text="💼 Сделки", callback_data="my_deals")
    builder.button(text="❓ Как это работает", callback_data="how_it_works")
    builder.button(text="🌐 Сайт", url="https://fragment.com")
    
    builder.adjust(1, 2, 2, 1)
    return builder.as_markup()

HOW_IT_WORKS_TEXT = (
    "📘 *Как работает Fragment*\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "1. 🤝 *Продавец или покупатель создаёт сделку*\n"
    "2. 💳 *Покупатель платит* — деньги замораживаются у сервиса\n"
    "3. 📦 *Продавец передаёт товар* в чате сделки\n"
    "4. ✅ *Покупатель проверяет и подтверждает сделку*\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🔒 *Спор в любой момент* — подключится модератор.\n"
    "💰 *Комиссия сервиса* — 12,5%, платит покупатель сверху.\n"
    "⚠️ *Никогда не переводите деньги «напрямую» мимо сделки.*"
)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user.id, message.from_user.first_name)
    
    text = (
        f"💎 *FRAGMENT · безопасная биржа*\n\n"
        f"✨ Привет, {message.from_user.first_name}! Сделки — прямо в Telegram.\n\n"
        f"🛡 *Гарант* — деньги у сервиса, пока сделка не закрыта\n"
        f"💳 *Кошелёк* — пополнение и вывод в пару тапов\n"
        f"💬 *Сделки и чат* — во встроенном приложении\n"
        f"⚡ *Автовыплата* — сразу после подтверждения"
    )
    
    await message.answer(
        text,
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )

@dp.callback_query(F.data == "how_it_works")
async def cb_how_it_works(callback: CallbackQuery):
    await callback.message.answer(
        HOW_IT_WORKS_TEXT,
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "wallet")
async def cb_wallet(callback: CallbackQuery):
    bal = get_balance(callback.from_user.id)
    await callback.message.answer(
        f"💳 *Ваш кошелёк*\n\n"
        f"Текущий баланс: *{bal:,.2f} ₽*\n\n"
        f"Для пополнения или вывода средств откройте Mini App.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "profile_info")
async def cb_profile(callback: CallbackQuery):
    await callback.message.answer(
        f"👤 *Ваш профиль*\n\n"
        f"Имя: {callback.from_user.full_name}\n"
        f"ID: `{callback.from_user.id}`\n"
        f"Уровень: 1\n\n"
        f"Более подробная статистика доступна в Mini App.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "my_deals")
async def cb_my_deals(callback: CallbackQuery):
    await callback.message.answer(
        "💼 *Мои сделки*\n\n"
        "Все активные и завершенные сделки отображаются во встроенном приложении.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

# Обработчики кнопок принятия/отклонения сделки из уведомления
@dp.callback_query(F.data.startswith("accept_"))
async def cb_accept(callback: CallbackQuery):
    deal_id = callback.data.split("_")[1]
    con = db_conn()
    deal = con.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    
    if deal and deal["status"] == "pending":
        con.execute("UPDATE deals SET status = 'active', partner_id = ?, partner_name = ? WHERE id = ?", 
                    (callback.from_user.id, callback.from_user.first_name, deal_id))
        con.commit()
        await callback.answer("Сделка принята!")
        await callback.message.edit_text(f"✅ Сделка {deal_id} принята! Статус: В работе.")
        await bot.send_message(deal["owner_id"], f"🎉 Продавец принял сделку {deal_id}!")
    else:
        await callback.answer("Сделка уже обработана", show_alert=True)
    con.close()

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

@dp.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    ensure_user(message.from_user.id)
    if not command.args:
        return

    code = command.args.strip().upper()
    PROMO_CODES = {
        "START100": 100.0,
        "FRAGMENT500": 500.0,
        "STARS1000": 1000.0
    }

    if code in PROMO_CODES:
        amount = PROMO_CODES[code]
        add_balance(message.from_user.id, amount)
        PROMO_CODES.pop(code, None) 
        await message.answer(f"✅ *Код успешно активирован!*\n\nВам начислено: **{amount} ₽**", parse_mode="Markdown")
    else:
        await message.answer("❌ Неверный или уже использованный промокод.")

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message, successful_payment: SuccessfulPayment):
    await message.answer(f"✅ *Оплата прошла успешно!*\nСумма: {successful_payment.total_amount} {successful_payment.currency}", parse_mode="Markdown")

# =========================================================
#                     ЗАПУСК
# =========================================================
async def run_api():
    config = uvicorn.Config(api, host=API_HOST, port=API_PORT, log_level="info")
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