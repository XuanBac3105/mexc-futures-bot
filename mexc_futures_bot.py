import os
import aiohttp
import asyncio
import json
import websockets
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
from collections import defaultdict

# Load biến môi trường từ file .env
load_dotenv()

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")

FUTURES_BASE = "https://contract.mexc.co"
WEBSOCKET_URL = "wss://contract.mexc.com/edge"  # MEXC Futures WebSocket endpoint

# Ngưỡng để báo động (%)
PUMP_THRESHOLD = 2.0    # Tăng >= 2% trong 1 phút
DUMP_THRESHOLD = -2.0   # Giảm >= 2% trong 1 phút

# Volume tối thiểu để tránh coin ít thanh khoản
MIN_VOL_THRESHOLD = 100000

SUBSCRIBERS = set()
KNOWN_SYMBOLS = set()  # Danh sách coin đã biết
ALL_SYMBOLS = []  # Cache danh sách coin

# WebSocket price tracking
LAST_PRICES = {}  # {symbol: {"price": float, "time": datetime}}
BASE_PRICES = {}  # {symbol: base_price} - giá base để so sánh
ALERTED_SYMBOLS = {}  # {symbol: timestamp} - tránh spam alert


# ================== UTIL ==================
async def fetch_json(session, url, params=None, retry=3):
    """Fetch JSON với retry logic cho 429 errors"""
    import random
    
    for attempt in range(retry):
        try:
            async with session.get(url, params=params, timeout=10) as r:
                if r.status == 429:
                    # Rate limit - đợi exponential backoff
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    print(f"⚠️ Rate limit {url}, retry sau {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    continue
                
                r.raise_for_status()
                data = await r.json()
                return data.get("data", data)
        except Exception as e:
            if attempt == retry - 1:  # Lần thử cuối
                print(f"❌ Error calling {url}: {e}")
                raise
            # Thử lại với delay
            await asyncio.sleep(random.uniform(0.5, 1.5))
    
    raise Exception(f"Failed after {retry} retries")


async def get_kline(session, symbol, interval="Min5", limit=10):
    url = f"{FUTURES_BASE}/api/v1/contract/kline/{symbol}"
    data = await fetch_json(session, url, {"interval": interval})
    closes = [float(x) for x in data["close"][-limit:]]
    highs = [float(x) for x in data["high"][-limit:]]
    lows = [float(x) for x in data["low"][-limit:]]
    vols = [float(v) for v in data["vol"][-limit:]]
    return closes, highs, lows, vols


async def get_ticker(session, symbol):
    """Lấy giá ticker hiện tại (realtime)"""
    url = f"{FUTURES_BASE}/api/v1/contract/ticker/{symbol}"
    data = await fetch_json(session, url)
    return float(data["lastPrice"]) if data and "lastPrice" in data else None


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
    """Format báo động pump/dump với link"""
    color = "🟢" if change_pct >= 0 else "🔴"
    icon = "🚀🚀🚀" if change_pct >= 0 else "💥💥💥"
    # Lấy tên coin (bỏ _USDT)
    coin_name = symbol.replace("_USDT", "")
    
    # Tạo link đến trang futures
    link = f"https://www.mexc.co/vi-VN/futures/{symbol}?type=linear_swap"
    
    return (
        f"┌{icon} [{coin_name}]({link}) ⚡ {change_pct:+.2f}% {color}\n"
        f"└ {old_price:.6g} → {new_price:.6g}"
    )


# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text(
        "🤖 Bot Quét MEXC Futures - WebSocket Realtime!\n\n"
        "✅ WebSocket stream cho 722 coins\n"
        "✅ Nhận giá REALTIME từ server\n"
        "✅ Báo NGAY LẬP TỨC khi ≥±2%\n"
        "✅ Không miss bất kỳ pump/dump nào\n\n"
        "Các lệnh:\n"
        "/subscribe – bật báo động\n"
        "/unsubscribe – tắt báo động\n"
        "/timelist – lịch coin sắp list\n"
        "/coinlist – coin vừa list gần đây"
    )


async def subscribe(update, context):
    SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text("Đã bật báo!")


async def unsubscribe(update, context):
    SUBSCRIBERS.discard(update.effective_chat.id)
    await update.message.reply_text("Đã tắt báo!")


async def websocket_stream(context):
    """WebSocket stream để nhận giá realtime từ MEXC Futures"""
    while True:
        try:
            async with websockets.connect(WEBSOCKET_URL) as ws:
                print(f"✅ Kết nối WebSocket thành công")
                
                # Subscribe tất cả ticker streams - MEXC Futures format
                for symbol in ALL_SYMBOLS:
                    # MEXC Futures WebSocket format: sub.ticker
                    sub_msg = {
                        "method": "sub.ticker",
                        "param": {
                            "symbol": symbol
                        }
                    }
                    await ws.send(json.dumps(sub_msg))
                    await asyncio.sleep(0.005)  # 5ms delay giữa subscriptions
                
                print(f"✅ Đã subscribe {len(ALL_SYMBOLS)} coin qua WebSocket")
                
                # Lắng nghe messages
                async for message in ws:
                    try:
                        data = json.loads(message)
                        
                        # Xử lý ping/pong
                        if "ping" in data:
                            await ws.send(json.dumps({"pong": data["ping"]}))
                            continue
                        
                        # Xử lý ticker data
                        if "channel" in data and data.get("channel") == "push.ticker":
                            if "data" in data:
                                await process_ticker(data["data"], context)
                            
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        print(f"❌ Error processing message: {e}")
                        continue
                        
        except Exception as e:
            print(f"❌ WebSocket error: {e}")
            print("🔄 Reconnecting in 5s...")
            await asyncio.sleep(5)


async def process_ticker(ticker_data, context):
    """Xử lý ticker data từ WebSocket và phát hiện pump/dump"""
    symbol = ticker_data.get("symbol")
    if not symbol:
        return
    
    try:
        current_price = float(ticker_data.get("lastPrice", 0))
        volume = float(ticker_data.get("volume24", 0))
        
        if current_price == 0 or volume < MIN_VOL_THRESHOLD:
            return
        
        # Lưu giá hiện tại
        LAST_PRICES[symbol] = {
            "price": current_price,
            "time": datetime.now()
        }
        
        # Thiết lập base price nếu chưa có
        if symbol not in BASE_PRICES:
            BASE_PRICES[symbol] = current_price
            return
        
        # Tính % thay đổi so với base price
        base_price = BASE_PRICES[symbol]
        change_pct = (current_price - base_price) / base_price * 100
        
        # Kiểm tra ngưỡng
        now = datetime.now()
        should_alert = False
        
        if change_pct >= PUMP_THRESHOLD or change_pct <= DUMP_THRESHOLD:
            # Kiểm tra đã alert gần đây chưa (cooldown 60s)
            last_alert = ALERTED_SYMBOLS.get(symbol)
            if not last_alert or (now - last_alert).seconds > 60:
                should_alert = True
                ALERTED_SYMBOLS[symbol] = now
        
        if should_alert and SUBSCRIBERS:
            msg = fmt_alert(symbol, base_price, current_price, change_pct)
            
            if change_pct >= PUMP_THRESHOLD:
                print(f"🚀 PUMP: {symbol} {change_pct:+.2f}%")
            else:
                print(f"💥 DUMP: {symbol} {change_pct:+.2f}%")
            
            # Gửi alert
            for chat in SUBSCRIBERS:
                try:
                    await context.bot.send_message(
                        chat,
                        msg,
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                except Exception as e:
                    print(f"❌ Lỗi gửi tin nhắn: {e}")
            
            # Reset base price sau khi alert
            BASE_PRICES[symbol] = current_price
            
    except Exception as e:
        print(f"❌ Error processing ticker for {symbol}: {e}")


async def reset_base_prices(context):
    """Job reset base prices mỗi 1 phút để phát hiện pump/dump mới"""
    global BASE_PRICES
    
    # Cập nhật base prices từ last prices
    for symbol, data in LAST_PRICES.items():
        BASE_PRICES[symbol] = data["price"]
    
    print(f"🔄 Reset {len(BASE_PRICES)} base prices")


async def calc_movers(session, interval, symbols):
    """Tính % thay đổi giá cho danh sách symbols - BATCH để tránh rate limit"""
    import asyncio
    
    async def get_single_mover(sym):
        """Lấy dữ liệu cho 1 coin - so sánh giá HIỆN TẠI vs candle cuối (bao gồm HIGH/LOW để bắt râu)"""
        try:
            # Lấy candle đã đóng (close, high, low, volume)
            closes, highs, lows, vols = await get_kline(session, sym, interval, 2)
            if len(closes) < 1 or closes[-1] == 0:
                return None
            
            # Lấy giá REALTIME hiện tại
            current_price = await get_ticker(session, sym)
            if not current_price:
                return None
            
            # Giá base để tính % thay đổi
            base_price = closes[-1]  # Candle đóng cửa
            high_price = highs[-1]   # Giá cao nhất của candle
            low_price = lows[-1]     # Giá thấp nhất của candle
            vol = vols[-1]
            
            # Tính % thay đổi so với close
            chg_from_close = (current_price - base_price) / base_price * 100
            
            # Kiểm tra xem giá hiện tại có vượt HIGH hoặc LOW không (phát hiện breakout)
            chg_from_high = (current_price - high_price) / high_price * 100
            chg_from_low = (current_price - low_price) / low_price * 100
            
            # Chọn % thay đổi lớn nhất để phát hiện các spike/wick
            if abs(chg_from_close) >= abs(chg_from_high) and abs(chg_from_close) >= abs(chg_from_low):
                chg = chg_from_close
                old_price = base_price
            elif abs(chg_from_high) > abs(chg_from_low):
                chg = chg_from_high
                old_price = high_price
            else:
                chg = chg_from_low
                old_price = low_price
            
            return (sym, chg, old_price, current_price, vol)
        except Exception as e:
            return None
    
    # CHIA NHỎ THÀNH BATCH để tránh 429 Too Many Requests
    BATCH_SIZE = 50  # Quét 50 coins/lần
    BATCH_DELAY_MIN = 0.6  # Random delay 0.6-1.0s giữa các batch
    BATCH_DELAY_MAX = 1.0
    
    all_movers = []
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i+BATCH_SIZE]
        tasks = [get_single_mover(sym) for sym in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Lọc bỏ None và exceptions
        movers = [r for r in results if r is not None and not isinstance(r, Exception)]
        all_movers.extend(movers)
        
        # Random delay giữa các batch (trừ batch cuối)
        if i + BATCH_SIZE < len(symbols):
            import random
            delay = random.uniform(BATCH_DELAY_MIN, BATCH_DELAY_MAX)
            await asyncio.sleep(delay)
    
    return all_movers


async def timelist(update, context):
    """Lệnh xem lịch coin sẽ list trong 1 tuần - API Calendar"""
    await update.message.reply_text("⏳ Đang lấy lịch listing...")
    
    try:
        async with aiohttp.ClientSession() as session:
            # Gọi API calendar
            timestamp = int(datetime.now().timestamp() * 1000)
            url = f"https://www.mexc.co/api/operation/new_coin_calendar?timestamp={timestamp}"
            
            async with session.get(url, timeout=15) as r:
                if r.status != 200:
                    raise Exception(f"HTTP {r.status}")
                
                data = await r.json()
                coins = data.get('data', {}).get('newCoins', [])
                
                if not coins:
                    raise Exception("Không tìm thấy dữ liệu listing")
                
                vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
                now = datetime.now(vn_tz)
                one_week_later = now + timedelta(days=7)
                
                msg = "📅 *LỊCH COIN SẮP LIST (1 TUẦN)*\n\n"
                count = 0
                
                for coin in coins:
                    symbol = coin.get('vcoinName')
                    full_name = coin.get('vcoinNameFull', symbol)
                    timestamp_ms = coin.get('firstOpenTime')
                    
                    if not timestamp_ms:
                        continue
                    
                    # Convert timestamp to datetime
                    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=vn_tz)
                    
                    # Chỉ hiển thị coin list trong 1 tuần tới
                    if now <= dt <= one_week_later:
                        weekdays = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
                        weekday = weekdays[dt.weekday()]
                        date_str = dt.strftime("%d/%m/%Y %H:%M")
                        
                        msg += f"🆕 `{symbol}` ({full_name})\n"
                        msg += f"   ⏰ {weekday}, {date_str}\n\n"
                        count += 1
                
                if count == 0:
                    await update.message.reply_text("📅 Chưa có coin nào sắp list trong tuần tới")
                else:
                    await update.message.reply_text(msg, parse_mode="Markdown")
    
    except Exception as e:
        print(f"❌ Lỗi scrape Futures listing: {e}")
        await update.message.reply_text(
            "❌ Không thể lấy dữ liệu từ MEXC\n\n"
            "Vui lòng xem trực tiếp tại:\n"
            "🔗 https://www.mexc.co/vi-VN/announcements/new-listings",
            parse_mode="Markdown"
        )


async def coinlist(update, context):
    """Lệnh xem các coin đã list trong 1 tuần - API Calendar"""
    await update.message.reply_text("⏳ Đang lấy danh sách coin mới...")
    
    try:
        async with aiohttp.ClientSession() as session:
            # Gọi API calendar
            timestamp = int(datetime.now().timestamp() * 1000)
            url = f"https://www.mexc.co/api/operation/new_coin_calendar?timestamp={timestamp}"
            
            async with session.get(url, timeout=15) as r:
                if r.status != 200:
                    raise Exception(f"HTTP {r.status}")
                
                data = await r.json()
                coins = data.get('data', {}).get('newCoins', [])
                
                if not coins:
                    raise Exception("Không tìm thấy dữ liệu listing")
                
                vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
                now = datetime.now(vn_tz)
                one_week_ago = now - timedelta(days=7)
                
                msg = "📋 *COIN ĐÃ LIST (1 TUẦN QUA)*\n\n"
                count = 0
                
                for coin in coins:
                    symbol = coin.get('vcoinName')
                    full_name = coin.get('vcoinNameFull', symbol)
                    timestamp_ms = coin.get('firstOpenTime')
                    
                    if not timestamp_ms:
                        continue
                    
                    # Convert timestamp to datetime
                    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=vn_tz)
                    
                    # Chỉ hiển thị coin list trong 1 tuần qua
                    if one_week_ago <= dt <= now:
                        weekdays = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
                        weekday = weekdays[dt.weekday()]
                        date_str = dt.strftime("%d/%m/%Y %H:%M")
                        
                        msg += f"✅ `{symbol}` ({full_name})\n"
                        msg += f"   ⏰ {weekday}, {date_str}\n\n"
                        count += 1
                
                if count == 0:
                    await update.message.reply_text("📋 Không có coin nào list trong tuần qua")
                else:
                    await update.message.reply_text(msg, parse_mode="Markdown")
    
    except Exception as e:
        print(f"❌ Lỗi scrape Futures listing: {e}")
        await update.message.reply_text(
            "❌ Không thể lấy dữ liệu từ MEXC\n\n"
            "Vui lòng xem trực tiếp tại:\n"
            "🔗 https://www.mexc.co/vi-VN/announcements/new-listings",
            parse_mode="Markdown"
        )


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
        movers = await calc_movers(session, "Min1", ALL_SYMBOLS)
    
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
                await context.bot.send_message(
                    chat, 
                    text, 
                    parse_mode="Markdown",
                    disable_web_page_preview=True  # Tắt preview link
                )
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
    app.add_handler(CommandHandler("timelist", timelist))
    app.add_handler(CommandHandler("coinlist", coinlist))

    jq = app.job_queue
    
    # Lấy danh sách symbols và khởi động WebSocket
    async def init_websocket(context):
        global ALL_SYMBOLS
        async with aiohttp.ClientSession() as session:
            ALL_SYMBOLS = await get_all_symbols(session)
            print(f"✅ Tìm thấy {len(ALL_SYMBOLS)} coin")
        
        # Khởi động WebSocket stream
        asyncio.create_task(websocket_stream(context))
    
    # Chạy init ngay khi khởi động
    jq.run_once(init_websocket, 5)
    
    # Reset base prices mỗi 1 phút
    jq.run_repeating(reset_base_prices, 60, first=65)
    
    # Kiểm tra coin mới mỗi 5 phút
    jq.run_repeating(job_new_listing, 300, first=30)

    print("🔥 Bot quét MEXC Futures với WebSocket đang chạy...")
    print(f"📊 Ngưỡng pump: >= {PUMP_THRESHOLD}%")
    print(f"📊 Ngưỡng dump: <= {DUMP_THRESHOLD}%")
    print(f"💰 Volume tối thiểu: {MIN_VOL_THRESHOLD:,}")
    print("🌐 WebSocket: Realtime price streaming")
    app.run_polling()


if __name__ == "__main__":
    main()
