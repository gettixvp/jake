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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
DATABASE_URL = os.environ.get("DATABASE_URL")
BASE_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost:10000")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
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
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS pending_listings CASCADE;")
            cur.execute("DROP TABLE IF EXISTS ads CASCADE;")
            cur.execute("DROP TABLE IF EXISTS users CASCADE;")

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
            cur.execute("CREATE INDEX ads_rooms_idx ON ads (rooms);")

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

# --- Flask Application Setup ---
app = Flask(__name__, static_folder='.', static_url_path='')

# --- Global variable for Telegram application ---
bot_application = None

# --- Kufar Parser with Selenium ---
class KufarParser:
    @staticmethod
    def fetch_ads(city: str, captcha_code: Optional[str] = None) -> tuple[List[Dict], bool]:
        base_url = f"https://www.kufar.by/l/r~{city}/snyat/kvartiru-dolgosrochno?cur=USD"
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")

        driver = None
        try:
            driver = webdriver.Chrome(options=chrome_options)
            driver.get(base_url)
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-name='listing-item']"))
            )
            time.sleep(random.uniform(1, 3))

            if "captcha" in driver.page_source.lower() and not captcha_code:
                return [], True

            if captcha_code:
                try:
                    captcha_input = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.ID, "captcha_input"))
                    )
                    captcha_input.send_keys(captcha_code)
                    driver.find_element(By.XPATH, "//button[@type='submit']").click()
                    time.sleep(2)
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-name='listing-item']"))
                    )
                except Exception as e:
                    logger.error(f"Error entering CAPTCHA: {e}")
                    return [], False

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            ads = []
            for item in soup.select("article[data-name='listing-item']"):
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
            return ads[:10], False
        except Exception as e:
            logger.error(f"Error fetching Kufar for {city}: {e}")
            return [], False
        finally:
            if driver: driver.quit()

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
def search_api():
    data = request.json
    if not data or 'user_id' not in data or 'city' not in data:
        return jsonify({"error": "Missing user_id or city"}), 400

    ads, captcha_required = KufarParser.fetch_ads(data['city'], data.get('captcha_code'))
    if captcha_required:
        return jsonify({"error": "CAPTCHA_REQUIRED"}), 403
    if not ads:
        return jsonify({"ads": []}), 200

    filtered_ads = [
        ad for ad in ads
        if (data.get('min_price') is None or ad['price'] >= data['min_price']) and
           (data.get('max_price') is None or ad['price'] <= data['max_price']) and
           (data.get('rooms') is None or ad['rooms'] == data['rooms'])
    ]
    store_ads(filtered_ads)
    return jsonify({"ads": filtered_ads[:10]})

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
        with conn.cursor(cursor_factory=DictCursor) as cur:
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

# Исправленные маршруты для Render
@app.route('/')
@app.route('/mini_app.html')
def serve_mini_app():
    return send_from_directory('.', 'mini_app.html')

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
async def setup_bot():
    global bot_application
    bot_application = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_application.add_handler(CommandHandler("start", start))
    bot_application.add_handler(CallbackQueryHandler(button_handler))
    await bot_application.bot.set_my_commands([BotCommand("start", "Запустить бота")])
    logger.info("Telegram bot handlers set up.")

# --- Main Execution ---
async def main():
    init_db()
    await setup_bot()
    config = Config()
    config.bind = ["0.0.0.0:10000"]
    await hypercorn.asyncio.serve(app, config)

if __name__ == "__main__":
    asyncio.run(main())
