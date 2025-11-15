import os
import aiohttp
from statistics import mean
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from dotenv import load_dotenv

# Load biến môi trường từ file .env
load_dotenv()

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")

FUTURES_BASE = "https://contract.mexc.co"

# Ngưỡng để báo động (%)
PUMP_THRESHOLD = 5.0    # Tăng >= 5% trong 5 phút
DUMP_THRESHOLD = -5.0   # Giảm >= 5% trong 5 phút

# Volume tối thiểu để tránh coin ít thanh khoản
MIN_VOL_THRESHOLD = 100000

SUBSCRIBERS = set()
KNOWN_NEW = set()
ALL_SYMBOLS = []  # Cache danh sách coin


# ================== UTIL ==================
async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=10) as r:
            print(f"📡 API Call: {url} - Status: {r.status}")
            r.raise_for_status()
            data = await r.json()
            return data.get("data", data)
    except Exception as e:
        print(f"❌ Error calling {url}: {e}")
        raise


async def get_kline(session, symbol, interval="Min5", limit=10):
    url = f"{FUTURES_BASE}/api/v1/contract/kline/{symbol}"
    data = await fetch_json(session, url, {"interval": interval})
    closes = [float(x) for x in data["close"][-limit:]]
    vols = [float(v) for v in data["vol"][-limit:]]
    return closes, vols


async def get_all_contracts(session):
    url = f"{FUTURES_BASE}/api/v1/contract/detail"
    data = await fetch_json(session, url)
    if isinstance(data, dict): data = [data]

    return [
        c for c in data
        if c.get("settleCoin") == "USDT" and c.get("state") == 0
    ]


async def get_all_symbols(session):
    """Lấy danh sách TẤT CẢ symbol USDT Futures đang active"""
    contracts = await get_all_contracts(session)
    return [c["symbol"] for c in contracts if c.get("symbol")]


def fmt_top(title, data):
    txt = [f"🔥 *{title}*"]
    for i, (sym, chg) in enumerate(data, start=1):
        icon = "🚀" if chg > 0 else "💥"
        txt.append(f"{i}. {icon} `{sym}` → {chg:+.2f}%")
    return "\n".join(txt)


def fmt_alert(symbol, old_price, new_price, change_pct):
    """Format báo động pump/dump"""
    color = "🟢" if change_pct >= 0 else "🔴"
    icon = "🚀🚀🚀" if change_pct >= 0 else "💥💥💥"
    # Lấy tên coin (bỏ _USDT)
    coin_name = symbol.replace("_USDT", "")
    return (
        f"┌{icon} {coin_name} ⚡ {change_pct:+.2f}% {color}\n"
        f"└ {old_price:.6g} → {new_price:.6g}"
    )


# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text(
        "🤖 Bot Quét MEXC Futures đã sẵn sàng!\n\n"
        "Bot sẽ tự động quét TẤT CẢ coin trên MEXC Futures\n"
        "và báo ngay khi có biến động mạnh (±5%)\n\n"
        "Các lệnh:\n"
        "/subscribe – bật báo động\n"
        "/unsubscribe – tắt báo động\n"
        "/top10 – xem top 10 gainers/losers hiện tại"
    )


async def subscribe(update, context):
    SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text("Đã bật báo!")


async def unsubscribe(update, context):
    SUBSCRIBERS.discard(update.effective_chat.id)
    await update.message.reply_text("Đã tắt báo!")


async def calc_movers(session, interval, symbols):
    """Tính % thay đổi giá cho danh sách symbols"""
    movers = []
    for sym in symbols:
        try:
            closes, vols = await get_kline(session, sym, interval, 2)
            if len(closes) < 2 or closes[-2] == 0:
                continue
            
            old_price = closes[-2]
            new_price = closes[-1]
            vol = vols[-1]
            
            chg = (new_price - old_price) / old_price * 100
            movers.append((sym, chg, old_price, new_price, vol))
        except Exception as e:
            # Bỏ qua coin lỗi (có thể mới list hoặc không có data)
            pass
    return movers


async def top10(update, context):
    """Lệnh xem top 10 gainers và losers"""
    await update.message.reply_text("⏳ Đang quét tất cả coin...")
    
    async with aiohttp.ClientSession() as session:
        symbols = await get_all_symbols(session)
        movers = await calc_movers(session, "Min5", symbols)
    
    if not movers:
        await update.message.reply_text("❌ Không lấy được dữ liệu")
        return
    
    # Lọc coin có volume đủ lớn
    movers = [(s, c, o, n, v) for s, c, o, n, v in movers if v >= MIN_VOL_THRESHOLD]
    
    top_g = sorted(movers, key=lambda x: x[1], reverse=True)[:10]
    top_l = sorted(movers, key=lambda x: x[1])[:10]
    
    msg_g = "🚀 *TOP 10 GAINERS (5 phút)*\n"
    for i, (sym, chg, old, new, vol) in enumerate(top_g, 1):
        coin = sym.replace("_USDT", "")
        msg_g += f"{i}. `{coin}` {chg:+.2f}%\n"
    
    msg_l = "\n💥 *TOP 10 LOSERS (5 phút)*\n"
    for i, (sym, chg, old, new, vol) in enumerate(top_l, 1):
        coin = sym.replace("_USDT", "")
        msg_l += f"{i}. `{coin}` {chg:+.2f}%\n"
    
    await update.message.reply_text(msg_g + msg_l, parse_mode="Markdown")


# ================== JOBS ==================
async def job_scan_pumps_dumps(context):
    """Job chính: Quét TẤT CẢ coin và báo khi có pump/dump"""
    if not SUBSCRIBERS:
        return
    
    print("🔍 Đang quét tất cả coin...")
    
    async with aiohttp.ClientSession() as session:
        # Lấy danh sách tất cả symbols
        global ALL_SYMBOLS
        if not ALL_SYMBOLS:
            ALL_SYMBOLS = await get_all_symbols(session)
            print(f"✅ Tìm thấy {len(ALL_SYMBOLS)} coin")
        
        # Tính movers cho tất cả coin
        movers = await calc_movers(session, "Min5", ALL_SYMBOLS)
    
    if not movers:
        return
    
    # Lọc coin có volume đủ và biến động mạnh
    alerts = []
    for sym, chg, old_price, new_price, vol in movers:
        if vol < MIN_VOL_THRESHOLD:
            continue
        
        # PUMP: tăng >= ngưỡng
        if chg >= PUMP_THRESHOLD:
            msg = fmt_alert(sym, old_price, new_price, chg)
            alerts.append(msg)
            print(f"🚀 PUMP: {sym} {chg:+.2f}%")
        
        # DUMP: giảm >= ngưỡng
        elif chg <= DUMP_THRESHOLD:
            msg = fmt_alert(sym, old_price, new_price, chg)
            alerts.append(msg)
            print(f"� DUMP: {sym} {chg:+.2f}%")
    
    # Gửi alert đến tất cả subscribers
    if alerts:
        # Gom nhóm để tránh spam
        text = "\n\n".join(alerts[:10])  # Chỉ gửi tối đa 10 alert mỗi lần
        if len(alerts) > 10:
            text += f"\n\n... và {len(alerts) - 10} coin khác"
        
        for chat in SUBSCRIBERS:
            try:
                await context.bot.send_message(chat, text, parse_mode="Markdown")
            except Exception as e:
                print(f"❌ Lỗi gửi tin nhắn: {e}")


async def job_new_listing(context):
    """Job phát hiện coin mới list"""
    if not SUBSCRIBERS:
        return

    async with aiohttp.ClientSession() as session:
        try:
            contracts = await get_all_contracts(session)
        except:
            return

    alerts = []
    for c in contracts:
        sym = c["symbol"]
        if sym not in KNOWN_NEW and c.get("isNew"):
            KNOWN_NEW.add(sym)
            coin = sym.replace("_USDT", "")
            alerts.append(f"🆕 *Coin mới list:* `{coin}`")
            print(f"🆕 NEW: {sym}")

    if alerts:
        text = "\n".join(alerts)
        for chat in SUBSCRIBERS:
            try:
                await context.bot.send_message(chat, text, parse_mode="Markdown")
            except:
                pass


# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("top10", top10))

    jq = app.job_queue
    # Quét pump/dump mỗi 30 giây (nhanh hơn)
    jq.run_repeating(job_scan_pumps_dumps, 30, first=10)
    # Kiểm tra coin mới mỗi 5 phút
    jq.run_repeating(job_new_listing, 300, first=30)

    print("🔥 Bot quét MEXC Futures đang chạy...")
    print(f"📊 Ngưỡng pump: >= {PUMP_THRESHOLD}%")
    print(f"📊 Ngưỡng dump: <= {DUMP_THRESHOLD}%")
    print(f"💰 Volume tối thiểu: {MIN_VOL_THRESHOLD:,}")
    app.run_polling()


if __name__ == "__main__":
    main()
