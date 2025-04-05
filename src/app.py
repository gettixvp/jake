import logging
import asyncio
import re
import urllib.parse
import os
import datetime
import random
import time
from typing import List, Dict, Optional, Tuple
from flask import Flask, request, jsonify, send_from_directory, abort
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import Forbidden, TimedOut, BadRequest
from bs4 import BeautifulSoup, Tag
import aiohttp
# Removed: from apscheduler.schedulers.asyncio import AsyncIOScheduler
# Removed: from apscheduler.triggers.interval import IntervalTrigger
import hypercorn.asyncio
from hypercorn.config import Config
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2 import sql

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()] # Output logs to console
)
# Suppress overly verbose logs from libraries
logging.getLogger('httpx').setLevel(logging.WARNING)
# logging.getLogger('apscheduler').setLevel(logging.WARNING) # No longer needed
logging.getLogger('telegram').setLevel(logging.INFO)
logging.getLogger('telegram.ext').setLevel(logging.INFO)
logging.getLogger('httpcore').setLevel(logging.INFO) # Reduce httpcore noise

logger = logging.getLogger(__name__)

# --- Configuration ---
# !! IMPORTANT: Keep your actual token and DB URL here or in environment variables !!
# Check if the variables exist in the environment, otherwise use defaults
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '7846698102:AAFR2bhmjAkPiV-PjtnFIu_oRnzxYPP1xVo') # Replace with your token if not using env var
ADMIN_ID_STR = os.environ.get('ADMIN_ID', '7756130972') # Replace with your admin ID if not using env var
try:
    ADMIN_ID = int(ADMIN_ID_STR)
except (ValueError, TypeError):
    logger.critical("Invalid or missing ADMIN_ID environment variable.")
    exit(1)

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgresql_6nv7_user:EQCCcg1l73t8S2g9sfF2LPVx6aA5yZts@dpg-cvlq2pggjchc738o29r0-a.frankfurt-postgres.render.com/postgresql_6nv7') # Replace if not using env var

# --- Database Setup ---
def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True # Autocommit changes for simplicity here, manage transactions if needed
        # logger.debug("Database connection established.") # DEBUG level might be too verbose
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection failed: {e}")
        # Optionally implement retry logic or raise the exception
        raise

def init_db():
    """Initializes the database schema if it doesn't exist."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS listings (
                    id SERIAL PRIMARY KEY,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT,
                    price TEXT,
                    source VARCHAR(50), -- 'onliner' or 'kufar'
                    city VARCHAR(100),
                    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_listings_url ON listings (url);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_listings_added_at ON listings (added_at);
            """)
            # Optional: Add user tracking table if needed later
            # cur.execute("""
            #     CREATE TABLE IF NOT EXISTS users (
            #         user_id BIGINT PRIMARY KEY,
            #         first_name TEXT,
            #         last_name TEXT,
            #         username TEXT,
            #         last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            #     );
            # """)
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
    finally:
        if conn:
            conn.close()
            # logger.debug("Database connection closed after init.") # DEBUG level

# --- Scraping Configuration ---
# Use provided URLs with bounds, ensure only_owner=true where specified
# !!! CRITICAL: Replace placeholder Kufar URLs with REAL Kufar search URLs !!!
SOURCES = {
    "Минск (Onliner)": "https://r.onliner.by/ak/#bounds%5Blb%5D%5Blat%5D=53.820922446131&bounds%5Blb%5D%5Blong%5D=27.344970703125&bounds%5Brt%5D%5Blat%5D=53.97547425743&bounds%5Brt%5D%5Blong%5D=27.77961730957",
    "Минск (Kufar - Собств.)": "https://www.kufar.by/l/r~minsk/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1", # Example Kufar URL - REPLACE
    "Брест (Kufar - Собств.)": "https://www.kufar.by/l/r~brest/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1", # Example Kufar URL - REPLACE
    "Витебск (Kufar - Собств.)": "https://www.kufar.by/l/r~vitebsk/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1", # Example Kufar URL - REPLACE
    "Гомель (Onliner)": "https://r.onliner.by/ak/#bounds%5Blb%5D%5Blat%5D=52.302600726968&bounds%5Blb%5D%5Blong%5D=30.732192993164&bounds%5Brt%5D%5Blat%5D=52.593037841157&bounds%5Brt%5D%5Blong%5D=31.166839599609",
    "Гродно (Kufar - Собств.)": "https://www.kufar.by/l/r~grodno/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1", # Example Kufar URL - REPLACE
    "Могилев (Kufar - Собств.)": "https://www.kufar.by/l/r~mogilev/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1" # Example Kufar URL - REPLACE
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36" # Updated UA
HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.5'}

# --- Helper Functions ---
async def fetch_url(session: aiohttp.ClientSession, url: str) -> Optional[Tuple[str, int]]:
    """Fetches content from a URL asynchronously with basic CAPTCHA check. Returns (content, status_code) or None."""
    # Reduced delay as multiple fetches happen concurrently
    await asyncio.sleep(random.uniform(0.5, 1.5))
    try:
        async with session.get(url, headers=HEADERS, timeout=25, ssl=False) as response: # ssl=False can help sometimes, but is less secure
            status_code = response.status
            content = await response.text()
            # Basic CAPTCHA detection based on title
            if status_code == 200 and "<title>" in content.lower():
                 title_match = re.search(r'<title>(.*?)</title>', content, re.IGNORECASE)
                 if title_match:
                     title_text = title_match.group(1).lower().strip()
                     captcha_keywords = ["защита от автоматических запросов", "check", "captcha", "доступ ограничен", "verify", "robot"]
                     if any(keyword in title_text for keyword in captcha_keywords):
                         logger.warning(f"Potential CAPTCHA detected at {url} (Title: {title_text})")
                         return (None, status_code) # Indicate CAPTCHA but still return status

            if status_code != 200:
                 logger.warning(f"Non-200 status fetching {url}: {status_code}")
                 # You might still want to return content for certain codes (e.g., 404 means gone)
                 # For now, only return content on 200 success, unless CAPTCHA suspected
                 if "защита от автоматических запросов" in content.lower(): # Check content too
                     logger.warning(f"Potential CAPTCHA detected in content at {url} (Status: {status_code})")
                     return (None, status_code)
                 return (None, status_code) # Return None content for non-200 non-captcha

            logger.info(f"Successfully fetched {url} (status: {status_code})")
            return (content, status_code)
    except aiohttp.ClientResponseError as e:
        logger.error(f"HTTP Error fetching {url}: {e.status} {e.message}")
        return (None, e.status if hasattr(e, 'status') else 500)
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching {url}")
        return (None, 408) # Request Timeout
    except aiohttp.ClientConnectionError as e:
         logger.error(f"Connection Error fetching {url}: {e}")
         return (None, 503) # Service Unavailable or connection issue
    except aiohttp.ClientError as e:
        logger.error(f"General Client Error fetching {url}: {e}")
        return (None, 500)
    except Exception as e:
        logger.error(f"Unexpected error fetching {url}: {e}", exc_info=True)
        return (None, 500) # Internal Server Error

def extract_city_from_key(key: str) -> str:
    """Extracts city name from the source key."""
    match = re.match(r"^(Минск|Брест|Витебск|Гомель|Гродно|Могилев)", key)
    return match.group(1) if match else "Unknown"

def extract_source_type_from_key(key: str) -> str:
    """Determines if it's Onliner or Kufar from the key."""
    if "onliner" in key.lower():
        return "onliner"
    if "kufar" in key.lower():
        return "kufar"
    return "unknown"

# --- Parsing Functions ---

def parse_onliner(html_content: str, city: str, url_source: str) -> List[Dict[str, str]]:
    """Parses apartment listings from Onliner HTML content."""
    listings = []
    if not html_content: return []
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        # Updated selector based on potential Onliner structure (adjust if needed)
        results = soup.select('div.classifieds__item') # This might be a more common container

        # Fallback selector if the first doesn't work
        if not results:
             results = soup.select('div.resultset div.classified')

        if not results:
            logger.warning(f"No Onliner listings found for {city} using common selectors. Structure might have changed or page was empty/blocked. URL: {url_source}")
            no_results_msg = soup.find(text=re.compile("По вашему запросу ничего не найдено", re.IGNORECASE))
            if no_results_msg:
                 logger.info(f"Onliner search for {city} returned 'no results'.")
            return []

        for item in results:
            try:
                # Try different selectors for robustness
                link_tag = item.select_one('a.classified__handle') or item.select_one('a[href*="/ak/"]')
                # Title might be within the link or a specific element
                title_tag = item.select_one('.classified__title') or link_tag

                # Price can be tricky, try common patterns
                price_tag = item.select_one('.classified__price-value span:first-child') or item.select_one('div[class*="price"] span')

                if link_tag and link_tag.get('href'):
                    # Ensure URL is absolute
                    relative_url = link_tag['href']
                    if relative_url.startswith('//'):
                         url = 'https:' + relative_url
                    elif relative_url.startswith('/'):
                         url = urllib.parse.urljoin("https://r.onliner.by", relative_url)
                    else:
                         url = relative_url # Assume it's absolute if no leading slash

                    title = title_tag.text.strip() if title_tag else "Нет заголовка"
                    # Clean up price string
                    price_raw = price_tag.text.strip() if price_tag else "Нет цены"
                    price = re.sub(r'\s+', ' ', price_raw).replace('&nbsp;', ' ')


                    listings.append({
                        "url": url,
                        "title": title,
                        "price": price,
                        "source": "onliner",
                        "city": city
                    })
                else:
                    logger.warning(f"Skipping item in Onliner ({city}) due to missing link/href in element: {item.prettify()[:200]}...")

            except Exception as e:
                logger.error(f"Error parsing individual Onliner item in {city}: {e}", exc_info=False)
                continue

    except Exception as e:
        logger.error(f"General error parsing Onliner content for {city}: {e}", exc_info=True)
    logger.info(f"Parsed {len(listings)} listings from Onliner ({city})")
    return listings


def parse_kufar(html_content: str, city: str, url_source: str) -> List[Dict[str, str]]:
    """Parses apartment listings from Kufar HTML content using robust selectors."""
    listings = []
    if not html_content: return []
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        # Find links that look like Kufar item links (more specific if possible)
        link_tags = soup.select('a[href^="/item/"]') # Main selector

        # Kufar sometimes wraps ads in articles or divs with specific data attributes
        # Fallback: Find potential ad containers first
        if not link_tags:
             ad_containers = soup.select('article[data-testid*="ad-card"]') or soup.select('div[class*="--AdCard"]')
             for container in ad_containers:
                  link_tag = container.select_one('a[href^="/item/"]')
                  if link_tag:
                       link_tags.append(link_tag)

        if not link_tags:
            logger.warning(f"No Kufar listing links found for {city} using selectors: a[href^='/item/'], article[data-testid*='ad-card'], div[class*='--AdCard']. Structure might have changed. URL: {url_source}")
            no_results_msg = soup.find(text=re.compile("(ничего не найдено|Нет объявлений по вашему запросу)", re.IGNORECASE))
            if no_results_msg:
                 logger.info(f"Kufar search for {city} returned 'no results'.")
            return []

        processed_urls = set()

        for link_tag in link_tags:
            try:
                href = link_tag.get('href')
                if not href:
                    continue

                url = urllib.parse.urljoin("https://www.kufar.by", href)
                if url in processed_urls:
                    continue

                # Find the closest ancestor that likely contains all ad info
                parent_card = link_tag.find_parent(['article', 'section', 'div[class*="--AdCard"]', 'li'])

                title = "Нет заголовка"
                price = "Нет цены"

                if parent_card:
                    # Search within the parent card
                    title_tag = parent_card.select_one('h3') or parent_card.select_one('div[class*="--title"]') or parent_card.select_one('p[class*="--name"]')
                    if title_tag:
                        title = title_tag.text.strip()

                    price_tag = parent_card.select_one('span[class*="--price"] p') or parent_card.select_one('div[class*="--price"] span') or parent_card.select_one('[class*="styles_price__"]') # More flexible price selector
                    if price_tag:
                        price_raw = " ".join(price_tag.stripped_strings)
                        price = re.sub(r'\s+', ' ', price_raw).replace('&nbsp;', ' ')
                else:
                    # Fallback if no clear parent card found - try siblings or link text
                    if link_tag.string and link_tag.string.strip():
                         title = link_tag.string.strip()
                    # Price might be a sibling or nearby element (harder without parent context)
                    # This part is less reliable
                    price_sibling = link_tag.find_next_sibling(text=re.compile(r'(p\.|руб|byn|\$)', re.IGNORECASE))
                    if price_sibling:
                        price = price_sibling.strip()


                # Final check for title using the link's text if still not found
                if title == "Нет заголовка" and link_tag.get_text(strip=True):
                    title = link_tag.get_text(strip=True)

                # Clean common price suffixes
                price = price.replace(' р.', '').replace('руб.', '').strip()


                listings.append({
                    "url": url,
                    "title": title,
                    "price": price,
                    "source": "kufar",
                    "city": city
                })
                processed_urls.add(url)

            except Exception as e:
                logger.error(f"Error parsing individual Kufar item in {city} (URL: {url if 'url' in locals() else 'N/A'}): {e}", exc_info=False)
                continue

    except Exception as e:
        logger.error(f"General error parsing Kufar content for {city}: {e}", exc_info=True)
    logger.info(f"Parsed {len(listings)} listings from Kufar ({city})")
    return listings

# --- Core Scraping Logic ---
async def scrape_source(session: aiohttp.ClientSession, key: str, url: str) -> List[Dict[str, str]]:
    """Scrapes a single source URL."""
    logger.info(f"Starting scrape for: {key} ({url})")
    content, status_code = await fetch_url(session, url)

    if content is None and status_code != 404: # Treat 404 as empty, but others (like CAPTCHA) as failure
        logger.warning(f"Failed to fetch content for {key} (Status: {status_code}), skipping.")
        return []
    if content is None and status_code == 404:
        logger.info(f"URL {key} returned 404 Not Found.")
        return []


    city = extract_city_from_key(key)
    source_type = extract_source_type_from_key(key)

    if source_type == "onliner":
        return parse_onliner(content, city, url)
    elif source_type == "kufar":
         if "kufar.by" not in url:
              logger.error(f"Misconfigured source: '{key}' is marked as Kufar but URL is '{url}'. Please provide a valid Kufar URL in SOURCES.")
              return []
         return parse_kufar(content, city, url)
    else:
        logger.warning(f"Unknown source type for key: {key}")
        return []

async def scrape_all_sources(user_id: Optional[int] = None) -> int:
    """Scrapes all configured sources, stores new findings, returns count of new items."""
    start_time = time.monotonic()
    logger.info(f"--- Starting scrape cycle (triggered by user: {user_id or 'System'}) ---")
    new_listings_count = 0
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Get existing URLs to check for duplicates efficiently
            cur.execute("SELECT url FROM listings")
            existing_urls = {row['url'] for row in cur.fetchall()}
            logger.info(f"Found {len(existing_urls)} existing listings in DB before scrape.")

            async with aiohttp.ClientSession() as session:
                tasks = [scrape_source(session, key, url) for key, url in SOURCES.items()]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                all_fetched_listings = []
                for i, result in enumerate(results):
                    source_key = list(SOURCES.keys())[i]
                    if isinstance(result, Exception):
                        logger.error(f"Scraping task failed for {source_key}: {result}", exc_info=result)
                    elif isinstance(result, list):
                         logger.info(f"Scraping task for {source_key} returned {len(result)} items.")
                         all_fetched_listings.extend(result)
                    else:
                         logger.warning(f"Scraping task for {source_key} returned unexpected type: {type(result)}")


            logger.info(f"Total items fetched across all sources: {len(all_fetched_listings)}")

            # Identify and prepare new listings for insertion
            listings_to_insert = []
            for listing in all_fetched_listings:
                if listing['url'] not in existing_urls:
                    listings_to_insert.append(listing)
                    existing_urls.add(listing['url']) # Add to set to avoid duplicates within this run
                else:
                    # Update last_seen_at for existing listings (optional, can be heavy)
                    # Consider doing this less frequently or only if data changes
                    try:
                        update_sql = sql.SQL("""
                            UPDATE listings SET last_seen_at = CURRENT_TIMESTAMP
                            WHERE url = %s AND last_seen_at < (CURRENT_TIMESTAMP - INTERVAL '1 hour')
                        """) # Only update if not seen recently
                        cur.execute(update_sql, (listing['url'],))
                    except Exception as db_update_err:
                         logger.error(f"Failed to update last_seen_at for {listing['url']}: {db_update_err}")


            new_listings_count = len(listings_to_insert)
            logger.info(f"Identified {new_listings_count} new listings to insert.")

            # Insert new listings into the database in batches if necessary
            if listings_to_insert:
                insert_query = """
                    INSERT INTO listings (url, title, price, source, city, added_at, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (url) DO NOTHING;
                """
                # Prepare data for executemany
                data_to_insert_tuples = [
                    (
                        listing['url'],
                        listing.get('title', 'Нет заголовка')[:255], # Limit title length
                        listing.get('price', 'Нет цены')[:50], # Limit price length
                        listing.get('source', 'unknown')[:50],
                        listing.get('city', 'Unknown')[:100],
                    )
                    for listing in listings_to_insert
                ]
                try:
                    # Use execute_batch for potential efficiency with many inserts
                    # psycopg2.extras.execute_batch(cur, insert_query, data_to_insert_tuples, page_size=100)
                    # Or stick with executemany if execute_batch is not available or needed
                    cur.executemany(insert_query, data_to_insert_tuples)

                    conn.commit() # Commit after successful insertion
                    logger.info(f"Successfully inserted {new_listings_count} new listings into the database.")
                except psycopg2.Error as db_err:
                     logger.error(f"Database insert/batch failed: {db_err}", exc_info=True)
                     conn.rollback() # Rollback on error
                     new_listings_count = 0 # Reset count as insert failed
                except Exception as e:
                     logger.error(f"Unexpected error during DB insert: {e}", exc_info=True)
                     conn.rollback()
                     new_listings_count = 0

    except psycopg2.Error as db_err:
         logger.error(f"Database error during scraping cycle: {db_err}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error during scraping cycle: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
            logger.debug("Database connection closed after scrape cycle.")

    end_time = time.monotonic()
    logger.info(f"--- Finished scrape cycle in {end_time - start_time:.2f} seconds. Found {new_listings_count} new listings. ---")
    return new_listings_count

# --- Telegram Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message with a button to open the Mini App."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username or 'NoUsername'}) started the bot.")

    # !!! IMPORTANT: SET YOUR WEBAPP URL HERE !!!
    # This must be the HTTPS URL where your Flask app is publicly accessible.
    # Example for Render: https://your-app-name.onrender.com/mini_app
    # Example for local ngrok: https://your_ngrok_subdomain.ngrok.io/mini_app
    WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-flask-app-url.com/mini_app") # Use env var or fallback
    if WEBAPP_URL == "https://your-flask-app-url.com/mini_app":
         logger.warning("WEBAPP_URL is not set or using the default placeholder. Mini App button might not work.")


    keyboard = [
        [InlineKeyboardButton("🔍 Открыть поиск квартир", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_html(
        rf"Привет, {user.mention_html()}! 👋"
        "\n\nНажмите кнопку ниже, чтобы открыть приложение и найти свежие объявления об аренде квартир.",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays help information."""
    await update.message.reply_text(
        "Бот ищет новые объявления об аренде квартир на Onliner и Kufar.\n"
        "Используйте команду /start, чтобы открыть Mini App для поиска и просмотра объявлений."
        )

# --- Flask Web Application (for Mini App Hosting & API) ---
# Use a unique static folder name to avoid potential conflicts if deploying multiple apps
STATIC_FOLDER = 'static_webapp'
flask_app = Flask(__name__, static_folder=STATIC_FOLDER, static_url_path=f'/{STATIC_FOLDER}')
# Disable strict slashes for flexibility, e.g. /mini_app works like /mini_app/
flask_app.url_map.strict_slashes = False

@flask_app.route('/')
def index():
    """Serves the main Mini App HTML file."""
    return send_from_directory(flask_app.static_folder, 'mini_app.html')

@flask_app.route('/mini_app') # Explicit route for the webapp URL
def mini_app_route():
    """Serves the main Mini App HTML file."""
    return send_from_directory(flask_app.static_folder, 'mini_app.html')


# Semaphore to prevent multiple concurrent scrapes
scrape_semaphore = asyncio.Semaphore(1)
# Variable to track if a scrape is in progress
is_scraping = False

@flask_app.route('/initiate_scrape', methods=['POST'])
async def initiate_scrape_endpoint():
    """Endpoint called by Mini App to trigger scraping. Prevents concurrent scrapes."""
    global is_scraping
    if not scrape_semaphore.locked(): # Check semaphore first
        async with scrape_semaphore: # Acquire lock
            is_scraping = True
            logger.info("Scrape lock acquired. Initiating scrape.")
            try:
                # Optional: Get user ID if needed for user-specific logic later
                user_data = request.json
                user_id = user_data.get('userId') if user_data else None
                logger.info(f"Received scrape initiation request from Mini App (User ID: {user_id})")

                # Run scraping
                new_count = await scrape_all_sources(user_id=user_id)
                logger.info(f"Scraping initiated by user {user_id} completed. Found {new_count} new items.")
                return jsonify({"status": "scrape_completed", "new_items": new_count}), 200 # OK - completed
            except Exception as e:
                logger.error(f"Error during initiated scrape: {e}", exc_info=True)
                return jsonify({"status": "scrape_failed", "error": str(e)}), 500 # Internal Server Error
            finally:
                 is_scraping = False
                 logger.info("Scrape lock released.")

    else:
        logger.warning("Scrape initiation request received, but another scrape is already in progress.")
        return jsonify({"status": "scrape_in_progress"}), 429 # Too Many Requests / Busy


@flask_app.route('/get_listings', methods=['GET'])
def get_listings_endpoint():
    """Endpoint called by Mini App to fetch latest listings."""
    limit = request.args.get('limit', 50, type=int) # Get limit or default to 50
    limit = max(1, min(limit, 150)) # Clamp limit

    logger.info(f"Received request to get listings (limit: {limit})")
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Fetch latest listings ordered by when they were added
            # Ensure added_at is selected for ordering and display
            cur.execute(
                """
                SELECT url, title, price, source, city, added_at
                FROM listings
                ORDER BY added_at DESC
                LIMIT %s
                """,
                (limit,)
            )
            listings = [dict(row) for row in cur.fetchall()]
            # Convert datetime to ISO string for JSON serialization
            for listing in listings:
                if isinstance(listing.get('added_at'), datetime.datetime):
                     # Format with timezone info if available
                     listing['added_at'] = listing['added_at'].isoformat()

            # Check if a scrape is currently running
            scrape_status = "in_progress" if is_scraping else "idle"

        return jsonify({"listings": listings, "scrape_status": scrape_status}), 200

    except psycopg2.Error as db_err:
         logger.error(f"Database error fetching listings: {db_err}", exc_info=True)
         return jsonify({"error": "Database error"}), 500
    except Exception as e:
        logger.error(f"Unexpected error fetching listings: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if conn:
            conn.close()
            logger.debug("Database connection closed after getting listings.")

async def run_flask_app():
    """Runs the Flask app using Hypercorn."""
    config = Config()
    # Use PORT environment variable provided by hosting services like Render, default to 8080
    port = int(os.environ.get('PORT', 8080))
    config.bind = [f"0.0.0.0:{port}"]
    config.use_reloader = False # Important for production/asyncio loop
    config.loglevel = "info"
    # Increase graceful shutdown timeout if needed
    # config.graceful_timeout = 10.0

    logger.info(f"Starting Hypercorn server for Flask app on port {port}...")
    try:
        await hypercorn.asyncio.serve(flask_app, config)
    except Exception as e:
        logger.error(f"Hypercorn server failed: {e}", exc_info=True)
        raise # Re-raise to potentially stop the main loop

# --- Telegram Application Setup ---
async def setup_bot_commands(application: Application):
    """Sets the bot commands visible in Telegram."""
    commands = [
        BotCommand("start", "🚀 Открыть поиск квартир"),
        BotCommand("help", "ℹ️ Помощь по боту")
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands set successfully.")
    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}")


async def post_init_tasks(application: Application):
    """Tasks to run after the bot application is initialized."""
    await setup_bot_commands(application)
    init_db() # Initialize DB schema on startup
    logger.info("Bot post-initialization complete.")
    # Notify admin on startup (optional)
    try:
        await application.bot.send_message(ADMIN_ID, "✅ Бот успешно запущен!")
    except Exception as e:
        logger.warning(f"Could not send startup notification to admin ({ADMIN_ID}): {e}")


async def main() -> None:
    """Start the bot and the web server."""
    if not TELEGRAM_TOKEN:
        logger.critical("TELEGRAM_TOKEN is not set. Exiting.")
        return
    if not DATABASE_URL:
        logger.critical("DATABASE_URL is not set. Exiting.")
        return

    # Create the Application and pass it your bot's token.
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init_tasks)
        .build()
    )

    # --- Register Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    # Add other handlers if needed (e.g., MessageHandler, CallbackQueryHandler)

    # --- Run Flask App and Bot Polling Concurrently ---
    loop = asyncio.get_running_loop()
    flask_task = loop.create_task(run_flask_app(), name="FlaskHypercornServer")
    polling_task = loop.create_task(application.run_polling(allowed_updates=Update.ALL_TYPES), name="TelegramPolling")

    logger.info("--- Starting Application (Flask + Telegram Polling) ---")

    # Wait for either task to complete (which indicates an error or shutdown)
    done, pending = await asyncio.wait(
        [flask_task, polling_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Handle task completion/failure
    for task in done:
        try:
            result = await task # Check for exceptions raised within the task
            logger.info(f"Task {task.get_name()} completed normally. Result: {result}")
        except asyncio.CancelledError:
             logger.info(f"Task {task.get_name()} was cancelled.")
        except Exception as e:
            logger.error(f"Task {task.get_name()} failed unexpectedly: {e}", exc_info=True)

    # Cancel pending tasks if one has finished/failed
    logger.info("One of the main tasks finished, cancelling pending tasks...")
    for task in pending:
        logger.info(f"Cancelling pending task: {task.get_name()}")
        task.cancel()

    # Wait for cancellations to complete
    await asyncio.gather(*pending, return_exceptions=True)

    # Graceful shutdown for the application object
    logger.info("Shutting down Telegram application...")
    await application.shutdown()

    logger.info("--- Application Shutdown Complete ---")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Application stopped manually (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Application crashed in main execution: {e}", exc_info=True)
    finally:
        # Gracefully cancel all running tasks on exit if loop is still running
        if loop.is_running():
             logger.info("Cleaning up remaining asyncio tasks...")
             tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
             for task in tasks:
                 task.cancel()
             loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        if not loop.is_closed():
             loop.close()
             logger.info("Event loop closed.")
