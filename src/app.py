import logging
import asyncio
import re
import os
import random
import time
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import Forbidden, TimedOut
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import hypercorn.asyncio
from hypercorn.config import Config
import psycopg2
from psycopg2.extras import DictCursor
import datetime

# --- Configuration ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "7846698102:AAFR2bhmjAkPiV-PjtnFIu_oRnzxYPP1xVo")
ADMIN_ID_STR = os.environ.get("ADMIN_ID", "7756130972")
try:
    ADMIN_ID = int(ADMIN_ID_STR)
except (ValueError, TypeError):
    logging.critical("Invalid or missing ADMIN_ID environment variable.")
    exit(1)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgresql_6nv7_user:EQCCcg1l73t8S2g9sfF2LPVx6aA5yZts@dpg-cvlq2pggjchc738o29r0-a.frankfurt-postgres.render.com/postgresql_6nv7")
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
]

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    handlers=[logging.StreamHandler()]
)
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
    retries = 3
    logger.info(f"Connecting to database: {DATABASE_URL.split('@')[-1]}")
    for i in range(retries):
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
            with conn.cursor() as cur:
                logger.warning("Dropping existing tables (ads, users, pending_listings)...")
                cur.execute("DROP TABLE IF EXISTS pending_listings CASCADE;")
                cur.execute("DROP TABLE IF EXISTS ads CASCADE;")
                cur.execute("DROP TABLE IF EXISTS users CASCADE;")

                logger.info("Creating 'users' table...")
                cur.execute("""
                    CREATE TABLE users (
                        id BIGINT PRIMARY KEY,
                        first_name TEXT,
                        last_name TEXT,
                        username TEXT UNIQUE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
                    );
                """)

                logger.info("Creating 'ads' table...")
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
                cur.execute("CREATE INDEX IF NOT EXISTS ads_city_idx ON ads (city);")
                cur.execute("CREATE INDEX IF NOT EXISTS ads_price_idx ON ads (price);")
                cur.execute("CREATE INDEX IF NOT EXISTS ads_rooms_idx ON ads (rooms);")
                cur.execute("CREATE INDEX IF NOT EXISTS ads_source_idx ON ads (source);")
                cur.execute("CREATE INDEX IF NOT EXISTS ads_created_at_idx ON ads (created_at DESC);")

                logger.info("Creating 'pending_listings' table...")
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
                return
        except psycopg2.OperationalError as e:
            logger.error(f"Attempt {i+1}/{retries}: Database connection error during init: {e}. Retrying in 5 seconds...")
            if conn: conn.rollback()
            time.sleep(5)
        except Exception as e:
            logger.exception(f"Attempt {i+1}/{retries}: Failed to initialize database: {e}")
            if conn: conn.rollback()
            time.sleep(5)
        finally:
            if conn: conn.close()
    else:
        logger.critical("Failed to initialize database after multiple retries.")
        raise ConnectionError("Could not initialize the database.")

# --- Flask Application Setup ---
app = Flask(__name__)

# --- Global variable for Telegram application ---
bot_application = None

# --- Kufar Parser with Selenium ---
class KufarParser:
    @staticmethod
    def fetch_ads(city: str, captcha_code: Optional[str] = None) -> tuple[List[Dict], bool]:
        """Fetch ads from Kufar using Selenium with CAPTCHA handling"""
        base_url = f"https://www.kufar.by/l/r~{city}/snyat/kvartiru-dolgosrochno?cur=USD"
        logger.info(f"Kufar Request URL: {base_url}")

        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")

        driver = None
        try:
            driver = webdriver.Chrome(options=chrome_options)
            driver.get(base_url)

            # Ожидаем загрузки списка объявлений
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-name='listing-item']"))
            )
            time.sleep(random.uniform(2, 4))  # Дополнительная задержка для полной загрузки

            # Проверяем наличие капчи
            if "captcha" in driver.page_source.lower() and not captcha_code:
                logger.info("CAPTCHA detected, requesting user input.")
                return [], True

            if captcha_code:
                try:
                    captcha_input = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.ID, "captcha_input"))
                    )
                    captcha_input.send_keys(captcha_code)
                    submit_button = driver.find_element(By.XPATH, "//button[@type='submit']")
                    submit_button.click()
                    time.sleep(2)
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-name='listing-item']"))
                    )
                except Exception as e:
                    logger.error(f"Error entering CAPTCHA: {e}")
                    return [], False

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            ads = []

            # Обновленные селекторы для Kufar (по состоянию на апрель 2025)
            for item in soup.select("article[data-name='listing-item']"):
                try:
                    link_tag = item.select_one("a[href*='/item/']")
                    if not link_tag:
                        continue
                    full_link = link_tag['href']

                    price_tag = item.select_one("span[data-name='price-usd']")
                    price = int(re.sub(r'\D', '', price_tag.text)) if price_tag else None

                    desc_tag = item.select_one("h3[data-name='title']")
                    description = desc_tag.text.strip() if desc_tag else "Нет описания"

                    address_tag = item.select_one("div[data-name='address']")
                    address = address_tag.text.strip() if address_tag else "Адрес не указан"

                    img_tag = item.select_one("img[data-name='image']")
                    image = (img_tag.get('src') or img_tag.get('data-src')) if img_tag else None

                    params_tag = item.select_one("div[data-name='parameters']")
                    params = KufarParser.parse_parameters(params_tag.text) if params_tag else {}

                    ads.append({
                        'link': full_link,
                        'source': 'Kufar',
                        'city': city,
                        'price': price,
                        'title': description.split(',')[0] if ',' in description else description,
                        'description': description,
                        'address': address,
                        'image': image,
                        'user_id': None,
                        **params
                    })
                except Exception as e:
                    logger.error(f"Error parsing ad: {e}")
                    continue

            logger.info(f"Parsed {len(ads)} ads from Kufar for {city}")
            return ads[:10], False
        except Exception as e:
            logger.error(f"Error fetching Kufar for {city}: {e}")
            return [], False
        finally:
            if driver:
                driver.quit()

    @staticmethod
    def parse_parameters(param_text: str) -> dict:
        """Parse apartment parameters from text"""
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

# --- Database Operations ---
def store_ads(ads: List[Dict]) -> int:
    if not ads: return 0
    added_count = 0
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        with conn.cursor() as cur:
            upsert_query = """
                INSERT INTO ads (link, source, city, price, rooms, area, floor, address, image, title, description, user_id, created_at, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (link) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    price = EXCLUDED.price,
                    rooms = EXCLUDED.rooms,
                    area = EXCLUDED.area,
                    floor = EXCLUDED.floor,
                    address = EXCLUDED.address,
                    image = EXCLUDED.image,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description
                RETURNING xmax;
            """
            for ad in ads:
                if not ad.get("link") or not ad.get("source"):
                    logger.warning(f"Skipping ad due to missing link or source: {ad.get('link', 'N/A')}")
                    continue

                values = (
                    ad.get("link"), ad.get("source"), ad.get("city"), ad.get("price"),
                    ad.get("rooms"), ad.get("area"), ad.get("floor"),
                    ad.get("address"), ad.get("image"), ad.get("title"), ad.get("description"), 
                    ad.get("user_id")
                )
                try:
                    cur.execute(upsert_query, values)
                    result = cur.fetchone()
                    if result and result[0] == 0:
                        added_count += 1
                except (psycopg2.Error, TypeError, ValueError) as insert_err:
                    logger.error(f"Error upserting ad {ad.get('link')}: {insert_err}. Values: {values}")
                    conn.rollback()
                else:
                    conn.commit()

        logger.info(f"DB Store: Processed {len(ads)} ads. Added {added_count} new.")
        return added_count
    except psycopg2.Error as e:
        logger.error(f"Database connection/operation error during store_ads: {e}")
        if conn: conn.rollback()
        return 0
    except Exception as e:
        logger.exception(f"Unexpected error in store_ads: {e}")
        if conn: conn.rollback()
        return 0
    finally:
        if conn: conn.close()

# --- Flask API Endpoints ---
@app.route('/api/search', methods=['POST'])
def search_api():
    data = request.json
    if not data or 'user_id' not in data or 'city' not in data:
        logger.warning("Missing user_id or city in /api/search request")
        return jsonify({"error": "Missing user_id or city"}), 400

    user_id = data['user_id']
    city = data['city']
    min_price = data.get('min_price', type=int)
    max_price = data.get('max_price', type=int)
    rooms = data.get('rooms')
    captcha_code = data.get('captcha_code')

    logger.info(f"Search request from user {user_id}: city={city}, min_price={min_price}, max_price={max_price}, rooms={rooms}, captcha={captcha_code is not None}")

    if city not in CITIES:
        return jsonify({"error": "Invalid city"}), 400

    ads, captcha_required = KufarParser.fetch_ads(city, captcha_code)
    if captcha_required:
        return jsonify({"error": "CAPTCHA_REQUIRED"}), 403

    if not ads:
        return jsonify({"error": "No ads found or parsing failed"}), 500

    filtered_ads = [ad for ad in ads if (
        (min_price is None or ad['price'] >= min_price) and
        (max_price is None or ad['price'] <= max_price) and
        (rooms is None or ad['rooms'] == rooms)
    )]

    store_ads(filtered_ads)

    response_data = []
    for ad in filtered_ads[:10]:
        ad['created_at'] = ad.get('created_at', datetime.datetime.now()).isoformat()
        ad['last_seen'] = ad.get('last_seen', datetime.datetime.now()).isoformat()
        response_data.append(ad)

    logger.info(f"Returning {len(response_data)} ads to user {user_id}")
    return jsonify({"ads": response_data})

@app.route('/api/register_user', methods=['POST'])
def register_user_api():
    data = request.json
    if not data or 'user_id' not in data:
        logger.warning("Received /api/register_user request with missing user_id")
        return jsonify({"error": "Missing user_id"}), 400

    user_id = data['user_id']
    first_name = data.get('first_name')
    last_name = data.get('last_name')
    username = data.get('username')
    logger.debug(f"Registering user: {user_id}, username: {username}")
    
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, first_name, last_name, username, created_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    username = EXCLUDED.username;
                """,
                (user_id, first_name, last_name, username)
            )
            conn.commit()
        logger.info(f"User registered/updated: {user_id} (username: {username})")
        return jsonify({"status": "success"})
    except psycopg2.Error as db_err:
        logger.error(f"DB error registering user {user_id}: {db_err}")
        if conn: conn.rollback()
        return jsonify({"error": "Database Error"}), 500
    except Exception as e:
        logger.exception(f"Unexpected error registering user {user_id}: {e}")
        if conn: conn.rollback()
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/add_listing', methods=['POST'])
async def add_listing_api():
    global bot_application
    conn = None
    try:
        user_id = request.form.get('user_id', type=int)
        title = request.form.get('title')
        price_str = request.form.get('price')
        rooms = request.form.get('rooms')
        city = request.form.get('city')

        if not all([user_id, title, price_str, rooms, city]):
            missing = [k for k, v in locals().items() if v is None and k in ['user_id', 'title', 'price_str', 'rooms', 'city']]
            logger.warning(f"Missing required fields for add_listing: {missing}")
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

        description = request.form.get('description', '')
        area_str = request.form.get('area')
        address = request.form.get('address', '')

        try:
            price = int(price_str)
            area = int(area_str) if area_str and area_str.isdigit() else None
            if price < 0 or (area is not None and area <= 0): raise ValueError("Invalid number")
        except (ValueError, TypeError):
            logger.warning(f"Invalid numeric value: price='{price_str}', area='{area_str}'")
            return jsonify({"error": "Invalid price or area value"}), 400

        uploaded_files = request.files.getlist('photos[]')
        image_filenames = ','.join(
            [f.filename for f in uploaded_files if f and f.filename]
        ) if uploaded_files else None
        logger.info(f"Received {len(uploaded_files)} file(s). Filenames: '{image_filenames}' for user {user_id}")

        listing_id = None
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_listings
                (user_id, title, description, price, rooms, area, city, address, image_filenames, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') RETURNING id
                """,
                (user_id, title, description, price, rooms, area, city, address, image_filenames)
            )
            result = cur.fetchone()
            if result: listing_id = result[0]
            else: raise Exception("Failed to retrieve listing ID.")
            conn.commit()
        logger.info(f"Pending listing {listing_id} created for user {user_id}")

        if listing_id and bot_application:
            try:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{listing_id}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"reject_{listing_id}")]
                ])
                message_text = (
                    f"Новое объявление #{listing_id} от пользователя {user_id}:\n"
                    f"🏠 {title}\n"
                    f"💰 ${price}/месяц\n"
                    f"🛋️ Комнаты: {rooms}\n"
                    f"📏 Площадь: {area or 'Не указано'} м²\n"
                    f"🌆 Город: {CITIES.get(city, city)}\n"
                    f"📍 Адрес: {address or 'Не указан'}\n"
                    f"📝 Описание: {description or 'Нет'}\n"
                    f"📸 Фото: {image_filenames or 'Нет'}"
                )
                await bot_application.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=message_text,
                    reply_markup=keyboard
                )
                logger.info(f"Sent moderation request for listing {listing_id} to admin {ADMIN_ID}")
            except Exception as e:
                logger.error(f"Failed to send moderation request for listing {listing_id}: {e}")

        return jsonify({"status": "success", "listing_id": listing_id})
    except psycopg2.Error as db_err:
        logger.error(f"DB error adding listing for user {user_id}: {db_err}")
        if conn: conn.rollback()
        return jsonify({"error": "Database Error"}), 500
    except Exception as e:
        logger.exception(f"Unexpected error in add_listing_api for user {user_id}: {e}")
        if conn: conn.rollback()
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/user_listings', methods=['GET'])
def user_listings_api():
    user_id = request.args.get('user_id', type=int)
    if not user_id:
        logger.warning("Missing user_id in /api/user_listings request")
        return jsonify({"error": "Missing user_id"}), 400

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT link, source, city, price, rooms, area, floor, address, image, title, description, created_at, last_seen
                FROM ads
                WHERE user_id = %s AND source = 'User'
                ORDER BY created_at DESC;
                """,
                (user_id,)
            )
            ads = cur.fetchall()
        logger.info(f"Retrieved {len(ads)} user listings for user {user_id}")
        return jsonify({"ads": [dict(ad) for ad in ads]})
    except psycopg2.Error as db_err:
        logger.error(f"DB error fetching user listings for {user_id}: {db_err}")
        return jsonify({"error": "Database Error"}), 500
    except Exception as e:
        logger.exception(f"Unexpected error in user_listings_api for {user_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/ads', methods=['GET'])
def ads_api():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT link, source, city, price, rooms, area, floor, address, image, title, description, user_id, created_at, last_seen
                FROM ads
                WHERE source = 'Kufar'
                ORDER BY created_at DESC
                LIMIT 10;
                """
            )
            ads = cur.fetchall()
        logger.info(f"Retrieved {len(ads)} popular ads")
        return jsonify({"ads": [dict(ad) for ad in ads]})
    except psycopg2.Error as db_err:
        logger.error(f"DB error fetching ads: {db_err}")
        return jsonify({"error": "Database Error"}), 500
    except Exception as e:
        logger.exception(f"Unexpected error in ads_api: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/new_listings', methods=['GET'])
def new_listings_api():
    user_id = request.args.get('user_id', type=int)
    if not user_id:
        logger.warning("Missing user_id in /api/new_listings request")
        return jsonify({"error": "Missing user_id"}), 400

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT link, source, city, price, rooms, area, floor, address, image, title, description, user_id, created_at, last_seen
                FROM ads
                WHERE source = 'Kufar' AND created_at > NOW() - INTERVAL '24 hours'
                ORDER BY created_at DESC
                LIMIT 10;
                """
            )
            ads = cur.fetchall()
        logger.info(f"Retrieved {len(ads)} new listings for user {user_id}")
        return jsonify({"ads": [dict(ad) for ad in ads]})
    except psycopg2.Error as db_err:
        logger.error(f"DB error fetching new listings for {user_id}: {db_err}")
        return jsonify({"error": "Database Error"}), 500
    except Exception as e:
        logger.exception(f"Unexpected error in new_listings_api for {user_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/')
@app.route('src/mini_app.html')
def serve_mini_app():
    return send_from_directory('.', 'mini_app.html')

# --- Telegram Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot")
    web_app_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'localhost:10000')}/mini_app.html"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть Поиск Квартир", web_app=web_app_url)]
    ])
    await update.message.reply_text(
        "Добро пожаловать в бот поиска квартир!\n"
        "Используйте кнопку ниже, чтобы открыть приложение:",
        reply_markup=keyboard
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.info(f"Callback query received: {data}")

    if data.startswith("approve_") or data.startswith("reject_"):
        if update.effective_user.id != ADMIN_ID:
            await query.edit_message_text("У вас нет прав для модерации.")
            return

        listing_id = int(data.split("_")[1])
        action = "approved" if data.startswith("approve_") else "rejected"
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(
                    "UPDATE pending_listings SET status = %s WHERE id = %s RETURNING *;",
                    (action, listing_id)
                )
                listing = cur.fetchone()
                if not listing:
                    await query.edit_message_text(f"Объявление #{listing_id} не найдено.")
                    return

                if action == "approved":
                    link = f"https://t.me/your_bot_name/listing_{listing_id}"
                    cur.execute(
                        """
                        INSERT INTO ads (link, source, city, price, rooms, area, address, image, title, description, user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """,
                        (link, 'User', listing['city'], listing['price'], listing['rooms'], listing['area'],
                         listing['address'], listing['image_filenames'], listing['title'], listing['description'],
                         listing['user_id'])
                    )
                conn.commit()

                status_text = "одобрено" if action == "approved" else "отклонено"
                await query.edit_message_text(f"Объявление #{listing_id} {status_text}.")
                await context.bot.send_message(
                    chat_id=listing['user_id'],
                    text=f"Ваше объявление '{listing['title']}' было {status_text} администратором."
                )
                logger.info(f"Listing {listing_id} {action} by admin {ADMIN_ID}")
        except psycopg2.Error as db_err:
            logger.error(f"DB error processing {action} for listing {listing_id}: {db_err}")
            if conn: conn.rollback()
            await query.edit_message_text("Ошибка базы данных при обработке объявления.")
        except Exception as e:
            logger.exception(f"Unexpected error processing {action} for listing {listing_id}: {e}")
            if conn: conn.rollback()
            await query.edit_message_text("Произошла ошибка при обработке объявления.")
        finally:
            if conn: conn.close()

# --- Telegram Bot Setup ---
async def setup_bot():
    global bot_application
    bot_application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    bot_application.add_handler(CommandHandler("start", start))
    bot_application.add_handler(CallbackQueryHandler(button_handler))

    await bot_application.bot.set_my_commands([
        BotCommand("start", "Запустить бота")
    ])
    logger.info("Telegram bot handlers set up.")

# --- Main Execution ---
async def main():
    init_db()
    await setup_bot()

    config = Config()
    config.bind = ["0.0.0.0:10000"]
    await hypercorn.asyncio.serve(app, config, shutdown_trigger=lambda: asyncio.Future())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"Fatal error in main: {e}")
        exit(1)
