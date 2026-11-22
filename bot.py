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
    WebAppInfo, CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://steps1337.github.io/mini-app-fragment/")
PROXY_URL = os.getenv("PROXY_URL") # Опционально
TIMEOUT_SECONDS = 60
API_PORT = int(os.getenv("PORT", 8000))
DB_PATH = os.path.join(os.path.dirname(__file__), "deals.db")

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
#                  БАЗА ДАННЫХ (SQLite) С МИГРАЦИЕЙ
# =========================================================
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            name TEXT DEFAULT '',
            username TEXT DEFAULT ''
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
    
    # Миграция: добавляем столбец username, если его нет
    cursor = con.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'username' not in columns:
        print("🔧 Миграция БД: добавляю столбец 'username'...")
        con.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''")
        
    con.commit()
    con.close()

def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def ensure_user(user_id: int, name: str = "", username: str = ""):
    con = db_conn()
    con.execute(
        "INSERT OR IGNORE INTO users (user_id, balance, name, username) VALUES (?, 0.0, ?, ?)", 
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

# =========================================================
#                  FASTAPI МОДЕЛИ И ЭНДПОИНТЫ
# =========================================================
class NewDealRequest(BaseModel):
    buyer_id: int
    buyer_name: str
    seller_id: int
    amount: float
    description: str = ""

class DealActionIn(BaseModel):
    user_id: int
    action: str  # 'delivered' или 'received'

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    yield
    await bot.session.close()

api = FastAPI(lifespan=lifespan)
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Поиск ID пользователя по username
@api.get("/resolve-username")
async def resolve_username(username: str):
    clean_username = username.strip().lower()
    if clean_username.startswith('@'):
        clean_username = clean_username[1:]
        
    con = db_conn()
    # Ищем в нашей базе пользователей, которые уже запускали бота
    row = con.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (clean_username,)).fetchone()
    con.close()
    
    if row:
        return {"user_id": row["user_id"]}
    else:
        raise HTTPException(status_code=404, detail=f"Пользователь @{clean_username} не найден. Он должен сначала запустить бота.")

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

# Создание сделки через Mini App (+)
@api.post("/api/create-deal")
async def create_deal_from_app(req: NewDealRequest):
    ensure_user(req.buyer_id, req.buyer_name)
    # Получаем имя продавца из базы, если он там есть
    con = db_conn()
    seller_row = con.execute("SELECT name FROM users WHERE user_id = ?", (req.seller_id,)).fetchone()
    seller_name = seller_row["name"] if seller_row else "Unknown"
    con.close()
    
    ensure_user(req.seller_id, seller_name)
    
    deal_id = secrets.token_hex(3).upper()
    now = datetime.utcnow().isoformat()
    
    con = db_conn()
    try:
        con.execute(
            """INSERT INTO deals (id, owner_id, owner_name, partner_id, role, amount, description, status, created_at)
               VALUES (?, ?, ?, ?, 'buyer', ?, ?, 'pending', ?)""",
            (deal_id, req.buyer_id, req.buyer_name, req.seller_id, req.amount, req.description, now)
        )
        con.commit()
        
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

# Обработка действий в сделке (Передал / Получил)
@api.post("/deal/{deal_id}/action")
async def handle_deal_action(deal_id: str, payload: DealActionIn):
    con = db_conn()
    deal = con.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    
    if not deal:
        con.close()
        raise HTTPException(404, "Сделка не найдена")
        
    is_buyer = (deal["owner_id"] == payload.user_id)
    is_seller = (deal["partner_id"] == payload.user_id)
    
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
        # Перевод средств продавцу
        add_balance(deal['partner_id'], deal['amount'])
        notify_text = f"✅ Сделка #{deal_id} завершена!\nПокупатель подтвердил получение. Средства переведены продавцу."

    else:
        con.close()
        raise HTTPException(400, "Неизвестное действие")

    con.execute("UPDATE deals SET status = ? WHERE id = ?", (new_status, deal_id))
    con.commit()
    con.close()

    target_id = deal["partner_id"] if is_buyer else deal["owner_id"]
    try:
        await bot.send_message(target_id, notify_text)
    except:
        pass

    return {"status": "ok", "new_status": new_status}

# =========================================================
#                  AIORAM HANDLERS (БОТ)
# =========================================================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    username = message.from_user.username or ""
    ensure_user(message.from_user.id, message.from_user.first_name, username)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Открыть Fragment", web_app=WebAppInfo(url=MINI_APP_URL))
    kb.adjust(1)
    
    text = (
        f"💎 *FRAGMENT · безопасная биржа*\n\n"
        f"✨ Привет, {message.from_user.first_name}! Сделки — прямо в Telegram.\n\n"
        f"🛡 *Гарант* — деньги у сервиса, пока сделка не закрыта\n"
        f"💳 *Кошелёк* — пополнение и вывод в пару тапов\n"
        f"💬 *Сделки и чат* — во встроенном приложении\n"
        f"⚡ *Автовыплата* — сразу после подтверждения"
    )
    
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

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
    if not command.args: return

    code = command.args.strip().upper()
    PROMO_CODES = {"START100": 100.0, "FRAGMENT500": 500.0, "STARS1000": 1000.0}

    if code in PROMO_CODES:
        amount = PROMO_CODES[code]
        add_balance(message.from_user.id, amount)
        PROMO_CODES.pop(code, None) 
        await message.answer(f"✅ *Код активирован!* Начислено: **{amount} ₽**", parse_mode="Markdown")
    else:
        await message.answer("❌ Неверный или использованный промокод.")

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