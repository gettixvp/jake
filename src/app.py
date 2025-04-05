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
                        area REAL CHECK (area > 0),
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
                        area REAL CHECK (area > 0),
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

# --- Parsers ---
class ApartmentParser:
    @staticmethod
    async def fetch_ads(city: str, min_price: Optional[int] = None, max_price: Optional[int] = None, rooms_filter: Optional[str] = None) -> List[Dict]:
        user_agent = random.choice(USER_AGENTS)
        headers = {
            "User-Agent": user_agent,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
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
                        logger.warning(f"No ads found on Kufar for {city}.")
                        return []

                    logger.info(f"Found {len(ad_elements)} potential ads on Kufar for {city}.")
                    for ad_element in ad_elements[:KUFAR_LIMIT]:
                        try:
                            link = ad_element.get("href")
                            if not link or not link.startswith('/l/'): continue
                            full_link = f"https://re.kufar.by{link}"

                            image = ad_element.select_one(".styles_image__7aRPM img")
                            image_url = image["src"] if image else None

                            price_elem = ad_element.select_one(".styles_price__usd__HpXMa")
                            price = int(re.sub(r"[^\d]", "", price_elem.text)) if price_elem else None

                            desc_elem = ad_element.select_one(".styles_body__5BrnC.styles_body__r33c8")
                            description = desc_elem.text.strip() if desc_elem else "Описание не указано"

                            address_elem = ad_element.select_one(".styles_address__l6Qe_")
                            address = address_elem.text.strip() if address_elem else "Адрес не указан"

                            params_elem = ad_element.select_one(".styles_parameters__7zKlL")
                            params = params_elem.text.strip() if params_elem else ""
                            rooms, area = None, None
                            if params:
                                rooms_match = re.search(r"(\d+)\s*комн\.?", params)
                                area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*м", params)
                                rooms = rooms_match.group(1) if rooms_match else "studio" if "студия" in params.lower() else None
                                area = float(area_match.group(1).replace(',', '.')) if area_match else None

                            if not ApartmentParser._check_room_filter(rooms, rooms_filter):
                                continue

                            results.append({
                                "link": full_link,
                                "source": "Kufar",
                                "city": city,
                                "price": price,
                                "rooms": rooms,
                                "area": area,
                                "address": address,
                                "image": image_url,
                                "description": description,
                                "user_id": None,
                                "created_at": datetime.datetime.utcnow().isoformat(),
                                "last_seen": datetime.datetime.utcnow().isoformat()
                            })
                        except Exception as parse_err:
                            logger.warning(f"Could not parse Kufar ad item ({link}): {parse_err}")
        except Exception as e:
            logger.exception(f"Error fetching/parsing Kufar for {city}: {e}")
        return results

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
        }
        results = []
        base_url = "https://r.onliner.by/ak/"
        fragment = ONLINER_CITY_URLS.get(city)
        if not fragment: return []

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
                    ad_elements = soup.select(".classified")

                    if not ad_elements:
                        logger.warning(f"No ads found on Onliner for {city}.")
                        return []

                    logger.info(f"Found {len(ad_elements)} potential ads on Onliner for {city}.")
                    for ad_element in ad_elements[:ONLINER_LIMIT]:
                        try:
                            link_elem = ad_element.select_one("a")
                            link = link_elem["href"] if link_elem else None
                            if not link or not link.startswith('https://r.onliner.by/ak/apartments/'): continue

                            image_elem = ad_element.select_one("img[data-bind='attr: {src: apartment.photo}']")
                            image = image_elem["src"] if image_elem else None

                            price_elem = ad_element.select_one(".classified__price.classified__price_secondary")
                            price = int(re.sub(r"[^\d]", "", price_elem.text.split("$")[0])) if price_elem and "$" in price_elem.text else None

                            address_elem = ad_element.select_one(".classified__caption-item.classified__caption-item_adress")
                            address = address_elem.text.strip() if address_elem else "Адрес не указан"

                            rooms_elem = ad_element.select_one(".classified__caption-item.classified__caption-item_type")
                            rooms = None
                            if rooms_elem:
                                text = rooms_elem.text.strip()
                                if "Студия" in text: rooms = "studio"
                                else: rooms = re.search(r"(\d+)к", text).group(1) if re.search(r"(\d+)к", text) else None

                            if not OnlinerParser._check_room_filter(rooms, rooms_filter): continue

                            results.append({
                                "link": link,
                                "source": "Onliner",
                                "city": city,
                                "price": price,
                                "rooms": rooms,
                                "area": None,  # Onliner не предоставляет площадь в текущих селекторах
                                "address": address,
                                "image": image,
                                "description": address,
                                "user_id": None,
                                "created_at": datetime.datetime.utcnow().isoformat(),
                                "last_seen": datetime.datetime.utcnow().isoformat()
                            })
                        except Exception as parse_err:
                            logger.warning(f"Could not parse Onliner ad item ({link}): {parse_err}")
        except Exception as e:
            logger.exception(f"Error fetching/parsing Onliner for {city}: {e}")
        return results

    @staticmethod
    def _check_room_filter(rooms_str: Optional[str], target_rooms: Optional[str]) -> bool:
        if target_rooms is None: return True
        if rooms_str is None: return False
        if target_rooms == 'studio': return rooms_str == 'studio'
        elif target_rooms == '4+': return rooms_str == '4+' or (rooms_str.isdigit() and int(rooms_str) >= 4)
        elif target_rooms.isdigit(): return rooms_str == target_rooms
        else: return False

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
                INSERT INTO ads (link, source, city, price, rooms, area, address, image, description, user_id, created_at, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (link) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    price = EXCLUDED.price,
                    area = EXCLUDED.area,
                    address = EXCLUDED.address,
                    image = EXCLUDED.image,
                    description = EXCLUDED.description
                RETURNING xmax;
            """
            for ad in ads:
                if not ad.get("link") or not ad.get("source"): continue
                values = (
                    ad.get("link"), ad.get("source"), ad.get("city"), ad.get("price"),
                    ad.get("rooms"), ad.get("area"), ad.get("address"), ad.get("image"),
                    ad.get("description"), ad.get("user_id")
                )
                try:
                    cur.execute(upsert_query, values)
                    result = cur.fetchone()
                    if result and result[0] == 0: added_count += 1
                except Exception as insert_err:
                    logger.error(f"Error upserting ad {ad.get('link')}: {insert_err}")
                    conn.rollback()
                else:
                    conn.commit()
        logger.info(f"DB Store: Processed {len(ads)} ads. Added {added_count} new.")
        return added_count
    except Exception as e:
        logger.exception(f"Error in store_ads: {e}")
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
        city = list(CITIES.keys())[i // 2]
        if isinstance(result, Exception):
            logger.error(f"Error fetching from {source} for {city}: {result}")
        elif isinstance(result, list):
            all_fetched_ads.extend(result)

    if all_fetched_ads:
        total_new_ads = store_ads(all_fetched_ads)
    logger.info(f"Total New Ads Found: {total_new_ads}")
    logger.info(f"Duration: {time.time() - start_time:.2f} seconds")

# --- Flask API Endpoints ---
@app.route('/api/ads', methods=['GET'])
def get_ads_api():
    city = request.args.get('city')
    min_price = request.args.get('min_price', type=int)
    max_price = request.args.get('max_price', type=int)
    rooms = request.args.get('rooms')
    kufar_offset = request.args.get('kufar_offset', default=0, type=int)
    onliner_offset = request.args.get('onliner_offset', default=0, type=int)
    user_offset = request.args.get('user_offset', default=0, type=int)

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

            kufar_ads = [ad for ad in all_ads_dicts if ad["source"] == "Kufar"]
            onliner_ads = [ad for ad in all_ads_dicts if ad["source"] == "Onliner"]
            user_ads = [ad for ad in all_ads_dicts if ad["source"] == "User"]

            kufar_limit = KUFAR_LIMIT
            onliner_limit = ONLINER_LIMIT
            user_limit = 10

            kufar_slice = kufar_ads[kufar_offset:kufar_offset + kufar_limit]
            onliner_slice = onliner_ads[onliner_offset:onliner_offset + onliner_limit]
            user_slice = user_ads[user_offset:user_offset + user_limit]

            result_slice = user_slice + kufar_slice + onliner_slice

            next_kufar_offset = kufar_offset + len(kufar_slice)
            next_onliner_offset = onliner_offset + len(onliner_slice)
            next_user_offset = user_offset + len(user_slice)

            has_more = (len(kufar_ads) > next_kufar_offset or
                       len(onliner_ads) > next_onliner_offset or
                       len(user_ads) > next_user_offset)

            response_data = []
            for ad in result_slice:
                ad['created_at'] = ad['created_at'].isoformat() if ad.get('created_at') else None
                ad['last_seen'] = ad['last_seen'].isoformat() if ad.get('last_seen') else None
                response_data.append(ad)

            return jsonify({
                "ads": response_data,
                "has_more": has_more,
                "next_kufar_offset": next_kufar_offset,
                "next_onliner_offset": next_onliner_offset,
                "next_user_offset": next_user_offset
            })
    except Exception as e:
        logger.exception(f"Error in /api/ads: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/new_listings', methods=['GET'])
def get_new_listings_api():
    user_id = request.args.get('user_id', type=int)
    limit = request.args.get('limit', default=10, type=int)
    offset = request.args.get('offset', default=0, type=int)

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            query = """
                SELECT * FROM ads 
                WHERE created_at > (SELECT COALESCE(MAX(last_seen), '2000-01-01') FROM users WHERE id = %s)
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """
            cur.execute(query, (user_id, limit, offset))
            new_ads = [dict(row) for row in cur.fetchall()]
            for ad in new_ads:
                ad['created_at'] = ad['created_at'].isoformat()
                ad['last_seen'] = ad['last_seen'].isoformat()
            return jsonify({"ads": new_ads, "has_more": len(new_ads) == limit})
    except Exception as e:
        logger.exception(f"Error in /api/new_listings: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/user_listings', methods=['GET'])
def get_user_listings_api():
    user_id = request.args.get('user_id', type=int)
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT * FROM ads WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
            user_ads = [dict(row) for row in cur.fetchall()]
            for ad in user_ads:
                ad['created_at'] = ad['created_at'].isoformat()
                ad['last_seen'] = ad['last_seen'].isoformat()
            return jsonify({"ads": user_ads})
    except Exception as e:
        logger.exception(f"Error in /api/user_listings: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/register_user', methods=['POST'])
def register_user_api():
    data = request.json
    if not data or 'user_id' not in data:
        return jsonify({"error": "Missing user_id"}), 400

    user_id = data['user_id']
    first_name = data.get('first_name')
    last_name = data.get('last_name')
    username = data.get('username')

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
        return jsonify({"status": "success"})
    except Exception as e:
        logger.exception(f"Error registering user {user_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/api/add_listing', methods=['POST'])
async def add_listing_api():
    conn = None
    try:
        user_id = request.form.get('user_id', type=int)
        title = request.form.get('title')
        price = request.form.get('price', type=int)
        rooms = request.form.get('rooms')
        city = request.form.get('city')

        if not all([user_id, title, price, rooms, city]):
            missing = [k for k, v in {'user_id': user_id, 'title': title, 'price': price, 'rooms': rooms, 'city': city}.items() if v is None]
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

        description = request.form.get('description', '')
        area = request.form.get('area', type=float)
        address = request.form.get('address', '')

        uploaded_files = request.files.getlist('photos[]')
        image_filenames = ','.join([f.filename for f in uploaded_files if f and f.filename]) if uploaded_files else None

        uploads_dir = os.path.join(os.path.dirname(__file__), 'uploads')
        os.makedirs(uploads_dir, exist_ok=True)
        saved_images = []
        for file in uploaded_files:
            if file and file.filename:
                filename = f"{user_id}_{int(time.time())}_{file.filename}"
                file.save(os.path.join(uploads_dir, filename))
                saved_images.append(filename)
        image_filenames = ','.join(saved_images) if saved_images else None

        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_listings
                (user_id, title, description, price, rooms, area, city, address, image_filenames, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') RETURNING id
                """,
                (user_id, title, description, price, rooms, area, city, address, image_filenames)
            )
            listing_id = cur.fetchone()[0]
            conn.commit()

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
        await bot_instance.send_message(chat_id=ADMIN_ID, text=message_text, reply_markup=keyboard)

        return jsonify({"status": "pending", "listing_id": listing_id})
    except Exception as e:
        logger.exception(f"Error in /api/add_listing: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    finally:
        if conn: conn.close()

@app.route('/')
def index():
    return '<h1>Apartment Bot Backend</h1><p>Open the Mini App in Telegram.</p>'

@app.route('/mini-app')
def mini_app_route():
    return send_from_directory(os.path.dirname(__file__), 'mini_app.html')

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
        await self.application.bot.set_my_commands(commands)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO users (id, first_name, last_name, username) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name, username = EXCLUDED.username;""",
                    (user.id, user.first_name, user.last_name, user.username)
                )
                conn.commit()
        except Exception as e:
            logger.exception(f"DB error saving user {user.id}: {e}")
        finally:
            if conn: conn.close()

        host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'YOUR_APP_NAME.onrender.com')
        web_app_url = f"https://{host}/mini-app"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть Поиск Квартир 🏠", web_app={"url": web_app_url})]])
        await update.message.reply_text("👋 Нажмите кнопку ниже, чтобы найти квартиру:", reply_markup=keyboard)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "ℹ️ **Помощь по боту**\n\n"
            "Я помогу вам найти квартиру для долгосрочной аренды в Беларуси.\n"
            "Используйте /start, чтобы открыть интерфейс поиска.\n"
            "Вы можете фильтровать объявления и добавлять свои."
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query.from_user.id != ADMIN_ID:
            await query.answer("⛔ Access Denied", show_alert=True)
            return

        await query.answer()
        action, listing_id = query.data.split("_", 1)
        listing_id = int(listing_id)

        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT * FROM pending_listings WHERE id = %s", (listing_id,))
                listing = cur.fetchone()
                if not listing:
                    await query.edit_message_text(f"⚠️ Listing {listing_id} not found.")
                    return

                if action == "approve":
                    cur.execute(
                        """INSERT INTO ads (link, source, city, price, rooms, area, address, image, description, user_id, created_at, last_seen)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (link) DO NOTHING""",
                        (f"user_listing_{listing_id}", "User", listing['city'], listing['price'], listing['rooms'],
                         listing['area'], listing['address'], listing['image_filenames'], listing['description'],
                         listing['user_id'], listing['submitted_at'], listing['submitted_at'])
                    )
                    cur.execute("DELETE FROM pending_listings WHERE id = %s", (listing_id,))
                    conn.commit()
                    await query.edit_message_text(f"✅ Approved: Listing {listing_id}")
                    await context.bot.send_message(listing['user_id'], f"🎉 Ваше объявление '{listing['title']}' одобрено!")
                elif action == "reject":
                    cur.execute("DELETE FROM pending_listings WHERE id = %s", (listing_id,))
                    conn.commit()
                    await query.edit_message_text(f"❌ Rejected: Listing {listing_id}")
                    await context.bot.send_message(listing['user_id'], f"😔 Ваше объявление '{listing['title']}' отклонено.")
        except Exception as e:
            logger.exception(f"Error handling callback {listing_id}: {e}")
            await query.edit_message_text("⚠️ Internal error.")
        finally:
            if conn: conn.close()

# --- Main Application Logic ---
async def shutdown_application(application: Application, scheduler: AsyncIOScheduler):
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await application.stop()
    await application.updater.shutdown()

async def main():
    logger.info("--- Application Starting ---")
    try:
        init_db()
    except ConnectionError:
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_instance = ApartmentBot(application)
    await bot_instance.setup_commands()

    scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
    scheduler.add_job(
        fetch_and_store_all_ads,
        trigger=IntervalTrigger(minutes=PARSE_INTERVAL, start_date=datetime.datetime.now() + datetime.timedelta(seconds=15)),
        id='ad_parser_job',
        replace_existing=True
    )
    scheduler.start()

    config = Config()
    port = int(os.environ.get("PORT", "10000"))
    config.bind = [f"0.0.0.0:{port}"]
    config.use_reloader = bool(os.environ.get("DEBUG"))

    async with application:
        await application.initialize()
        await application.start()
        polling_task = asyncio.create_task(application.updater.start_polling(allowed_updates=Update.ALL_TYPES))
        hypercorn_task = asyncio.create_task(hypercorn.asyncio.serve(app, config))

        try:
            done, pending = await asyncio.wait(
                [polling_task, hypercorn_task],
                return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                if task.exception(): raise task.exception()
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            for task in [polling_task, hypercorn_task]:
                task.cancel()
            await shutdown_application(application, scheduler)
            raise
        finally:
            await asyncio.gather(polling_task, hypercorn_task, return_exceptions=True)
            await shutdown_application(application, scheduler)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Application stopped manually.")
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
    finally:
        if not loop.is_closed():
            loop.close()
