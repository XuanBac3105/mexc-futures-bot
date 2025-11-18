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
PUMP_THRESHOLD = 2.5    # Tăng >= 2.5%
DUMP_THRESHOLD = -2.5   # Giảm >= 2.5%

# Volume tối thiểu để tránh coin ít thanh khoản
MIN_VOL_THRESHOLD = 100000

SUBSCRIBERS = set()
ALERT_MODE = {}  # {chat_id: mode} - 1: tất cả, 2: chỉ biến động mạnh ≥3%
MUTED_COINS = {}  # {chat_id: set(symbols)} - danh sách coin bị mute
KNOWN_SYMBOLS = set()  # Danh sách coin đã biết
ALL_SYMBOLS = []  # Cache danh sách coin

# WebSocket price tracking
LAST_PRICES = {}  # {symbol: {"price": float, "time": datetime}}
BASE_PRICES = {}  # {symbol: base_price} - Dynamic reset: chỉ reset sau khi alert
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
    """Format báo động pump/dump với 3 mức độ"""
    color = "🟢" if change_pct >= 0 else "🔴"
    
    # Phân loại 3 mức độ biến động
    abs_change = abs(change_pct)
    
    if abs_change >= 4.0:
        # Mức 3: BIẾN ĐỘNG CỰC MẠNH >= 4%
        icon = "🔥🚀🔥🚀🔥" if change_pct >= 0 else "🔥💥🔥💥🔥"
        highlight = "⚠️ BIẾN ĐỘNG CỰC MẠNH ⚠️\n"
    elif abs_change >= 3.0:
        # Mức 2: BIẾN ĐỘNG MẠNH 3-3.9%
        icon = "⚡🚀⚡🚀⚡" if change_pct >= 0 else "⚡💥⚡💥⚡"
        highlight = "🔔 BIẾN ĐỘNG MẠNH 🔔\n"
    else:
        # Mức 1: Thông thường 2.3-2.9%
        icon = "🚀🚀🚀" if change_pct >= 0 else "💥💥💥"
        highlight = ""
    
    # Lấy tên coin (bỏ _USDT)
    coin_name = symbol.replace("_USDT", "")
    
    # Link đơn giản hơn để MEXC app dễ detect
    link = f"https://www.mexc.co/futures/{symbol}"
    
    return (
        f"{highlight}"
        f"┌{icon} [{coin_name}]({link}) ⚡ {change_pct:+.2f}% {color}\n"
        f"└ {old_price:.6g} → {new_price:.6g}"
    )


# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    SUBSCRIBERS.add(chat_id)
    if chat_id not in ALERT_MODE:
        ALERT_MODE[chat_id] = 1  # Mặc định: tất cả
    
    current_mode = ALERT_MODE.get(chat_id, 1)
    mode_text = "Tất cả (≥2.5%)" if current_mode == 1 else "Chỉ biến động mạnh (≥3%)"
    
    await update.message.reply_text(
        "🤖 Bot Quét MEXC Futures !\n\n"
        "✅ Nhận giá REALTIME từ server\n"
        "✅ Báo NGAY LẬP TỨC khi ≥±2.5%\n"
        "✅ Dynamic base price - không miss pump/dump\n\n"
        f"📊 Chế độ hiện tại: {mode_text}\n\n"
        "Các lệnh:\n"
        "/subscribe – bật báo động\n"
        "/unsubscribe – tắt báo động\n"
        "/mode1 – báo tất cả (≥2.5%)\n"
        "/mode2 – chỉ báo biến động mạnh (≥3%)\n"
        "/mute COIN – tắt thông báo coin\n"
        "/unmute COIN – bật lại thông báo coin\n"
        "/mutelist – xem danh sách coin đã mute\n"
        "/timelist – lịch coin sắp list\n"
        "/coinlist – coin vừa list gần đây"
    )


async def subscribe(update, context):
    SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text("Đã bật báo!")


async def unsubscribe(update, context):
    SUBSCRIBERS.discard(update.effective_chat.id)
    await update.message.reply_text("Đã tắt báo!")


async def mode1(update, context):
    chat_id = update.effective_chat.id
    ALERT_MODE[chat_id] = 1
    await update.message.reply_text(
        "✅ Đã chuyển sang Mode 1\n\n"
        "📊 Báo TẤT CẢ biến động ≥±2.5%:\n"
        "  🚀 Thông thường (2.5-2.9%)\n"
        "  ⚡ Biến động mạnh (3-3.9%)\n"
        "  🔥 Biến động cực mạnh (≥4%)"
    )


async def mode2(update, context):
    chat_id = update.effective_chat.id
    ALERT_MODE[chat_id] = 2
    await update.message.reply_text(
        "✅ Đã chuyển sang Mode 2\n\n"
        "📊 CHỈ báo biến động mạnh ≥±3%:\n"
        "  ⚡ Biến động mạnh (3-3.9%)\n"
        "  🔥 Biến động cực mạnh (≥4%)"
    )


async def mute_coin(update, context):
    chat_id = update.effective_chat.id
    
    if not context.args:
        await update.message.reply_text(
            "❌ Vui lòng nhập tên coin\n\n"
            "Ví dụ: /mute XION hoặc /mute xion"
        )
        return
    
    coin = context.args[0].upper().strip()  # Tự động chuyển thành chữ hoa
    symbol = f"{coin}_USDT" if not coin.endswith("_USDT") else coin
    
    if chat_id not in MUTED_COINS:
        MUTED_COINS[chat_id] = set()
    
    MUTED_COINS[chat_id].add(symbol)
    await update.message.reply_text(f"🔇 Đã tắt thông báo cho `{coin}`", parse_mode="Markdown")


async def unmute_coin(update, context):
    chat_id = update.effective_chat.id
    
    if not context.args:
        await update.message.reply_text(
            "❌ Vui lòng nhập tên coin\n\n"
            "Ví dụ: /unmute XION hoặc /unmute xion"
        )
        return
    
    coin = context.args[0].upper().strip()  # Tự động chuyển thành chữ hoa
    symbol = f"{coin}_USDT" if not coin.endswith("_USDT") else coin
    
    if chat_id in MUTED_COINS and symbol in MUTED_COINS[chat_id]:
        MUTED_COINS[chat_id].remove(symbol)
        await update.message.reply_text(f"🔔 Đã bật lại thông báo cho `{coin}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ `{coin}` chưa bị mute", parse_mode="Markdown")


async def mutelist(update, context):
    chat_id = update.effective_chat.id
    
    if chat_id not in MUTED_COINS or not MUTED_COINS[chat_id]:
        await update.message.reply_text("ℹ️ Chưa có coin nào bị mute")
        return
    
    coins = [sym.replace("_USDT", "") for sym in MUTED_COINS[chat_id]]
    msg = "🔇 *DANH SÁCH COIN ĐÃ MUTE*\n\n"
    msg += "\n".join([f"• `{coin}`" for coin in sorted(coins)])
    msg += f"\n\n_Tổng: {len(coins)} coin_"
    
    await update.message.reply_text(msg, parse_mode="Markdown")


async def websocket_stream(context):
    """WebSocket stream để nhận giá realtime từ MEXC Futures"""
    reconnect_delay = 5
    subscribed_symbols = set()  # Track những coin đã subscribe
    
    while True:
        try:
            # Tăng timeout và thêm ping interval
            async with websockets.connect(
                WEBSOCKET_URL,
                ping_interval=20,  # Ping server mỗi 20s để giữ kết nối
                ping_timeout=10,   # Timeout cho pong response
                close_timeout=10
            ) as ws:
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
                    subscribed_symbols.add(symbol)
                    await asyncio.sleep(0.005)  # 5ms delay giữa subscriptions
                
                print(f"✅ Đã subscribe {len(ALL_SYMBOLS)} coin qua WebSocket")
                
                # Reset reconnect delay sau khi connect thành công
                reconnect_delay = 5
                
                # Lắng nghe messages
                async for message in ws:
                    try:
                        data = json.loads(message)
                        
                        # Xử lý ping/pong
                        if "ping" in data:
                            await ws.send(json.dumps({"pong": data["ping"]}))
                            continue
                        
                        # Kiểm tra coin mới và subscribe (mỗi 30s kiểm tra 1 lần)
                        if not hasattr(websocket_stream, 'last_check'):
                            websocket_stream.last_check = datetime.now()
                        
                        if (datetime.now() - websocket_stream.last_check).seconds >= 30:
                            new_symbols = set(ALL_SYMBOLS) - subscribed_symbols
                            if new_symbols:
                                print(f"🔄 Phát hiện {len(new_symbols)} coin mới, đang subscribe...")
                                for symbol in new_symbols:
                                    sub_msg = {
                                        "method": "sub.ticker",
                                        "param": {"symbol": symbol}
                                    }
                                    await ws.send(json.dumps(sub_msg))
                                    subscribed_symbols.add(symbol)
                                    await asyncio.sleep(0.005)
                                print(f"✅ Đã subscribe {len(new_symbols)} coin mới")
                            websocket_stream.last_check = datetime.now()
                        
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
            print(f"🔄 Reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            
            # Exponential backoff: 5s -> 10s -> 20s -> max 60s
            reconnect_delay = min(reconnect_delay * 2, 60)


async def process_ticker(ticker_data, context):
    """Xử lý ticker data từ WebSocket và phát hiện pump/dump - DUAL BASE PRICE"""
    symbol = ticker_data.get("symbol")
    if not symbol:
        return
    
    try:
        current_price = float(ticker_data.get("lastPrice", 0))
        volume = float(ticker_data.get("volume24", 0))
        
        if current_price == 0 or volume < MIN_VOL_THRESHOLD:
            return
        
        now = datetime.now()
        current_second = now.second
        
        # Lưu giá hiện tại
        LAST_PRICES[symbol] = {
            "price": current_price,
            "time": now
        }
        
        # Thiết lập base price nếu chưa có
        if symbol not in BASE_PRICES:
            BASE_PRICES[symbol] = current_price
            return
        
        # Tính % thay đổi từ BASE_PRICE (dynamic - chỉ reset sau alert)
        base_price = BASE_PRICES[symbol]
        price_change = (current_price - base_price) / base_price * 100
        
        # Kiểm tra ngưỡng và cooldown
        should_alert = False
        
        if (price_change >= PUMP_THRESHOLD or price_change <= DUMP_THRESHOLD) and current_second > 2:
            # Kiểm tra cooldown (5s) để tránh spam
            last_alert = ALERTED_SYMBOLS.get(symbol)
            if not last_alert or (now - last_alert).total_seconds() > 5.0:
                should_alert = True
                ALERTED_SYMBOLS[symbol] = now

        if should_alert and SUBSCRIBERS:
            # Dùng BASE_PRICE và hiển thị % thay đổi
            msg = fmt_alert(symbol, base_price, current_price, price_change)

            if price_change >= PUMP_THRESHOLD:
                print(f"🚀 PUMP: {symbol} +{price_change:.2f}%")
            else:
                print(f"💥 DUMP: {symbol} {price_change:.2f}%")

            # Reset base price SAU KHI alert (dynamic reset)
            BASE_PRICES[symbol] = current_price

            # Gửi alert theo ALERT_MODE của từng user
            tasks = []
            for chat in SUBSCRIBERS:
                # Kiểm tra coin có bị mute không
                if chat in MUTED_COINS and symbol in MUTED_COINS[chat]:
                    continue
                
                mode = ALERT_MODE.get(chat, 1)  # Mặc định mode 1

                # Mode 1: Báo tất cả >= 2.5%
                # Mode 2: Chỉ báo biến động mạnh ≥3%
                if mode == 2 and abs(price_change) < 3.0:
                    continue

                tasks.append(
                    context.bot.send_message(
                        chat,
                        msg,
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                )

            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception as e:
                    print(f"❌ Lỗi gửi tin nhắn: {e}")
            
    except Exception as e:
        print(f"❌ Error processing ticker for {symbol}: {e}")


async def reset_base_prices(context):
    """Job backup reset base prices mỗi 5 phút"""
    global BASE_PRICES
    
    # Cập nhật base prices từ last prices (chỉ cho coin không có alert gần đây)
    for symbol, data in LAST_PRICES.items():
        # Chỉ reset nếu không có alert trong 5 phút qua
        if symbol not in ALERTED_SYMBOLS or \
           (datetime.now() - ALERTED_SYMBOLS[symbol]).seconds > 300:
            BASE_PRICES[symbol] = data["price"]
    
    print(f"🔄 Backup reset {len(BASE_PRICES)} base prices")


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
    
    global KNOWN_SYMBOLS, ALL_SYMBOLS
    
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
        
        # CẬP NHẬT ALL_SYMBOLS để WebSocket subscribe coin mới
        ALL_SYMBOLS = symbols
        print(f"🔄 Đã cập nhật danh sách: {len(ALL_SYMBOLS)} coin (thêm {len(new_coins)} coin mới)")
        
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
        BotCommand("mode1", "Báo tất cả (≥2.5%)"),
        BotCommand("mode2", "Chỉ báo biến động mạnh (≥3%)"),
        BotCommand("mute", "Tắt thông báo coin (ví dụ: /mute XION)"),
        BotCommand("unmute", "Bật lại thông báo coin"),
        BotCommand("mutelist", "Xem danh sách coin đã mute"),
        BotCommand("timelist", "Lịch coin sắp list trong 1 tuần"),
        BotCommand("coinlist", "Coin đã list trong 1 tuần qua"),
    ]
    
    # Retry logic cho set_my_commands (tránh timeout khi khởi động)
    for attempt in range(3):
        try:
            await app.bot.set_my_commands(commands)
            print("✅ Đã thiết lập menu lệnh bot")
            break
        except Exception as e:
            print(f"⚠️ Lỗi set commands (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2)
            else:
                print("⚠️ Skip set commands, bot vẫn hoạt động bình thường")


def main():
    # Tăng timeout cho Telegram API (Railway có thể chậm)
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,  # Tăng từ mặc định 5s
        read_timeout=30.0,     # Tăng từ mặc định 5s
        write_timeout=30.0,
        pool_timeout=30.0
    )
    
    app = ApplicationBuilder().token(BOT_TOKEN).request(request).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("mode1", mode1))
    app.add_handler(CommandHandler("mode2", mode2))
    app.add_handler(CommandHandler("mute", mute_coin))
    app.add_handler(CommandHandler("unmute", unmute_coin))
    app.add_handler(CommandHandler("mutelist", mutelist))
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
    
    # Backup reset base prices mỗi 5 phút (dynamic reset là chính)
    jq.run_repeating(reset_base_prices, 300, first=305)
    
    # Kiểm tra coin mới mỗi 5 phút
    jq.run_repeating(job_new_listing, 300, first=30)

    print("🔥 Bot quét MEXC Futures...")
    print(f"📊 Ngưỡng pump: >= {PUMP_THRESHOLD}%")
    print(f"📊 Ngưỡng dump: <= {DUMP_THRESHOLD}%")
    print(f"💰 Volume tối thiểu: {MIN_VOL_THRESHOLD:,}")
    print("🌐 WebSocket: Realtime price streaming")
    
    # Chạy với graceful shutdown
    try:
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        print("🛑 Bot đang tắt...")
    except Exception as e:
        print(f"❌ Bot error: {e}")


if __name__ == "__main__":
    main()