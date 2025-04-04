import logging
import asyncio
import re
import urllib.parse
import os
import datetime
import random
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
import threading

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
]
REQUEST_TIMEOUT = 20
PARSE_INTERVAL = 30
KUFAR_LIMIT = 7
ONLINER_LIMIT = 7

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

ONLINER_CITY_URLS = {
    "minsk": "#bounds[lb][lat]=53.820922446131&bounds[lb][long]=27.344970703125&bounds[rt][lat]=53.97547425743&bounds[rt][long]=27.77961730957",
    "brest": "#bounds[lb][lat]=51.941725203142&bounds[lb][long]=23.492889404297&bounds[rt][lat]=52.234528294214&bounds[rt][long]=23.927536010742",
    "vitebsk": "#bounds[lb][lat]=55.085834940707&bounds[lb][long]=29.979629516602&bounds[rt][lat]=55.357648391381&bounds[rt][long]=30.414276123047",
    "gomel": "#bounds[lb][lat]=52.302600726968&bounds[lb][long]=30.732192993164&bounds[rt][lat]=52.593037841157&bounds[rt][long]=31.166839599609",
    "grodno": "#bounds[lb][lat]=53.538267122397&bounds[lb][long]=23.629531860352&bounds[rt][lat]=53.820517109806&bounds[rt][long]=24.064178466797",
    "mogilev": "#bounds[lb][lat]=53.74261986683&bounds[lb][long]=30.132064819336&bounds[rt][lat]=54.023503252809&bounds[rt][long]=30.566711425781",
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
                        source TEXT NOT NULL CHECK (source IN ('Kufar', 'Onliner', 'User')),
                        city TEXT,
                        price INTEGER CHECK (price >= 0),
                        rooms TEXT,
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

# --- Parsers ---
class ApartmentParser:
    @staticmethod
    async def fetch_ads(city: str, min_price: Optional[int] = None, max_price: Optional[int] = None, rooms_filter: Optional[str] = None) -> List[Dict]:
        user_agent = random.choice(USER_AGENTS)
        headers = {
            "User-Agent": user_agent,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        results = []
        base_url = f"https://re.kufar.by/l/{city}/snyat/kvartiru-dolgosrochno"

        url_parts = [base_url]
        if rooms_filter and rooms_filter.isdigit():
            url_parts.append(f"/{rooms_filter}k")

        query_params = {"cur": "USD", "sort": "date_dsc"}
        if min_price is not None or max_price is not None:
            min_p = str(min_price) if min_price is not None else ''
            max_p = str(max_price) if max_price is not None else ''
            query_params["prc"] = f"r:{min_p},{max_p}"

        full_url = f"{'/'.join(url_parts)}?{urllib.parse.urlencode(query_params, safe=':,')}"
        logger.info(f"Kufar Request URL: {full_url}")

        await asyncio.sleep(random.uniform(1, 3))

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as response:
                    logger.info(f"Kufar response status: {response.status} for {city}")
                    response.raise_for_status()
                    html = await response.text()
                    soup = BeautifulSoup(html, "html.parser")
                    ad_elements = soup.select("section a[data-testid^='listing-item-']")

                    if not ad_elements:
                        logger.warning(f"No ads found with selector 'section a[data-testid^=listing-item-]' on Kufar for {city}.")
                        if "captcha" in html.lower():
                            logger.error("Kufar CAPTCHA detected.")
                            with open(f"kufar_captcha_{city}.html", "w", encoding="utf-8") as f:
                                f.write(html)
                        return []

                    logger.info(f"Found {len(ad_elements)} potential ads on Kufar for {city}.")
                    for ad_element in ad_elements:
                        try:
                            link = ad_element.get("href")
                            if not link or not link.startswith('/l/'): continue

                            full_link = f"https://re.kufar.by{link}"
                            price = ApartmentParser._parse_price(ad_element)
                            rooms_str, area = ApartmentParser._parse_rooms_area(ad_element)

                            if not ApartmentParser._check_room_filter(rooms_str, rooms_filter):
                                continue

                            results.append({
                                "link": full_link,
                                "source": "Kufar",
                                "city": city,
                                "price": price,
                                "rooms": rooms_str,
                                "address": ApartmentParser._parse_address(ad_element),
                                "image": ApartmentParser._parse_image(ad_element),
                                "description": ApartmentParser._parse_description(ad_element),
                                "user_id": None
                            })
                        except Exception as parse_err:
                            logger.warning(f"Could not parse Kufar ad item ({link}): {parse_err}")
        except aiohttp.ClientResponseError as e:
            logger.error(f"HTTP error fetching Kufar for {city}: {e.status} {e.message}")
        except asyncio.TimeoutError:
            logger.error(f"Timeout error fetching Kufar for {city}")
        except Exception as e:
            logger.exception(f"Unexpected error fetching/parsing Kufar for {city}: {e}")

        logger.info(f"Parsed {len(results)} ads from Kufar for {city}.")
        return results[:KUFAR_LIMIT]  # Ограничиваем количество

    @staticmethod
    def _parse_price(ad) -> Optional[int]:
        try:
            price_div = ad.find("div", string=re.compile(r'\$\s*per month'))
            if price_div:
                price_span = price_div.find("span")
                if price_span:
                    price_text = price_span.text.strip()
                    return int(re.sub(r"[^\d]", "", price_text))
            price_element = ad.select_one("span[class*='price']")
            if price_element and '$' in price_element.text:
                price_text = price_element.text.strip()
                return int(re.sub(r"[^\d]", "", price_text))
        except (AttributeError, ValueError, TypeError) as e:
            logger.warning(f"Could not parse Kufar price: {e}")
        return None

    @staticmethod
    def _parse_rooms_area(ad) -> (Optional[str], Optional[float]):
        rooms_str = None
        area = None
        try:
            params_div = ad.select_one("div[class*='parameters']")
            if params_div:
                text = params_div.text.strip().replace('\xa0', ' ')
                rooms_match = re.search(r"(\d+)\s*(?:комнат|комн\.?)", text, re.IGNORECASE)
                studio_match = re.search(r"Студия", text, re.IGNORECASE)
                area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*м²", text)

                if studio_match: rooms_str = "studio"
                elif rooms_match:
                    num = int(rooms_match.group(1))
                    rooms_str = "4+" if num >= 4 else str(num)

                if area_match:
                    area = float(area_match.group(1).replace(',', '.'))
        except Exception as e:
            logger.warning(f"Could not parse Kufar rooms/area: {e}")
        return rooms_str, area

    @staticmethod
    def _parse_address(ad) -> str:
        try:
            address_div = ad.select_one("div[class*='address']")
            if address_div:
                return address_div.text.strip()
        except AttributeError:
            pass
        return "Адрес не указан"

    @staticmethod
    def _parse_image(ad) -> Optional[str]:
        try:
            img = ad.select_one("img[data-testid='image']")
            if img:
                src = img.get('data-src') or img.get('src')
                if src and src.startswith('//'): return f"https:{src}"
                return src
        except AttributeError:
            pass
        return None

    @staticmethod
    def _parse_description(ad) -> str:
        try:
            title_h3 = ad.select_one("h3[class*='title']")
            if title_h3:
                return title_h3.text.strip()
        except AttributeError:
            pass
        return "Описание не указано"

    @staticmethod
    def _check_room_filter(rooms_str: Optional[str], target_rooms: Optional[str]) -> bool:
        if target_rooms is None: return True
        if rooms_str is None: return False
        if target_rooms == 'studio': return rooms_str == 'studio'
        elif target_rooms == '4+': return rooms_str == '4+' or (rooms_str.isdigit() and int(rooms_str) >= 4)
        elif target_rooms.isdigit(): return rooms_str == target_rooms
        else: return False

class OnlinerParser:
    @staticmethod
    async def fetch_ads(city: str, min_price: Optional[int] = None, max_price: Optional[int] = None, rooms_filter: Optional[str] = None) -> List[Dict]:
        user_agent = random.choice(USER_AGENTS)
        headers = {
            "User-Agent": user_agent,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        results = []
        base_url = "https://r.onliner.by/ak/"
        fragment = ONLINER_CITY_URLS.get(city)
        if not fragment or '#' not in fragment:
            logger.error(f"Invalid Onliner URL fragment for city: {city}")
            return []

        query_params = {}
        if rooms_filter:
            if rooms_filter.isdigit(): query_params["rent_type[]"] = f"{rooms_filter}_room"
            elif rooms_filter == 'studio': query_params["rent_type[]"] = "studio"

        if min_price is not None: query_params["price[min]"] = min_price
        if max_price is not None: query_params["price[max]"] = max_price
        if min_price is not None or max_price is not None: query_params["currency"] = "usd"

        query_string = urllib.parse.urlencode(query_params, doseq=True)
        full_url = f"{base_url}?{query_string}{fragment}" if query_string else f"{base_url}{fragment}"
        logger.info(f"Onliner Request URL: {full_url}")

        await asyncio.sleep(random.uniform(1, 3))

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as response:
                    logger.info(f"Onliner response status: {response.status} for {city}")
                    response.raise_for_status()
                    html = await response.text()
                    soup = BeautifulSoup(html, "html.parser")
                    ad_elements = soup.select("div.classifieds__item")

                    if not ad_elements:
                        logger.warning(f"No ads found with selector 'div.classifieds__item' on Onliner for {city}.")
                        if "Произошла ошибка" in html:
                            logger.error("Onliner page indicates an error occurred.")
                        return []

                    logger.info(f"Found {len(ad_elements)} potential ads on Onliner for {city}.")
                    for ad_element in ad_elements:
                        try:
                            link_tag = ad_element.select_one("a.classified__handle")
                            link = link_tag['href'] if link_tag else None
                            if not link or not link.startswith('https://r.onliner.by/ak/apartments/'): continue

                            price = OnlinerParser._parse_price(ad_element)
                            rooms_str, area = OnlinerParser._parse_rooms_area(ad_element)

                            if not OnlinerParser._check_room_filter(rooms_str, rooms_filter): continue

                            results.append({
                                "link": link,
                                "source": "Onliner",
                                "city": city,
                                "price": price,
                                "rooms": rooms_str,
                                "address": OnlinerParser._parse_address(ad_element),
                                "image": OnlinerParser._parse_image(ad_element),
                                "description": OnlinerParser._parse_description(ad_element, rooms_str, area),
                                "user_id": None
                            })
                        except Exception as parse_err:
                            logger.warning(f"Could not parse Onliner ad item ({link}): {parse_err}")
        except aiohttp.ClientResponseError as e:
            logger.error(f"HTTP error fetching Onliner for {city}: {e.status} {e.message}")
        except asyncio.TimeoutError:
            logger.error(f"Timeout error fetching Onliner for {city}")
        except Exception as e:
            logger.exception(f"Unexpected error fetching/parsing Onliner for {city}: {e}")

        logger.info(f"Parsed {len(results)} ads from Onliner for {city}.")
        return results[:ONLINER_LIMIT]  # Ограничиваем количество

    @staticmethod
    def _parse_price(ad) -> Optional[int]:
        try:
            price_span = ad.select_one(".classified__price-value span[data-bind*='usd']")
            if price_span:
                price_text = price_span.text.strip()
                return int(re.sub(r"[^\d]", "", price_text))
        except (AttributeError, ValueError, TypeError) as e:
            logger.warning(f"Could not parse Onliner price: {e}")
        return None

    @staticmethod
    def _parse_rooms_area(ad) -> (Optional[str], Optional[float]):
        rooms_str, area = None, None
        try:
            type_element = ad.select_one(".classified__caption-item_type")
            area_element = ad.select_one(".classified__caption-item_area")

            if type_element:
                text = type_element.text.strip()
                rooms_match = re.search(r"(\d+)-комн", text)
                studio_match = re.search(r"Студия", text, re.IGNORECASE)
                if studio_match: rooms_str = "studio"
                elif rooms_match:
                    num = int(rooms_match.group(1))
                    rooms_str = "4+" if num >= 4 else str(num)

                area_match_type = re.search(r"(\d+(?:[.,]\d+)?)\s*м²", text)
                if area_match_type:
                    area = float(area_match_type.group(1).replace(',', '.'))

            if area_element:
                area_match_dedicated = re.search(r"(\d+(?:[.,]\d+)?)\s*м²", area_element.text)
                if area_match_dedicated:
                    try:
                        area = float(area_match_dedicated.group(1).replace(',', '.'))
                    except ValueError: pass
        except Exception as e:
            logger.warning(f"Could not parse Onliner rooms/area: {e}")
        return rooms_str, area

    @staticmethod
    def _parse_address(ad) -> str:
        try:
            addr_el = ad.select_one(".classified__caption-item_address")
            if addr_el: return addr_el.text.strip()
        except AttributeError: pass
        return "Адрес не указан"

    @staticmethod
    def _parse_image(ad) -> Optional[str]:
        try:
            img = ad.select_one(".classified__figure img")
            if img:
                src = img.get("data-src") or img.get("src")
                if src and src.startswith('//'): return f"https:{src}"
                return src
        except AttributeError: pass
        return None

    @staticmethod
    def _parse_description(ad, rooms_str: Optional[str], area: Optional[float]) -> str:
        parts = []
        if rooms_str:
            if rooms_str == "studio": parts.append("Студия")
            elif rooms_str == "4+": parts.append("4+ комн.")
            else: parts.append(f"{rooms_str} комн.")
        if area: parts.append(f"{area:.1f}".replace('.0','') + " м²")

        try:
            title = ad.select_one(".classified__title")
            if title and title.text.strip(): parts.append(title.text.strip())
        except AttributeError: pass

        return ", ".join(parts) if parts else "Описание не указано"

    @staticmethod
    def _check_room_filter(rooms_str: Optional[str], target_rooms: Optional[str]) -> bool:
        if target_rooms is None: return True
        if rooms_str is None: return False
        if target_rooms == 'studio': return rooms_str == 'studio'
        elif target_rooms == '4+': return rooms_str == '4+' or (rooms_str.isdigit() and int(rooms_str) >= 4)
        elif target_rooms.isdigit(): return rooms_str == target_rooms
        else: return False

# --- Database Operations ---
def store_ads(ads: List[Dict]):
    if not ads: return 0
    added_count = 0
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        with conn.cursor() as cur:
            upsert_query = """
                INSERT INTO ads (link, source, city, price, rooms, address, image, description, user_id, created_at, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (link) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    price = EXCLUDED.price,
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
                    ad.get("rooms"), ad.get("address"), ad.get("image"),
                    ad.get("description"), ad.get("user_id")
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

    tasks = []
    for city in CITIES.keys():
        tasks.append(ApartmentParser.fetch_ads(city))
        tasks.append(OnlinerParser.fetch_ads(city))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_fetched_ads = []
    for i, result in enumerate(results):
        source = "Kufar" if i % 2 == 0 else "Onliner"
        city_index = i // 2
        city = list(CITIES.keys())[city_index]

        if isinstance(result, Exception):
            logger.error(f"Error fetching from {source} for {city}: {result}")
        elif isinstance(result, list):
            logger.info(f"Fetched {len(result)} ads from {source} for {city}.")
            all_fetched_ads.extend(result)
        else:
            logger.warning(f"Unexpected result type from {source} for {city}: {type(result)}")

    if all_fetched_ads:
        total_new_ads = store_ads(all_fetched_ads)
    else:
        logger.info("No ads fetched from any source in this cycle.")

    end_time = time.time()
    logger.info(f"--- Finished Periodic Ad Fetching Task ---")
    logger.info(f"Total New Ads Found: {total_new_ads}")
    logger.info(f"Duration: {end_time - start_time:.2f} seconds")

# --- Flask API Endpoints ---
@app.route('/api/ads', methods=['GET'])
def get_ads_api():
    city = request.args.get('city')
    min_price_str = request.args.get('min_price')
    max_price_str = request.args.get('max_price')
    rooms = request.args.get('rooms')
    kufar_offset = request.args.get('kufar_offset', default=0, type=int)
    onliner_offset = request.args.get('onliner_offset', default=0, type=int)
    user_offset = request.args.get('user_offset', default=0, type=int)

    min_price = int(min_price_str) if min_price_str and min_price_str.isdigit() else None
    max_price = int(max_price_str) if max_price_str and max_price_str.isdigit() else None

    logger.info(f"API Request /api/ads: city={city}, min_p={min_price}, max_p={max_price}, rooms={rooms}, k_off={kufar_offset}, o_off={onliner_offset}, u_off={user_offset}")
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            query = "SELECT * FROM ads WHERE 1=1"
            params = []

            if city:
                query += " AND city = %s"
                params.append(city)
            if min_price is not None:
                query += " AND price >= %s"
                params.append(min_price)
            if max_price is not None:
                query += " AND price <= %s"
                params.append(max_price)
            if rooms:
                if rooms == '4+':
                    query += " AND (rooms = '4+' OR (rooms ~ E'^\\\\d+$' AND CAST(rooms AS INTEGER) >= 4))"
                elif rooms == 'studio':
                    query += " AND rooms = 'studio'"
                elif rooms.isdigit():
                    query += " AND rooms = %s"
                    params.append(rooms)

            query += " ORDER BY created_at DESC"

            cur.execute(query, tuple(params))
            all_ads_dicts = [dict(row) for row in cur.fetchall()]
            logger.info(f"DB Query found {len(all_ads_dicts)} total ads matching filters.")

            kufar_ads = [ad for ad in all_ads_dicts if ad["source"] == "Kufar"]
            onliner_ads = [ad for ad in all_ads_dicts if ad["source"] == "Onliner"]
            user_ads = [ad for ad in all_ads_dicts if ad["source"] == "User"]

            kufar_limit = KUFAR_LIMIT
            onliner_limit = ONLINER_LIMIT
            user_limit = 10

            kufar_slice = kufar_ads[kufar_offset : kufar_offset + kufar_limit]
            onliner_slice = onliner_ads[onliner_offset : onliner_offset + onliner_limit]
            user_slice = user_ads[user_offset : user_offset + user_limit]

            result_slice = user_slice + kufar_slice + onliner_slice

            next_kufar_offset = kufar_offset + len(kufar_slice)
            next_onliner_offset = onliner_offset + len(onliner_slice)
            next_user_offset = user_offset + len(user_slice)

            has_more_kufar = len(kufar_ads) > next_kufar_offset
            has_more_onliner = len(onliner_ads) > next_onliner_offset
            has_more_user = len(user_ads) > next_user_offset
            has_more = has_more_kufar or has_more_onliner or has_more_user

            response_data = []
            for ad_dict in result_slice:
                ad_dict['created_at'] = ad_dict['created_at'].isoformat() if ad_dict.get('created_at') else None
                ad_dict['last_seen'] = ad_dict['last_seen'].isoformat() if ad_dict.get('last_seen') else None
                response_data.append(ad_dict)

            logger.info(f"API Response: Returning {len(response_data)} ads. HasMore: {has_more}. Next Offsets: K={next_kufar_offset}, O={next_onliner_offset}, U={next_user_offset}")

            return jsonify({
                "ads": response_data,
                "has_more": has_more,
                "next_kufar_offset": next_kufar_offset,
                "next_onliner_offset": next_onliner_offset,
                "next_user_offset": next_user_offset
            })

    except psycopg2.Error as db_err:
        logger.error(f"Database error in /api/ads: {db_err}")
        return jsonify({"error": "Database Error", "details": str(db_err)}), 500
    except Exception as e:
        logger.exception(f"Unexpected error in /api/ads: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

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

        uploaded_files = request.files.getlist('photos')
        image_filenames = ','.join(
            [f.filename for f in uploaded_files if f and f.filename]
        )
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
                (user_id, title, description, price, rooms, area, city, address, image_filenames or None)
            )
            result = cur.fetchone()
            if result: listing_id = result[0]
            else: raise Exception("Failed to retrieve listing ID.")
            conn.commit()
        logger.info(f"Pending listing {listing_id} created for user {user_id}")

        if listing_id:
            try:
                bot_instance = Application.builder().token(TELEGRAM_TOKEN).build().bot
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{listing_id}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"reject_{listing_id}")]
                ])
                message_text = (
                    f"🆕 Moderation Request (ID: {listing_id})\n"
                    f"👤 User: {user_id}\n"
                    f"🏠 Title: {title}\n💲 ${price} | {rooms}r | {area or '?'}m²\n"
                    f"📍 {city}, {address or 'N/A'}\n"
                    f"📝 {description or '-'}\n"
                    f"🖼️ Files: {image_filenames or 'None'}"
                )
                await bot_instance.send_message(
                    chat_id=ADMIN_ID, text=message_text, reply_markup=keyboard
                )
                logger.info(f"Admin notification sent for listing {listing_id}")
            except Exception as bot_err:
                logger.error(f"Failed to send admin notification for {listing_id}: {bot_err}")

        return jsonify({"status": "pending", "listing_id": listing_id})

    except psycopg2.Error as db_err:
        logger.error(f"DB error in add_listing: {db_err}")
        if conn: conn.rollback()
        return jsonify({"error": "Database Error"}), 500
    except Exception as e:
        logger.exception(f"Unexpected error in /api/add_listing: {e}")
        if conn: conn.rollback()
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

# --- Flask Routes for Serving Files ---
@app.route('/')
def index():
    logger.debug("Serving index page")
    return ('<html><head><title>Apartment Bot</title></head>'
            '<body><h1>Apartment Bot Backend</h1>'
            '<p>Open the Mini App in Telegram via the bot.</p>'
            '</body></html>')

@app.route('/mini-app')
def mini_app_route():
    html_file = "mini_app.html"
    root_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(root_dir, html_file)
    if not os.path.exists(file_path):
        logger.error(f"HTML file not found at expected path: {file_path}")
        return "Error: Mini App interface file not found.", 404
    logger.info(f"Serving {html_file} from {root_dir}")
    return send_from_directory(root_dir, html_file)

@app.route('/favicon.ico')
def favicon():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    favicon_path = os.path.join(root_dir, 'favicon.ico')
    if not os.path.exists(favicon_path):
        logger.warning(f"Favicon not found at: {favicon_path}")
        return "Favicon not found", 404
    logger.debug(f"Serving favicon from {favicon_path}")
    return send_from_directory(root_dir, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

# --- Telegram Bot Class ---
class ApartmentBot:
    def __init__(self, application: Application):
        self.application = application
        self._setup_handlers()

    def _setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("help", self.help))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))

    async def setup_commands(self):
        commands = [
            BotCommand("start", "🚀 Запустить Поиск Квартир"),
            BotCommand("help", "ℹ️ Получить помощь")
        ]
        try:
            await self.application.bot.set_my_commands(commands)
            logger.info("Bot commands set.")
        except Exception as e:
            logger.error(f"Failed to set bot commands: {e}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id, first_name, last_name, username = user.id, user.first_name, user.last_name, user.username
        logger.info(f"/start from user {user_id} ({username or 'no_username'})")

        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO users (id, first_name, last_name, username) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name, username = EXCLUDED.username;""",
                    (user_id, first_name, last_name, username)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"DB error saving user {user_id}: {e}")
            if conn: conn.rollback()
        finally:
            if conn: conn.close()

        host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
        if not host:
            logger.warning("RENDER_EXTERNAL_HOSTNAME not set. Using fallback URL structure.")
            host = f"{os.environ.get('RENDER_SERVICE_NAME', 'YOUR_APP_NAME')}.onrender.com"

        web_app_url = f"https://{host}/mini-app"
        logger.info(f"Web App URL for user {user_id}: {web_app_url}")

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть Поиск Квартир 🏠", web_app={"url": web_app_url})]])
        await update.message.reply_text("👋 Нажмите кнопку ниже, чтобы найти квартиру:", reply_markup=keyboard)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id, username = user.id, user.username
        logger.info(f"/help from user {user_id} ({username or 'no_username'})")
        help_text = (
            "ℹ️ **Помощь по боту**\n\n"
            "Я помогу вам найти квартиру для долгосрочной аренды в Беларуси.\n"
            "Используйте команду /start, чтобы открыть интерфейс поиска.\n"
            "Вы можете фильтровать объявления по городу, цене и количеству комнат.\n"
            "Также вы можете добавить свое объявление через интерфейс.\n\n"
            "Если у вас есть вопросы, свяжитесь с администратором."
        )
        await update.message.reply_text(help_text)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = query.from_user
        if user.id != ADMIN_ID:
            await query.answer("⛔ Access Denied", show_alert=True)
            return

        await query.answer()
        data = query.data
        action, listing_id_str = data.split("_", 1)
        listing_id = int(listing_id_str)
        logger.info(f"Admin action '{action}' for listing_id {listing_id}")

        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT * FROM pending_listings WHERE id = %s", (listing_id,))
                listing = cur.fetchone()
                if not listing:
                    await query.edit_message_text(f"⚠️ Listing {listing_id} not found or already processed.")
                    return

                original_poster_id = listing['user_id']
                listing_title_short = listing['title'][:50] + ('...' if len(listing['title']) > 50 else '')

                if action == "approve":
                    cur.execute(
                        """INSERT INTO ads (link, source, city, price, rooms, address, image, description, user_id, created_at, last_seen)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (link) DO NOTHING""",
                        (f"user_listing_{listing_id}", "User", listing['city'], listing['price'], listing['rooms'],
                         listing['address'], listing['image_filenames'], listing['description'], original_poster_id,
                         listing['submitted_at'], listing['submitted_at'])
                    )
                    cur.execute("DELETE FROM pending_listings WHERE id = %s", (listing_id,))
                    conn.commit()
                    await query.edit_message_text(f"✅ Approved & Published: Listing {listing_id}")
                    logger.info(f"Listing {listing_id} approved.")
                    try:
                        await context.bot.send_message(original_poster_id, f"🎉 Ваше объявление '{listing_title_short}' одобрено!")
                    except Exception as notify_err:
                        logger.warning(f"Failed to notify user {original_poster_id} of approval: {notify_err}")

                elif action == "reject":
                    cur.execute("DELETE FROM pending_listings WHERE id = %s", (listing_id,))
                    conn.commit()
                    await query.edit_message_text(f"❌ Rejected: Listing {listing_id}")
                    logger.info(f"Listing {listing_id} rejected.")
                    try:
                        await context.bot.send_message(original_poster_id, f"😔 Ваше объявление '{listing_title_short}' отклонено.")
                    except Exception as notify_err:
                        logger.warning(f"Failed to notify user {original_poster_id} of rejection: {notify_err}")

        except psycopg2.Error as db_err:
            logger.error(f"DB error handling callback for {listing_id}: {db_err}")
            if conn: conn.rollback()
            try: await query.edit_message_text("⚠️ Database error.")
            except: pass
        except Exception as e:
            logger.exception(f"Unexpected error handling callback {listing_id}: {e}")
            if conn: conn.rollback()
            try: await query.edit_message_text("⚠️ Internal error.")
            except: pass
        finally:
            if conn: conn.close()

# --- Main Application Logic ---
async def main():
    logger.info("--- Application Starting ---")

    try:
        init_db()
    except ConnectionError:
        logger.critical("Stopping application due to DB initialization failure.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_instance = ApartmentBot(application)
    await bot_instance.setup_commands()

    scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
    initial_run_time = datetime.datetime.now() + datetime.timedelta(seconds=15)
    scheduler.add_job(
        fetch_and_store_all_ads,
        trigger=IntervalTrigger(minutes=PARSE_INTERVAL, start_date=initial_run_time),
        id='ad_parser_job',
        name='Fetch and Store Ads',
        replace_existing=True
    )
    scheduler.start()
    logger.info(f"Scheduler started. First run at ~{initial_run_time.strftime('%H:%M:%S')}, then every {PARSE_INTERVAL} min.")

    config = Config()
    port = os.environ.get("PORT", "10000")
    config.bind = [f"0.0.0.0:{port}"]
    config.use_reloader = bool(os.environ.get("DEBUG"))
    config.accesslog = logger
    config.errorlog = logger
    logger.info(f"Hypercorn configured for 0.0.0.0:{port}")

    logger.info("Starting Telegram bot polling thread...")
    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        finally:
            loop.close()

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    logger.info("Starting Hypercorn ASGI server...")
    server_task = asyncio.create_task(hypercorn.asyncio.serve(app, config))

    try:
        await server_task
    except asyncio.CancelledError:
        logger.info("Server task cancelled.")
    finally:
        logger.info("Shutting down scheduler...")
        if scheduler.running:
            scheduler.shutdown(wait=False)
        logger.info("Stopping PTB application...")
        logger.info("--- Application Shutdown Complete ---")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped manually (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Application exited with critical error: {e}", exc_info=True)
