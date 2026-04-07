import asyncio
import logging
import os
import json
import httpx
from datetime import datetime, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─── ВСТАВЬТЕ ВАШИ КЛЮЧИ ──────────────────────────────────────
TELEGRAM_TOKEN="8185689201:AAEoD9gbD6XkVZZ8hQjwAFXrzw0F0KneZbk"
DEEPSEEK_API_KEY="sk-f9476***********************536e"
# ──────────────────────────────────────────────────────────────

# ─── ЗАЩИТА — только владелец может пользоваться ботом ────────
OWNER_ID = 690453849  # Ваш Telegram ID — только вы!
# ──────────────────────────────────────────────────────────────

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# ─── Загрузка базы знаний ──────────────────────────────────────
def load_knowledge():
    try:
        if os.path.exists("knowledge.txt"):
            with open("knowledge.txt", "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""

KNOWLEDGE = load_knowledge()

WATCHLIST = {
    "Акции": ["MSFT", "GOOGL", "AMZN", "NVDA", "AMD", "MU", "FSLR"],
    "Крипта": ["BTC-USD", "ETH-USD"],
    "Сырьё":  ["GC=F", "CL=F"]
}

PORTFOLIO_FILE = "portfolio.json"
JOURNAL_FILE   = "journal.json"
ALERTS_FILE    = "alerts.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

user_histories: dict[int, list] = {}

SYSTEM_PROMPT = f"""Ты — персональный торговый ассистент инвестора с 20-летним опытом.
Ты знаешь всю историю, стратегию и портфель своего клиента.

БАЗА ЗНАНИЙ КЛИЕНТА:
{KNOWLEDGE}

ТВОИ ПРАВИЛА:
- Всегда учитывай текущий портфель и активные ордера клиента
- Стратегия: холодные лимитки — работа от уровней, без эмоций
- Философия: Физика, а не хайп. Владей дефицитом, а не приложением
- Отвечай на русском языке, чётко и структурированно
- Давай: анализ ситуации, уровни поддержки/сопротивления, сценарии, рекомендацию
- Напоминай про активные ордера если они релевантны вопросу
- Используй эмодзи. Дисклеймер в конце."""

def load_json(filename, default=None):
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки {filename}: {e}")
    return default if default is not None else {}

def save_json(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения {filename}: {e}")

async def get_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev  = meta.get("chartPreviousClose", price)
            change = ((price - prev) / prev * 100) if prev else 0
            return {"symbol": symbol, "price": round(price, 2), "change": round(change, 2)}
    except Exception as e:
        logger.error(f"Ошибка цены {symbol}: {e}")
        return None

async def get_all_prices():
    all_symbols = [s for group in WATCHLIST.values() for s in group]
    results = await asyncio.gather(*[get_price(s) for s in all_symbols])
    return {r["symbol"]: r for r in results if r}

def format_prices(prices):
    lines = []
    for group, symbols in WATCHLIST.items():
        lines.append(f"\n*{group}*")
        for sym in symbols:
            if sym in prices:
                p = prices[sym]
                emoji = "🟢" if p["change"] >= 0 else "🔴"
                sign  = "+" if p["change"] >= 0 else ""
                name  = sym.replace("-USD", "").replace("=F", "")
                lines.append(f"{emoji} `{name:<6}` ${p['price']:>10,.2f}  {sign}{p['change']:.2f}%")
            else:
                lines.append(f"⚪ `{sym}` — нет данных")
    return "\n".join(lines)

async def ask_deepseek(user_id, user_message):
    if user_id not in user_histories:
        user_histories[user_id] = []
    user_histories[user_id].append({"role": "user", "content": user_message})
    history = user_histories[user_id][-20:]
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
        "temperature": 0.7,
        "max_tokens": 2000
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(DEEPSEEK_URL, json=payload, headers=headers)
            r.raise_for_status()
            answer = r.json()["choices"][0]["message"]["content"]
            user_histories[user_id].append({"role": "assistant", "content": answer})
            return answer
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "❌ Неверный ключ DeepSeek. Проверьте DEEPSEEK_API_KEY в bot.py"
        return f"❌ Ошибка DeepSeek: {e.response.status_code}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"

async def send_morning_digest(context):
    user_id = context.job.data
    try:
        prices = await get_all_prices()
        price_text = format_prices(prices)
        movers = [f"{sym}: ${p['price']} ({p['change']:+.2f}%)" for sym, p in prices.items()]
        prompt = (
            f"Утренний брифинг {datetime.now().strftime('%d.%m.%Y')}.\n"
            f"Цены:\n{chr(10).join(movers)}\n\n"
            "Дай краткий утренний анализ: ключевые движения, на что обратить внимание сегодня, "
            "какие лимитные ордера могут сработать по нашему портфелю."
        )
        analysis = await ask_deepseek(user_id, prompt)
        msg = (
            f"🌅 *Утренний дайджест — {datetime.now().strftime('%d.%m.%Y %H:%M')}*\n"
            f"{price_text}\n\n{'─'*30}\n{analysis}"
        )
        await send_long_message_direct(context.bot, user_id, msg)
    except Exception as e:
        logger.error(f"Ошибка дайджеста: {e}")

async def check_alerts(context):
    alerts = load_json(ALERTS_FILE, {})
    if not alerts:
        return
    prices = await get_all_prices()
    triggered = []
    for alert_id, alert in list(alerts.items()):
        sym    = alert["symbol"]
        target = alert["target"]
        cond   = alert["condition"]
        uid    = alert["user_id"]
        price_data = prices.get(sym) or prices.get(sym + "-USD")
        if not price_data:
            continue
        current = price_data["price"]
        hit = (cond == "above" and current >= target) or (cond == "below" and current <= target)
        if hit:
            triggered.append(alert_id)
            direction = "достиг" if cond == "above" else "упал до"
            msg = (
                f"🚨 *АЛЕРТ СРАБОТАЛ!*\n\n"
                f"*{sym}* {direction} уровня ${target:,.2f}\n"
                f"Текущая цена: *${current:,.2f}*\n\nПроверьте лимитный ордер!"
            )
            try:
                await context.bot.send_message(uid, msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Ошибка отправки алерта: {e}")
    for aid in triggered:
        del alerts[aid]
    if triggered:
        save_json(ALERTS_FILE, alerts)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    uid = update.effective_user.id
    context.job_queue.run_daily(send_morning_digest, time=time(7, 0, 0), data=uid, name=f"digest_{uid}")
    context.job_queue.run_repeating(check_alerts, interval=300, first=10, name=f"alerts_{uid}")
    keyboard = [
        [InlineKeyboardButton("📊 Цены сейчас", callback_data="prices"),
         InlineKeyboardButton("💼 Портфель",    callback_data="portfolio")],
        [InlineKeyboardButton("🚨 Мои алерты",  callback_data="my_alerts"),
         InlineKeyboardButton("📓 Журнал",      callback_data="journal")],
        [InlineKeyboardButton("🌅 Дайджест",    callback_data="digest"),
         InlineKeyboardButton("❓ Помощь",      callback_data="help")]
    ]
    await update.message.reply_text(
        "👋 *Торговый ИИ-ассистент v2.0*\n\n"
        "📌 *Возможности:*\n"
        "• Цены в реальном времени\n"
        "• Утренний дайджест в 7:00\n"
        "• Алерты по уровням 24/7\n"
        "• Журнал сделок и портфель\n"
        "• ИИ-анализ через DeepSeek\n\n"
        "💬 Напишите вопрос или выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Команды бота:*\n\n"
        "/prices — цены всех активов\n"
        "/alert NVDA above 500 — алерт выше $500\n"
        "/alert BTC-USD below 80000 — алерт ниже $80000\n"
        "/alerts — список моих алертов\n"
        "/buy NVDA 10 450.50 — записать покупку\n"
        "/sell NVDA 5 520.00 — записать продажу\n"
        "/portfolio — мой портфель с П/У\n"
        "/journal — журнал сделок\n"
        "/digest — дайджест прямо сейчас\n"
        "/clear — очистить историю чата\n\n"
        "💬 Или просто напишите вопрос:\n"
        "_Анализ NVDA, цена 480, что думаешь?_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Получаю цены...")
    prices = await get_all_prices()
    text = f"📊 *Цены — {datetime.now().strftime('%d.%m.%Y %H:%M')}*\n{format_prices(prices)}"
    await msg.edit_text(text, parse_mode="Markdown")

async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            "❓ Формат: `/alert СИМВОЛ above/below ЦЕНА`\n\nПримеры:\n`/alert NVDA above 500`\n`/alert BTC-USD below 80000`",
            parse_mode="Markdown"
        )
        return
    symbol = args[0].upper()
    condition = args[1].lower()
    try:
        target = float(args[2])
    except ValueError:
        await update.message.reply_text("❌ Цена должна быть числом")
        return
    if condition not in ["above", "below"]:
        await update.message.reply_text("❌ Условие: above или below")
        return
    alerts = load_json(ALERTS_FILE, {})
    alert_id = f"{update.effective_user.id}_{symbol}_{condition}_{target}_{datetime.now().timestamp():.0f}"
    alerts[alert_id] = {"user_id": update.effective_user.id, "symbol": symbol, "condition": condition, "target": target, "created": datetime.now().isoformat()}
    save_json(ALERTS_FILE, alerts)
    direction = "поднимется выше" if condition == "above" else "опустится ниже"
    await update.message.reply_text(f"✅ *Алерт установлен!*\n\n📌 {symbol} {direction} *${target:,.2f}*\nПроверка каждые 5 минут.", parse_mode="Markdown")

async def cmd_my_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = load_json(ALERTS_FILE, {})
    uid = update.effective_user.id
    my = {k: v for k, v in alerts.items() if v["user_id"] == uid}
    if not my:
        await update.message.reply_text("📭 Нет активных алертов.\n\nДобавьте: `/alert NVDA above 500`", parse_mode="Markdown")
        return
    lines = ["🚨 *Ваши алерты:*\n"]
    for i, (aid, a) in enumerate(my.items(), 1):
        direction = "▲ выше" if a["condition"] == "above" else "▼ ниже"
        lines.append(f"{i}. `{a['symbol']}` {direction} ${a['target']:,.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def _add_trade(update, context, trade_type):
    args = context.args
    if len(args) != 3:
        cmd = "buy" if trade_type == "BUY" else "sell"
        await update.message.reply_text(f"❓ Формат: `/{cmd} СИМВОЛ КОЛ-ВО ЦЕНА`\nПример: `/{cmd} NVDA 10 450.50`", parse_mode="Markdown")
        return
    try:
        symbol = args[0].upper()
        qty    = float(args[1])
        price  = float(args[2])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат числа")
        return
    journal = load_json(JOURNAL_FILE, [])
    trade = {"user_id": update.effective_user.id, "type": trade_type, "symbol": symbol, "qty": qty, "price": price, "total": round(qty * price, 2), "date": datetime.now().strftime("%d.%m.%Y %H:%M")}
    journal.append(trade)
    save_json(JOURNAL_FILE, journal)
    emoji = "🟢" if trade_type == "BUY" else "🔴"
    action = "Покупка" if trade_type == "BUY" else "Продажа"
    await update.message.reply_text(f"{emoji} *{action} записана!*\n\n📌 {symbol}\nКол-во: {qty} | Цена: ${price:,.2f}\nСумма: *${trade['total']:,.2f}*", parse_mode="Markdown")

async def cmd_buy(update, context):
    await _add_trade(update, context, "BUY")

async def cmd_sell(update, context):
    await _add_trade(update, context, "SELL")

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    journal = load_json(JOURNAL_FILE, [])
    my_trades = [t for t in journal if t["user_id"] == uid]
    if not my_trades:
        await update.message.reply_text("📭 Портфель пуст.\n\nДобавьте: `/buy NVDA 10 450.50`", parse_mode="Markdown")
        return
    positions = {}
    for t in my_trades:
        sym = t["symbol"]
        if sym not in positions:
            positions[sym] = {"qty": 0, "cost": 0}
        if t["type"] == "BUY":
            positions[sym]["qty"]  += t["qty"]
            positions[sym]["cost"] += t["total"]
        else:
            positions[sym]["qty"]  -= t["qty"]
            positions[sym]["cost"] -= t["total"]
    price_results = await asyncio.gather(*[get_price(s) for s in positions.keys()])
    current_prices = {r["symbol"]: r["price"] for r in price_results if r}
    lines = [f"💼 *Портфель — {datetime.now().strftime('%d.%m.%Y')}*\n"]
    total_cost = total_value = 0
    for sym, pos in positions.items():
        if pos["qty"] <= 0:
            continue
        avg = pos["cost"] / pos["qty"]
        current = current_prices.get(sym, 0)
        value = pos["qty"] * current
        pnl = value - pos["cost"]
        pnl_pct = (pnl / pos["cost"] * 100) if pos["cost"] > 0 else 0
        emoji = "🟢" if pnl >= 0 else "🔴"
        sign = "+" if pnl >= 0 else ""
        lines.append(f"{emoji} *{sym}*\n   {pos['qty']} шт | Ср: ${avg:,.2f} | Текущая: ${current:,.2f}\n   П/У: {sign}${pnl:,.2f} ({sign}{pnl_pct:.1f}%)\n")
        total_cost += pos["cost"]
        total_value += value
    total_pnl = total_value - total_cost
    total_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    emoji = "🟢" if total_pnl >= 0 else "🔴"
    sign = "+" if total_pnl >= 0 else ""
    lines.append(f"{'─'*25}\n{emoji} *Итого: ${total_value:,.2f}*\nП/У: *{sign}${total_pnl:,.2f}* ({sign}{total_pct:.1f}%)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    journal = load_json(JOURNAL_FILE, [])
    my_trades = [t for t in journal if t["user_id"] == uid][-15:]
    if not my_trades:
        await update.message.reply_text("📭 Журнал пуст.")
        return
    lines = ["📓 *Последние сделки:*\n"]
    for t in reversed(my_trades):
        emoji = "🟢" if t["type"] == "BUY" else "🔴"
        action = "Покупка" if t["type"] == "BUY" else "Продажа"
        lines.append(f"{emoji} {t['date']} | {action} {t['symbol']} x{t['qty']} @ ${t['price']:,.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Готовлю дайджест...")
    prices = await get_all_prices()
    price_text = format_prices(prices)
    movers = [f"{sym}: ${p['price']} ({p['change']:+.2f}%)" for sym, p in prices.items()]
    prompt = (f"Дайджест {datetime.now().strftime('%d.%m.%Y %H:%M')}.\nЦены:\n{chr(10).join(movers)}\n\nДай краткий анализ: ключевые движения, возможности для лимитных ордеров.")
    analysis = await ask_deepseek(update.effective_user.id, prompt)
    text = f"🌅 *Дайджест — {datetime.now().strftime('%d.%m.%Y %H:%M')}*\n{price_text}\n\n{'─'*25}\n{analysis}"
    await msg.delete()
    await send_long_message(update.message, text)

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories.pop(update.effective_user.id, None)
    await update.message.reply_text("🗑 История очищена!")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    class FakeUpdate:
        def __init__(self, message, user):
            self.message = message
            self.effective_user = user

    fu = FakeUpdate(query.message, query.from_user)

    if query.data == "prices":
        msg = await query.message.reply_text("⏳ Получаю цены...")
        prices = await get_all_prices()
        text = f"📊 *Цены — {datetime.now().strftime('%d.%m.%Y %H:%M')}*\n{format_prices(prices)}"
        await msg.edit_text(text, parse_mode="Markdown")
    elif query.data == "portfolio":
        await cmd_portfolio(fu, context)
    elif query.data == "my_alerts":
        await cmd_my_alerts(fu, context)
    elif query.data == "journal":
        await cmd_journal(fu, context)
    elif query.data == "digest":
        await cmd_digest(fu, context)
    elif query.data == "help":
        await cmd_help(fu, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ Доступ запрещён.")
        logger.warning(f"Попытка доступа от {user.first_name} (id={user.id})")
        return
    logger.info(f"Сообщение от {user.first_name} (id={user.id}): {text[:80]}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    thinking_msg = await update.message.reply_text("🤔 Анализирую...")
    answer = await ask_deepseek(user.id, text)
    await thinking_msg.delete()
    await send_long_message(update.message, answer)


async def send_long_message(message, text):
    max_len = 4000
    if len(text) <= max_len:
        try:
            await message.reply_text(text, parse_mode="Markdown")
        except Exception:
            await message.reply_text(text)
    else:
        parts = [text[i:i+max_len] for i in range(0, len(text), max_len)]
        for i, part in enumerate(parts):
            prefix = f"📄 Часть {i+1}/{len(parts)}\n\n" if len(parts) > 1 else ""
            try:
                await message.reply_text(prefix + part, parse_mode="Markdown")
            except Exception:
                await message.reply_text(prefix + part)
            await asyncio.sleep(0.3)

async def send_long_message_direct(bot, chat_id, text):
    max_len = 4000
    parts = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    for part in parts:
        try:
            await bot.send_message(chat_id, part, parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id, part)
        await asyncio.sleep(0.3)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)

def main():
    logger.info("🚀 Запуск торгового бота v2.0...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("prices",    cmd_prices))
    app.add_handler(CommandHandler("alert",     cmd_alert))
    app.add_handler(CommandHandler("alerts",    cmd_my_alerts))
    app.add_handler(CommandHandler("buy",       cmd_buy))
    app.add_handler(CommandHandler("sell",      cmd_sell))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("journal",   cmd_journal))
    app.add_handler(CommandHandler("digest",    cmd_digest))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("✅ Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
