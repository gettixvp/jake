import logging
import asyncio
import re
import urllib.parse
import os
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import Forbidden, TimedOut
from bs4 import BeautifulSoup
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import hypercorn.asyncio
from hypercorn.config import Config
import psycopg2
from psycopg2.extras import DictCursor

# --- Configuration ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "7846698102:AAFR2bhmjAkPiV-PjtnFIu_oRnzxYPP1xVo")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 7756130972))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgresql_6nv7_user:EQCCcg1l73t8S2g9sfF2LPVx6aA5yZts@dpg-cvlq2pggjchc738o29r0-a.frankfurt-postgres.render.com/postgresql_6nv7")
BASE_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost:10000")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
REQUEST_TIMEOUT = 10
PARSE_INTERVAL = 30
KUFAR_LIMIT = 7

# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Constants ---
CITIES = {
    "minsk": "🏙️ Минск",
    "brest": "🌇 Брест",
    "grodno": "🌃 Гродно",
    "gomel": "🌆 Гомель",
    "vitebsk": "🏙 Витебск",
    "mogilev": "🏞️ Могилев",
}

# --- Database Initialization ---
def init_db():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS ads CASCADE;")
            cur.execute("DROP TABLE IF EXISTS users CASCADE;")
            cur.execute("DROP TABLE IF EXISTS pending_listings CASCADE;")

            cur.execute("""
                CREATE TABLE users (
                    id BIGINT PRIMARY KEY,
                    first_name TEXT,
                    last_name TEXT,
                    username TEXT UNIQUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
                );
            """)

            cur.execute("""
                CREATE TABLE ads (
                    link TEXT PRIMARY KEY,
                    source TEXT NOT NULL CHECK (source IN ('Kufar', 'User')),
                    city TEXT,
                    price INTEGER CHECK (price >= 0),
                    rooms TEXT,
                    area INTEGER,
                    floor TEXT,
                    address TEXT,
                    image TEXT,
                    title TEXT,
                    description TEXT,
                    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    last_seen TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
                );
            """)
            cur.execute("CREATE INDEX ads_city_idx ON ads (city);")
            cur.execute("CREATE INDEX ads_price_idx ON ads (price);")

            cur.execute("""
                CREATE TABLE pending_listings (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    description TEXT,
                    price INTEGER NOT NULL CHECK (price >= 0),
                    rooms TEXT NOT NULL,
                    area INTEGER CHECK (area > 0),
                    city TEXT NOT NULL,
                    address TEXT,
                    image_filenames TEXT,
                    submitted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'pending' NOT NULL CHECK (status IN ('pending', 'approved', 'rejected'))
                );
            """)
            conn.commit()
            logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

init_db()

# --- Flask Application Setup ---
app = Flask(__name__, static_folder='.', static_url_path='')

# --- Kufar Parser ---
class KufarParser:
    @staticmethod
    async def fetch_ads(city: str, min_price: Optional[int] = None, max_price: Optional[int] = None, rooms: Optional[str] = None) -> List[Dict]:
        headers = {"User-Agent": USER_AGENT}
        url = f"https://www.kufar.by/l/r~{city}/snyat/kvartiru-dolgosrochno?cur=USD"
        if rooms and rooms != "studio":
            url += f"&r={rooms}"
        if min_price and max_price:
            url += f"&prc=r%3A{min_price}%2C{max_price}"
        logger.info(f"Fetching Kufar ads from: {url}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as response:
                    response.raise_for_status()
                    soup = BeautifulSoup(await response.text(), "html.parser")
                    ads = []
                    for item in soup.select("article[data-name='listing-item']")[:KUFAR_LIMIT]:
                        try:
                            link_tag = item.select_one("a[href*='/item/']")
                            if not link_tag: continue
                            full_link = link_tag['href']
                            price_tag = item.select_one("span[data-name='price-usd']")
                            price = int(re.sub(r'\D', '', price_tag.text)) if price_tag else None
                            desc_tag = item.select_one("h3[data-name='title']")
                            description = desc_tag.text.strip() if desc_tag else "Нет описания"
                            address_tag = item.select_one("div[data-name='address']")
                            address = address_tag.text.strip() if address_tag else "Адрес не указан"
                            img_tag = item.select_one("img[data-name='image']")
                            image = (img_tag.get('src') or img_tag.get('data-src')) if img_tag else None
                            params = KufarParser.parse_parameters(item.select_one("div[data-name='parameters']").text if item.select_one("div[data-name='parameters']") else "")

                            if KufarParser._check_filters(price, params['rooms'], min_price, max_price, rooms):
                                ads.append({
                                    'link': full_link,
                                    'source': 'Kufar',
                                    'city': city,
                                    'price': price,
                                    'title': description.split(',')[0] if ',' in description else description,
                                    'description': description,
                                    'address': address,
                                    'image': image,
                                    'rooms': params['rooms'],
                                    'area': params['area'],
                                    'floor': params['floor'],
                                    'user_id': None
                                })
                        except Exception as e:
                            logger.error(f"Error parsing Kufar ad: {e}")
                    return ads
            except Exception as e:
                logger.error(f"Error fetching Kufar for {city}: {e}")
                return []

    @staticmethod
    def parse_parameters(param_text: str) -> dict:
        params = {'rooms': None, 'area': None, 'floor': None}
        text = param_text.lower()
        if 'студия' in text:
            params['rooms'] = 'studio'
        else:
            rooms_match = re.search(r'(\d+)\s*комн', text)
            if rooms_match:
                rooms = int(rooms_match.group(1))
                params['rooms'] = f"{rooms}" if rooms < 4 else "4+"
        area_match = re.search(r'(\d+)\s*м²?', text)
        if area_match:
            params['area'] = int(area_match.group(1))
        floor_match = re.search(r'этаж\s*(\d+)\s*из\s*(\d+)', text)
        if floor_match:
            params['floor'] = f"{floor_match.group(1)}/{floor_match.group(2)}"
        return params

    @staticmethod
    def _check_filters(price: Optional[int], rooms: Optional[str], min_price: Optional[int], max_price: Optional[int], target_rooms: Optional[str]) -> bool:
        if price is None:
            return False
        price_valid = (min_price is None or price >= min_price) and (max_price is None or price <= max_price)
        rooms_valid = target_rooms is None or rooms == target_rooms
        return price_valid and rooms_valid

# --- Database Operations ---
def store_ads(ads: List[Dict]) -> int:
    if not ads: return 0
    added_count = 0
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            for ad in ads:
                cur.execute("""
                    INSERT INTO ads (link, source, city, price, rooms, area, floor, address, image, title, description, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (link) DO UPDATE SET
                        last_seen = CURRENT_TIMESTAMP,
                        price = EXCLUDED.price,
                        description = EXCLUDED.description
                    RETURNING xmax;
                """, (
                    ad.get("link"), ad.get("source"), ad.get("city"), ad.get("price"),
                    ad.get("rooms"), ad.get("area"), ad.get("floor"), ad.get("address"),
                    ad.get("image"), ad.get("title"), ad.get("description"), ad.get("user_id")
                ))
                if cur.fetchone()[0] == 0: added_count += 1
            conn.commit()
        return added_count
    except Exception as e:
        logger.error(f"Error storing ads: {e}")
        if conn: conn.rollback()
        return 0
    finally:
        if conn: conn.close()

# --- Flask API Endpoints ---
@app.route('/api/search', methods=['POST'])
async def search_api():
    data = request.json
    if not data or 'user_id' not in data or 'city' not in data:
        logger.error("Missing user_id or city in request")
        return jsonify({"error": "Missing user_id or city"}), 400

    try:
        kufar_ads = await KufarParser.fetch_ads(
            data['city'],
            data.get('min_price'),
            data.get('max_price'),
            data.get('rooms')
        )
        logger.info(f"Fetched {len(kufar_ads)} Kufar ads")
    except Exception as e:
        logger.error(f"Error fetching Kufar ads: {e}")
        return jsonify({"error": "Failed to fetch Kufar ads"}), 500

    ads = kufar_ads
    try:
        store_ads(ads)
        logger.info(f"Stored {len(ads)} ads")
    except Exception as e:
        logger.error(f"Error storing ads: {e}")
        return jsonify({"error": "Failed to store ads"}), 500

    return jsonify({"ads": ads[:10]})

@app.route('/api/register_user', methods=['POST'])
def register_user_api():
    data = request.json
    if not data or 'user_id' not in data:
        return jsonify({"error": "Missing user_id"}), 400

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (id, first_name, last_name, username)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    username = EXCLUDED.username;
            """, (data['user_id'], data.get('first_name'), data.get('last_name'), data.get('username')))
            conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Error registering user: {e}")
        if conn: conn.rollback()
        return jsonify({"error": "Database Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/add_listing', methods=['POST'])
async def add_listing_api():
    global bot_application
    conn = None
    try:
        form = request.form
        files = request.files.getlist('photos[]')
        if not all([form.get('user_id'), form.get('title'), form.get('price'), form.get('rooms'), form.get('city')]):
            return jsonify({"error": "Missing required fields"}), 400

        price = int(form['price'])
        area = int(form['area']) if form.get('area') and form['area'].isdigit() else None
        image_filenames = ','.join([f.filename for f in files if f]) if files else None

        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pending_listings (user_id, title, description, price, rooms, area, city, address, image_filenames)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (form['user_id'], form['title'], form.get('description'), price, form['rooms'], area, form['city'], form.get('address'), image_filenames))
            listing_id = cur.fetchone()[0]
            conn.commit()

        if bot_application:
            await bot_application.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"Новое объявление #{listing_id}:\n🏠 {form['title']}\n💰 ${price}\n🌆 {CITIES.get(form['city'], form['city'])}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{listing_id}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"reject_{listing_id}")]
                ])
            )
        return jsonify({"status": "success", "listing_id": listing_id})
    except Exception as e:
        logger.error(f"Error adding listing: {e}")
        if conn: conn.rollback()
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/user_listings', methods=['GET'])
def user_listings_api():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT * FROM ads WHERE user_id = %s AND source = 'User' ORDER BY created_at DESC", (user_id,))
            ads = [dict(ad) for ad in cur.fetchall()]
        return jsonify({"ads": ads})
    except Exception as e:
        logger.error(f"Error fetching user listings: {e}")
        return jsonify({"error": "Database Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/ads', methods=['GET'])
def ads_api():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=Dict wCursor) as cur:
            cur.execute("SELECT * FROM ads WHERE source = 'Kufar' ORDER BY created_at DESC LIMIT 10")
            ads = [dict(ad) for ad in cur.fetchall()]
        return jsonify({"ads": ads})
    except Exception as e:
        logger.error(f"Error fetching ads: {e}")
        return jsonify({"error": "Database Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/new_listings', methods=['GET'])
def new_listings_api():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT * FROM ads WHERE source = 'Kufar' AND created_at > NOW() - INTERVAL '24 hours'
                ORDER BY created_at DESC LIMIT 10
            """)
            ads = [dict(ad) for ad in cur.fetchall()]
        return jsonify({"ads": ads})
    except Exception as e:
        logger.error(f"Error fetching new listings: {e}")
        return jsonify({"error": "Database Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/')
@app.route('/mini_app.html')
def serve_mini_app():
    try:
        return send_from_directory('.', 'mini_app.html')
    except FileNotFoundError:
        logger.error("mini_app.html not found")
        return "Mini App HTML not found", 500

# --- Telegram Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    web_app_url = f"https://{BASE_URL}/mini_app.html"
    await update.message.reply_text(
        "Добро пожаловать в бот поиска квартир!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Открыть Поиск Квартир", web_app=web_app_url)]
        ])
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("approve_") or data.startswith("reject_"):
        if update.effective_user.id != ADMIN_ID:
            await query.edit_message_text("У вас нет прав для модерации.")
            return

        listing_id = int(data.split("_")[1])
        action = "approved" if data.startswith("approve_") else "rejected"
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("UPDATE pending_listings SET status = %s WHERE id = %s RETURNING *", (action, listing_id))
                listing = cur.fetchone()
                if not listing:
                    await query.edit_message_text(f"Объявление #{listing_id} не найдено.")
                    return
                if action == "approved":
                    link = f"https://{BASE_URL}/listing_{listing_id}"
                    cur.execute("""
                        INSERT INTO ads (link, source, city, price, rooms, area, address, image, title, description, user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (link, 'User', listing['city'], listing['price'], listing['rooms'], listing['area'],
                          listing['address'], listing['image_filenames'], listing['title'], listing['description'], listing['user_id']))
                conn.commit()
                await query.edit_message_text(f"Объявление #{listing_id} {'одобрено' if action == 'approved' else 'отклонено'}.")
                await context.bot.send_message(
                    chat_id=listing['user_id'],
                    text=f"Ваше объявление '{listing['title']}' было {'одобрено' if action == 'approved' else 'отклонено'}."
                )
        except Exception as e:
            logger.error(f"Error processing listing {listing_id}: {e}")
            if conn: conn.rollback()
            await query.edit_message_text("Ошибка при обработке объявления.")
        finally:
            if conn: conn.close()

# --- Telegram Bot Setup ---
bot_application = None

async def setup_bot():
    global bot_application
    bot_application = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_application.add_handler(CommandHandler("start", start))
    bot_application.add_handler(CallbackQueryHandler(button_handler))
    await bot_application.bot.set_my_commands([BotCommand("start", "Запустить бота")])
    logger.info("Telegram bot handlers set up.")

# --- Background Parsing ---
async def fetch_and_store_ads():
    for city in CITIES.keys():
        logger.info(f"Starting parsing for city: {city}")
        kufar_ads = await KufarParser.fetch_ads(city)
        total_ads = kufar_ads
        if total_ads:
            store_ads(total_ads)
            logger.info(f"Stored {len(total_ads)} ads for {city}")

# --- Main Execution ---
async def main():
    init_db()
    await setup_bot()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(fetch_and_store_ads, 'interval', minutes=PARSE_INTERVAL)
    scheduler.start()
    await fetch_and_store_ads()
    config = Config()
    config.bind = ["0.0.0.0:10000"]
    await hypercorn.asyncio.serve(app, config)

if __name__ == "__main__":
    asyncio.run(main())
