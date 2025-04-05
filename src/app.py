import logging
import asyncio
import re
import urllib.parse
import os
import datetime
import random
import time
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import Forbidden, TimedOut
from bs4 import BeautifulSoup
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import hypercorn.asyncio
from hypercorn.config import Config
import psycopg2
from psycopg2.extras import DictCursor

# --- Configuration ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "7846698102:AAFR2bhmjAkPiV-PjtnFIu_oRnzxYPP1xVo")
ADMIN_ID_STR = os.environ.get("ADMIN_ID", "7756130972")
try:
    ADMIN_ID = int(ADMIN_ID_STR)
except (ValueError, TypeError):
    logging.critical("Invalid or missing ADMIN_ID environment variable.")
    exit(1)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgresql_6nv7_user:EQCCcg1l73t8S2g9sfF2LPVx6aA5yZts@dpg-cvlq2pggjchc738o29r0-a.frankfurt-postgres.render.com/postgresql_6nv7")

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)
logging.getLogger('apscheduler.executors').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('urllib3.connectionpool').setLevel(logging.INFO)

# --- Constants ---
CITIES = {
    "minsk": "🏙️ Минск",
    "brest": "🌇 Брест",
    "grodno": "🌃 Гродно",
    "gomel": "🌆 Гомель",
    "vitebsk": "🏙 Витебск",
    "mogilev": "🏞️ Могилев",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
]

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
        logger.critical("Failed to initialize database after multiple retries. Check connection string and DB status.")
        raise ConnectionError("Could not initialize the database.")

# --- Flask Application Setup ---
app = Flask(__name__)

# --- Глобальная переменная для приложения Telegram ---
bot_application = None

# --- Improved Kufar Parser ---
class KufarParser:
    @staticmethod
    def parse_parameters(param_text: str) -> dict:
        """Парсит строку параметров квартиры"""
        params = {
            'rooms': None,
            'area': None,
            'floor': None
        }
        
        text = param_text.lower()
        
        # Парсинг комнат
        if 'студия' in text:
            params['rooms'] = 'studio'
        else:
            rooms_match = re.search(r'(\d+)\s*комн', text)
            if rooms_match:
                rooms = int(rooms_match.group(1))
                params['rooms'] = f"{rooms}" if rooms < 4 else "4+"
        
        # Парсинг площади
        area_match = re.search(r'(\d+)\s*м²?', text)
        if area_match:
            params['area'] = int(area_match.group(1))
        
        # Парсинг этажа
        floor_match = re.search(r'этаж\s*(\d+)\s*из\s*(\d+)', text)
        if floor_match:
            params['floor'] = f"{floor_match.group(1)}/{floor_match.group(2)}"
        
        return params

    @staticmethod
    async def fetch_ads(city: str) -> List[Dict]:
        """Основной метод парсинга объявлений"""
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://www.kufar.by/",
            "DNT": "1"
        }
        
        base_url = f"https://www.kufar.by/l/r~{city}/snyat/kvartiru-dolgosrochno"
        logger.info(f"Kufar Request URL: {base_url}")
        
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                await asyncio.sleep(random.uniform(5, 15))
                
                async with session.get(base_url, timeout=30) as response:
                    if response.status == 429:
                        logger.error(f"Rate limited for {city}, status: {response.status}")
                        return []
                    
                    html = await response.text()
                    
                    if "captcha" in html.lower():
                        logger.error("Kufar CAPTCHA detected!")
                        return []
                    
                    soup = BeautifulSoup(html, 'html.parser')
                    ads = []
                    
                    for item in soup.select('article.styles_wrapper__Q06m9'):
                        try:
                            # Ссылка на объявление
                            link_tag = item.select_one('a[href^="/l/"]')
                            if not link_tag:
                                continue
                                
                            full_link = f"https://www.kufar.by{link_tag['href']}"
                            
                            # Цена
                            price_tag = item.select_one('span.styles_price__usd__HpXMa')
                            price = int(re.sub(r'\D', '', price_tag.text)) if price_tag else None
                            
                            # Описание
                            desc_tag = item.select_one('h3.styles_body__5BrnC.styles_body__r33c8')
                            description = desc_tag.text.strip() if desc_tag else "Нет описания"
                            
                            # Адрес
                            address_tag = item.select_one('p.styles_address__l6Qe_')
                            address = address_tag.text.strip() if address_tag else "Адрес не указан"
                            
                            # Изображение
                            img_tag = item.select_one('img.styles_image__7aRPM')
                            image = (img_tag.get('src') or img_tag.get('data-src')) if img_tag else None
                            
                            # Параметры (комнаты, площадь, этаж)
                            params_tag = item.select_one('p.styles_parameters__7zKlL')
                            params = KufarParser.parse_parameters(params_tag.text) if params_tag else {}
                            
                            ads.append({
                                'link': full_link,
                                'source': 'Kufar',
                                'city': city,
                                'price': price,
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
                    return ads
                    
        except Exception as e:
            logger.error(f"Error fetching Kufar for {city}: {e}")
            return []

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
                INSERT INTO ads (link, source, city, price, rooms, area, floor, address, image, description, user_id, created_at, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (link) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    price = EXCLUDED.price,
                    rooms = EXCLUDED.rooms,
                    area = EXCLUDED.area,
                    floor = EXCLUDED.floor,
                    address = EXCLUDED.address,
                    image = EXCLUDED.image,
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
                    ad.get("address"), ad.get("image"), ad.get("description"), 
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

# --- Background Parsing Task ---
async def fetch_and_store_all_ads():
    logger.info("--- Starting Periodic Ad Fetching Task ---")
    start_time = time.time()
    total_new_ads = 0

    # Запускаем парсинг для каждого города последовательно с задержками
    for city in CITIES.keys():
        try:
            city_ads = await KufarParser.fetch_ads(city)
            if city_ads:
                new_ads = store_ads(city_ads)
                total_new_ads += new_ads
            
            # Пауза между городами
            await asyncio.sleep(random.uniform(10, 20))
            
        except Exception as e:
            logger.error(f"Error processing city {city}: {e}")
            continue

    end_time = time.time()
    logger.info(f"--- Finished Periodic Ad Fetching Task ---")
    logger.info(f"Total New Ads Found: {total_new_ads}")
    logger.info(f"Duration: {end_time - start_time:.2f} seconds")

# ... (остальной код остается без изменений, включая Flask endpoints, Telegram Bot класс и main функцию)

# --- Main Application Logic ---
async def shutdown_application(application: Application, scheduler: AsyncIOScheduler):
    logger.info("Initiating shutdown...")
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
    
    await application.stop()
    logger.info("Application polling stopped.")
    
    try:
        await application.updater.stop()
        logger.info("Updater stopped successfully.")
    except Exception as e:
        logger.error(f"Error stopping updater: {e}")
    
    await application.shutdown()
    logger.info("Application shutdown complete.")

async def main():
    global bot_application
    logger.info("--- Application Starting ---")

    try:
        init_db()
    except ConnectionError:
        logger.critical("Stopping application due to DB initialization failure.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_instance = ApartmentBot(application)
    bot_application = application
    await bot_instance.setup_commands()

    scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
    initial_run_time = datetime.datetime.now() + datetime.timedelta(seconds=15)
    scheduler.add_job(
        fetch_and_store_all_ads,
        trigger=IntervalTrigger(minutes=30, start_date=initial_run_time),
        id='ad_parser_job',
        name='Fetch and Store Ads',
        replace_existing=True
    )
    scheduler.start()
    logger.info(f"Scheduler started. First run at ~{initial_run_time.strftime('%H:%M:%S')}, then every 30 min.")

    config = Config()
    port = int(os.environ.get("PORT", "10000"))
    config.bind = [f"0.0.0.0:{port}"]
    config.use_reloader = bool(os.environ.get("DEBUG"))
    config.accesslog = logger
    config.errorlog = logger
    logger.info(f"Hypercorn configured for 0.0.0.0:{port}")

    logger.info("Starting Telegram bot polling and Hypercorn server...")

    async with application:
        await application.initialize()
        await application.start()
        
        polling_task = asyncio.create_task(
            application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
        )
        
        hypercorn_task = asyncio.create_task(
            hypercorn.asyncio.serve(app, config)
        )

        try:
            done, pending = await asyncio.wait(
                [polling_task, hypercorn_task],
                return_when=asyncio.FIRST_EXCEPTION
            )
            
            for task in done:
                if task.exception():
                    logger.error(f"Task failed: {task.exception()}")
                    
        except Exception as e:
            logger.error(f"Main loop error: {e}")
        finally:
            if not polling_task.done():
                polling_task.cancel()
            if not hypercorn_task.done():
                hypercorn_task.cancel()
            
            await shutdown_application(application, scheduler)

    logger.info("--- Application Shutdown Complete ---")

if __name__ == "__main__":
    pid_file = "/tmp/apartment_bot.pid"
    if os.path.exists(pid_file):
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)
            logger.critical(f"Another instance is already running with PID {pid}. Exiting.")
            exit(1)
        except OSError:
            logger.info("Stale PID file found. Removing and proceeding.")
            os.remove(pid_file)

    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Application stopped manually (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Application crashed: {e}", exc_info=True)
    finally:
        if os.path.exists(pid_file):
            os.remove(pid_file)
        if not loop.is_closed():
            loop.close()
