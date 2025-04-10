import logging
import asyncio
import re
import urllib.parse
import os
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Forbidden, TimedOut
from bs4 import BeautifulSoup
import aiohttp
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
import time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import hypercorn.asyncio
from hypercorn.config import Config
import psycopg2
from psycopg2.extras import DictCursor

# --- Configuration ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "7846698102:AAFR2bhmjAkPiV-PjtnFIu_oRnzxYPP1xVo")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_MJr6nebWzp3C@ep-fragrant-math-a2ladk0z-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require")
BASE_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost:10000")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
REQUEST_TIMEOUT = 10
PARSE_INTERVAL = 30
KUFAR_LIMIT = 7
ONLINER_LIMIT = 7

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

ONLINER_CITY_URLS = {
    "minsk": "https://r.onliner.by/ak/#bounds[lb][lat]=53.820922446131&bounds[lb][long]=27.344970703125&bounds[rt][lat]=53.97547425743&bounds[rt][long]=27.77961730957",
    "brest": "https://r.onliner.by/ak/#bounds[lb][lat]=51.941725203142&bounds[lb][long]=23.492889404297&bounds[rt][lat]=52.234528294214&bounds[rt][long]=23.927536010742",
    "vitebsk": "https://r.onliner.by/ak/#bounds[lb][lat]=55.085834940707&bounds[lb][long]=29.979629516602&bounds[rt][lat]=55.357648391381&bounds[rt][long]=30.414276123047",
    "gomel": "https://r.onliner.by/ak/#bounds[lb][lat]=52.302600726968&bounds[lb][long]=30.732192993164&bounds[rt][lat]=52.593037841157&bounds[rt][long]=31.166839599609",
    "grodno": "https://r.onliner.by/ak/#bounds[lb][lat]=53.538267122397&bounds[lb][long]=23.629531860352&bounds[rt][lat]=53.820517109806&bounds[rt][long]=24.064178466797",
    "mogilev": "https://r.onliner.by/ak/#bounds[lb][lat]=53.74261986683&bounds[lb][long]=30.132064819336&bounds[rt][lat]=54.023503252809&bounds[rt][long]=30.566711425781",
}

# --- Database Initialization ---
def init_db():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS ads CASCADE;")
            cur.execute("""
                CREATE TABLE ads (
                    link TEXT PRIMARY KEY,
                    source TEXT NOT NULL CHECK (source IN ('Kufar', 'Onliner')),
                    city TEXT,
                    price INTEGER CHECK (price >= 0),
                    rooms TEXT,
                    area INTEGER,
                    floor TEXT,
                    address TEXT,
                    image TEXT,
                    title TEXT,
                    description TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    last_seen TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
                );
            """)
            cur.execute("CREATE INDEX ads_city_idx ON ads (city);")
            cur.execute("CREATE INDEX ads_price_idx ON ads (price);")
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
                                    'floor': params['floor']
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

# --- Onliner Parser ---
class OnlinerParser:
    @staticmethod
    def fetch_ads(city: str, min_price: Optional[int] = None, max_price: Optional[int] = None, rooms: Optional[str] = None) -> List[Dict]:
        base_url = ONLINER_CITY_URLS.get(city)
        if not base_url:
            logger.error(f"Unknown city for Onliner: {city}")
            return []

        query_params = {"currency": "usd"}
        if rooms and rooms != "studio":
            query_params["rent_type[]"] = f"{rooms}_room{'s' if int(rooms) > 1 else ''}"
        elif rooms == "studio":
            query_params["rent_type[]"] = "1_room"
            query_params["only_owner"] = "true"  # Фильтр для студий
        if min_price and max_price:
            query_params["price[min]"] = min_price
            query_params["price[max]"] = max_price
        
        url = base_url if not query_params else f"https://r.onliner.by/ak/?{urllib.parse.urlencode(query_params)}#{base_url.split('#')[1]}"
        logger.info(f"Fetching Onliner ads from: {url}")

        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument(f"user-agent={USER_AGENT}")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

        try:
            driver.get(url)
            time.sleep(5)
            soup = BeautifulSoup(driver.page_source, "html.parser")
            ads = soup.select("a[href*='/ak/apartments/']")[:ONLINER_LIMIT]
            results = []
            for ad in ads:
                try:
                    link = ad.get("href", "")
                    if not link.startswith("https://r.onliner.by/ak/apartments/"):
                        continue
                    price = OnlinerParser._parse_price(ad)
                    room_count = OnlinerParser._parse_rooms(ad)
                    address = OnlinerParser._parse_address(ad)
                    image = OnlinerParser._parse_image(ad)
                    description = OnlinerParser._parse_description(ad)

                    if OnlinerParser._check_filters(price, room_count, min_price, max_price, rooms):
                        results.append({
                            "link": link,
                            "source": "Onliner",
                            "city": city,
                            "price": price,
                            "rooms": room_count,
                            "address": address,
                            "image": image,
                            "title": description.split(',')[0] if ',' in description else description,
                            "description": description,
                            "area": None,
                            "floor": None
                        })
                except Exception as e:
                    logger.error(f"Error parsing Onliner ad: {e}")
            return results
        except Exception as e:
            logger.error(f"Error fetching Onliner for {city}: {e}")
            return []
        finally:
            driver.quit()

    @staticmethod
    def _parse_price(ad) -> Optional[int]:
        price_element = ad.select_one(".classified__price-value span[data-bind*='formatPrice']")
        return int(re.sub(r"[^\d]", "", price_element.text)) if price_element else None

    @staticmethod
    def _parse_rooms(ad) -> Optional[str]:
        rooms_element = ad.select_one(".classified__caption-item.classified__caption-item_type")
        if rooms_element:
            if "студия" in rooms_element.text.lower():
                return "studio"
            match = re.search(r"(\d+)к", rooms_element.text)
            return match.group(1) if match else None
        return None

    @staticmethod
    def _parse_address(ad) -> str:
        address_element = ad.select_one(".classified__caption-item.classified__caption-item_adress")
        return address_element.text.strip() if address_element else "Адрес не указан"

    @staticmethod
    def _parse_image(ad) -> Optional[str]:
        image_element = ad.select_one(".classified__figure img")
        return image_element.get("src") if image_element else None

    @staticmethod
    def _parse_description(ad) -> str:
        desc_element = ad.select_one(".classified__caption-item.classified__caption-item_type")
        return desc_element.text.strip() if desc_element else "Описание не указано"

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
                    INSERT INTO ads (link, source, city, price, rooms, area, floor, address, image, title, description)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (link) DO UPDATE SET
                        last_seen = CURRENT_TIMESTAMP,
                        price = EXCLUDED.price,
                        description = EXCLUDED.description
                    RETURNING xmax;
                """, (
                    ad.get("link"), ad.get("source"), ad.get("city"), ad.get("price"),
                    ad.get("rooms"), ad.get("area"), ad.get("floor"), ad.get("address"),
                    ad.get("image"), ad.get("title"), ad.get("description")
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
        return jsonify({"error": "Missing user_id or city"}), 400

    kufar_ads = await KufarParser.fetch_ads(
        data['city'],
        data.get('min_price'),
        data.get('max_price'),
        data.get('rooms')
    )
    onliner_ads = await asyncio.to_thread(
        OnlinerParser.fetch_ads,
        data['city'],
        data.get('min_price'),
        data.get('max_price'),
        data.get('rooms')
    )
    ads = kufar_ads + onliner_ads
    store_ads(ads)
    return jsonify({"ads": ads[:10]})

@app.route('/api/ads', methods=['GET'])
def ads_api():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT * FROM ads ORDER BY created_at DESC LIMIT 10")
            ads = [dict(ad) for ad in cur.fetchall()]
        return jsonify({"ads": ads})
    except Exception as e:
        logger.error(f"Error fetching ads: {e}")
        return jsonify({"error": "Database Error"}), 500
    finally:
        if conn: conn.close()

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

# --- Telegram Bot Setup ---
bot_application = None

async def setup_bot():
    global bot_application
    bot_application = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_application.add_handler(CommandHandler("start", start))
    await bot_application.bot.set_my_commands([BotCommand("start", "Запустить бота")])
    logger.info("Telegram bot handlers set up.")

# --- Background Parsing ---
async def fetch_and_store_ads():
    for city in CITIES.keys():
        logger.info(f"Starting parsing for city: {city}")
        kufar_ads = await KufarParser.fetch_ads(city)
        onliner_ads = await asyncio.to_thread(OnlinerParser.fetch_ads, city)
        total_ads = kufar_ads + onliner_ads
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
