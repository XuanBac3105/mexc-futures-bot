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
from datetime import datetime, timedelta
import pytz

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
KNOWN_SYMBOLS = set()  # Danh sách coin đã biết
ALL_SYMBOLS = []  # Cache danh sách coin
CACHED_MOVERS = []  # Cache kết quả quét mới nhất
LAST_SCAN_TIME = None  # Thời gian quét lần cuối


# ================== UTIL ==================
async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=10) as r:
            # Chỉ log lỗi, không log success để giảm spam
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
        "/top10 – xem top 10 gainers + losers\n"
        "/gainers5 – top 10 coin tăng mạnh nhất 5 phút\n"
        "/losers5 – top 10 coin giảm mạnh nhất 5 phút\n"
        "/timelist – lịch coin sắp list trong 1 tuần\n"
        "/coinlist – coin đã list trong 1 tuần qua"
    )


async def subscribe(update, context):
    SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text("Đã bật báo!")


async def unsubscribe(update, context):
    SUBSCRIBERS.discard(update.effective_chat.id)
    await update.message.reply_text("Đã tắt báo!")


async def calc_movers(session, interval, symbols):
    """Tính % thay đổi giá cho danh sách symbols - SONG SONG"""
    import asyncio
    
    async def get_single_mover(sym):
        """Lấy dữ liệu cho 1 coin"""
        try:
            closes, vols = await get_kline(session, sym, interval, 2)
            if len(closes) < 2 or closes[-2] == 0:
                return None
            
            old_price = closes[-2]
            new_price = closes[-1]
            vol = vols[-1]
            
            chg = (new_price - old_price) / old_price * 100
            return (sym, chg, old_price, new_price, vol)
        except Exception as e:
            return None
    
    # Quét TẤT CẢ coins SONG SONG (không đợi từng coin)
    tasks = [get_single_mover(sym) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Lọc bỏ None và exceptions
    movers = [r for r in results if r is not None and not isinstance(r, Exception)]
    
    return movers


async def top10(update, context):
    """Lệnh xem top 10 gainers và losers"""
    global CACHED_MOVERS, LAST_SCAN_TIME
    
    # Dùng cache nếu có (data mới nhất từ job tự động)
    if CACHED_MOVERS:
        movers = CACHED_MOVERS
        time_ago = (datetime.now() - LAST_SCAN_TIME).seconds if LAST_SCAN_TIME else 0
        await update.message.reply_text(f"📊 Dữ liệu {time_ago}s trước...")
    else:
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


async def gainers5(update, context):
    """Lệnh xem top 10 gainers"""
    global CACHED_MOVERS, LAST_SCAN_TIME
    
    # Dùng cache nếu có
    if CACHED_MOVERS:
        movers = CACHED_MOVERS
        time_ago = (datetime.now() - LAST_SCAN_TIME).seconds if LAST_SCAN_TIME else 0
        await update.message.reply_text(f"📊 Dữ liệu {time_ago}s trước...")
    else:
        await update.message.reply_text("⏳ Đang quét...")
        async with aiohttp.ClientSession() as session:
            symbols = await get_all_symbols(session)
            movers = await calc_movers(session, "Min5", symbols)
    
    if not movers:
        await update.message.reply_text("❌ Không lấy được dữ liệu")
        return
    
    # Lọc coin có volume đủ lớn
    movers = [(s, c, o, n, v) for s, c, o, n, v in movers if v >= MIN_VOL_THRESHOLD]
    top_g = sorted(movers, key=lambda x: x[1], reverse=True)[:10]
    
    msg = "🚀 *TOP 10 GAINERS (5 phút)*\n\n"
    for i, (sym, chg, old, new, vol) in enumerate(top_g, 1):
        coin = sym.replace("_USDT", "")
        msg += f"{i}. `{coin}` {chg:+.2f}% ({old:.6g} → {new:.6g})\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")


async def losers5(update, context):
    """Lệnh xem top 10 losers"""
    global CACHED_MOVERS, LAST_SCAN_TIME
    
    # Dùng cache nếu có
    if CACHED_MOVERS:
        movers = CACHED_MOVERS
        time_ago = (datetime.now() - LAST_SCAN_TIME).seconds if LAST_SCAN_TIME else 0
        await update.message.reply_text(f"📊 Dữ liệu {time_ago}s trước...")
    else:
        await update.message.reply_text("⏳ Đang quét...")
        async with aiohttp.ClientSession() as session:
            symbols = await get_all_symbols(session)
            movers = await calc_movers(session, "Min5", symbols)
    
    if not movers:
        await update.message.reply_text("❌ Không lấy được dữ liệu")
        return
    
    # Lọc coin có volume đủ lớn
    movers = [(s, c, o, n, v) for s, c, o, n, v in movers if v >= MIN_VOL_THRESHOLD]
    top_l = sorted(movers, key=lambda x: x[1])[:10]
    
    msg = "💥 *TOP 10 LOSERS (5 phút)*\n\n"
    for i, (sym, chg, old, new, vol) in enumerate(top_l, 1):
        coin = sym.replace("_USDT", "")
        msg += f"{i}. `{coin}` {chg:+.2f}% ({old:.6g} → {new:.6g})\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")


async def timelist(update, context):
    """Lệnh xem lịch coin sẽ list trong 1 tuần"""
    await update.message.reply_text("⏳ Đang lấy lịch listing...")
    
    try:
        # Gọi API MEXC để lấy thông tin coin mới sắp list
        async with aiohttp.ClientSession() as session:
            # API lấy announcement/news về coin listing
            url = "https://www.mexc.com/api/platform/spot/market/newcoin"
            async with session.get(url, timeout=10) as r:
                if r.status != 200:
                    await update.message.reply_text("❌ Không thể lấy dữ liệu từ MEXC")
                    return
                
                data = await r.json()
                
                if not data or not data.get("data"):
                    await update.message.reply_text("📅 Chưa có coin nào sắp list trong tuần tới")
                    return
                
                # Múi giờ Việt Nam
                vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
                now = datetime.now(vn_tz)
                one_week_later = now + timedelta(days=7)
                
                msg = "📅 *LỊCH COIN SẮP LIST (1 TUẦN)*\n\n"
                count = 0
                
                for item in data.get("data", []):
                    # Lấy thời gian listing (timestamp milliseconds)
                    list_time = item.get("onlineTime")
                    if not list_time:
                        continue
                    
                    # Convert timestamp to datetime
                    dt = datetime.fromtimestamp(list_time / 1000, tz=vn_tz)
                    
                    # Chỉ hiển thị coin list trong 1 tuần tới
                    if now <= dt <= one_week_later:
                        coin_name = item.get("vcoinName", "Unknown")
                        weekday = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"][dt.weekday()]
                        date_str = dt.strftime(f"{weekday}, %d/%m/%Y lúc %H:%M")
                        
                        msg += f"🆕 `{coin_name}`\n"
                        msg += f"   ⏰ {date_str}\n\n"
                        count += 1
                
                if count == 0:
                    await update.message.reply_text("📅 Chưa có coin nào sắp list trong tuần tới")
                else:
                    await update.message.reply_text(msg, parse_mode="Markdown")
    
    except Exception as e:
        print(f"❌ Lỗi lấy lịch listing: {e}")
        await update.message.reply_text("❌ Lỗi khi lấy dữ liệu. Vui lòng thử lại sau.")


async def coinlist(update, context):
    """Lệnh xem các coin đã list trong 1 tuần"""
    await update.message.reply_text("⏳ Đang lấy danh sách coin mới...")
    
    try:
        async with aiohttp.ClientSession() as session:
            # API lấy announcement/news về coin listing
            url = "https://www.mexc.com/api/platform/spot/market/newcoin"
            async with session.get(url, timeout=10) as r:
                if r.status != 200:
                    await update.message.reply_text("❌ Không thể lấy dữ liệu từ MEXC")
                    return
                
                data = await r.json()
                
                if not data or not data.get("data"):
                    await update.message.reply_text("📋 Không có coin nào list trong tuần qua")
                    return
                
                # Múi giờ Việt Nam
                vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
                now = datetime.now(vn_tz)
                one_week_ago = now - timedelta(days=7)
                
                msg = "📋 *COIN ĐÃ LIST (1 TUẦN QUA)*\n\n"
                count = 0
                
                for item in data.get("data", []):
                    list_time = item.get("onlineTime")
                    if not list_time:
                        continue
                    
                    dt = datetime.fromtimestamp(list_time / 1000, tz=vn_tz)
                    
                    # Chỉ hiển thị coin list trong 1 tuần qua
                    if one_week_ago <= dt <= now:
                        coin_name = item.get("vcoinName", "Unknown")
                        weekday = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"][dt.weekday()]
                        date_str = dt.strftime(f"{weekday}, %d/%m/%Y lúc %H:%M")
                        
                        msg += f"✅ `{coin_name}`\n"
                        msg += f"   ⏰ {date_str}\n\n"
                        count += 1
                
                if count == 0:
                    await update.message.reply_text("📋 Không có coin nào list trong tuần qua")
                else:
                    await update.message.reply_text(msg, parse_mode="Markdown")
    
    except Exception as e:
        print(f"❌ Lỗi lấy danh sách coin: {e}")
        await update.message.reply_text("❌ Lỗi khi lấy dữ liệu. Vui lòng thử lại sau.")


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
        
        # LƯU CACHE cho các lệnh thủ công
        global CACHED_MOVERS, LAST_SCAN_TIME
        CACHED_MOVERS = movers
        LAST_SCAN_TIME = datetime.now()
    
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
    """Job phát hiện coin mới list bằng cách so sánh danh sách"""
    if not SUBSCRIBERS:
        return

    async with aiohttp.ClientSession() as session:
        try:
            symbols = await get_all_symbols(session)
        except:
            return
    
    global KNOWN_SYMBOLS
    
    # Lần đầu chạy: lưu danh sách hiện tại
    if not KNOWN_SYMBOLS:
        KNOWN_SYMBOLS = set(symbols)
        print(f"✅ Đã lưu {len(KNOWN_SYMBOLS)} coin ban đầu")
        return
    
    # So sánh với danh sách cũ
    new_coins = set(symbols) - KNOWN_SYMBOLS
    
    if new_coins:
        alerts = []
        for sym in new_coins:
            KNOWN_SYMBOLS.add(sym)
            coin = sym.replace("_USDT", "")
            alerts.append(f"🆕 *COIN MỚI LIST:* `{coin}`")
            print(f"🆕 NEW LISTING: {sym}")
        
        # Gửi thông báo
        text = "\n".join(alerts)
        for chat in SUBSCRIBERS:
            try:
                await context.bot.send_message(chat, text, parse_mode="Markdown")
            except Exception as e:
                print(f"❌ Lỗi gửi thông báo coin mới: {e}")


# ================== MAIN ==================
async def post_init(app):
    """Set bot commands menu"""
    from telegram import BotCommand
    
    commands = [
        BotCommand("start", "Khởi động bot và xem hướng dẫn"),
        BotCommand("subscribe", "Bật thông báo pump/dump tự động"),
        BotCommand("unsubscribe", "Tắt thông báo tự động"),
        BotCommand("top10", "Top 10 coin tăng/giảm mạnh nhất"),
        BotCommand("gainers5", "Top 10 coin tăng mạnh nhất 5 phút"),
        BotCommand("losers5", "Top 10 coin giảm mạnh nhất 5 phút"),
        BotCommand("timelist", "Lịch coin sắp list trong 1 tuần"),
        BotCommand("coinlist", "Coin đã list trong 1 tuần qua"),
    ]
    
    await app.bot.set_my_commands(commands)
    print("✅ Đã thiết lập menu lệnh bot")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("top10", top10))
    app.add_handler(CommandHandler("gainers5", gainers5))
    app.add_handler(CommandHandler("losers5", losers5))
    app.add_handler(CommandHandler("timelist", timelist))
    app.add_handler(CommandHandler("coinlist", coinlist))

    jq = app.job_queue
    # Quét pump/dump mỗi 30 giây (nhanh hơn) - cho phép 2 instances chạy song song
    jq.run_repeating(job_scan_pumps_dumps, 30, first=10, job_kwargs={'max_instances': 2})
    # Kiểm tra coin mới mỗi 5 phút
    jq.run_repeating(job_new_listing, 300, first=30)

    print("🔥 Bot quét MEXC Futures đang chạy...")
    print(f"📊 Ngưỡng pump: >= {PUMP_THRESHOLD}%")
    print(f"📊 Ngưỡng dump: <= {DUMP_THRESHOLD}%")
    print(f"💰 Volume tối thiểu: {MIN_VOL_THRESHOLD:,}")
    app.run_polling()


if __name__ == "__main__":
    main()
