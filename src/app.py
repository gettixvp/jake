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
# from apscheduler.schedulers.asyncio import AsyncIOScheduler # Removed scheduler for manual trigger
# from apscheduler.triggers.interval import IntervalTrigger
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
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.INFO)
logging.getLogger('telegram.ext').setLevel(logging.INFO)
logging.getLogger('httpcore').setLevel(logging.INFO) # Reduce httpcore noise

logger = logging.getLogger(__name__)

# --- Configuration ---
# !! IMPORTANT: Keep your actual token and DB URL here or in environment variables !!
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
        conn.autocommit = True # Autocommit changes
        logger.info("Database connection established.")
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
            # Optional: Add user tracking if needed later
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
            logger.info("Database connection closed after init.")

# --- Scraping Configuration ---
# Use provided URLs with bounds, ensure only_owner=true where specified
SOURCES = {
    "Минск (Onliner)": "https://r.onliner.by/ak/#bounds%5Blb%5D%5Blat%5D=53.820922446131&bounds%5Blb%5D%5Blong%5D=27.344970703125&bounds%5Brt%5D%5Blat%5D=53.97547425743&bounds%5Brt%5D%5Blong%5D=27.77961730957",
    "Минск (Kufar - Собств.)": "https://r.onliner.by/ak/?only_owner=true#bounds%5Blb%5D%5Blat%5D=53.7702250123455&bounds%5Blb%5D%5Blong%5D=27.32986450195313&bounds%5Brt%5D%5Blat%5D=54.02632676232751&bounds%5Brt%5D%5Blong%5D=27.79403686523438", # This looks like an Onliner URL? Assuming Kufar Minsk needed
    "Брест (Kufar - Собств.)": "https://r.onliner.by/ak/?only_owner=true#bounds%5Blb%5D%5Blat%5D=51.941725203142&bounds%5Blb%5D%5Blong%5D=23.492889404297&bounds%5Brt%5D%5Blat%5D=52.234528294214&bounds%5Brt%5D%5Blong%5D=23.927536010742", # Also looks like Onliner? Assuming Kufar Brest needed
    "Витебск (Kufar - Собств.)": "https://r.onliner.by/ak/?only_owner=true#bounds%5Blb%5D%5Blat%5D=54.97288463122323&bounds%5Blb%5D%5Blong%5D=29.733123779296875&bounds%5Brt%5D%5Blat%5D=55.46873480729721&bounds%5Brt%5D%5Blong%5D=30.66146850585938", # Also looks like Onliner? Assuming Kufar Vitebsk needed
    "Гомель (Onliner)": "https://r.onliner.by/ak/#bounds%5Blb%5D%5Blat%5D=52.302600726968&bounds%5Blb%5D%5Blong%5D=30.732192993164&bounds%5Brt%5D%5Blat%5D=52.593037841157&bounds%5Brt%5D%5Blong%5D=31.166839599609",
    "Гродно (Kufar - Собств.)": "https://r.onliner.by/ak/?only_owner=true#bounds%5Blb%5D%5Blat%5D=53.538267122397&bounds%5Blb%5D%5Blong%5D=23.629531860352&bounds%5Brt%5D%5Blat%5D=53.820517109806&bounds%5Brt%5D%5Blong%5D=24.064178466797", # Also looks like Onliner? Assuming Kufar Grodno needed
    "Могилев (Kufar - Собств.)": "https://r.onliner.by/ak/?only_owner=true#bounds%5Blb%5D%5Blat%5D=53.62672436247066&bounds%5Blb%5D%5Blong%5D=29.885559082031254&bounds%5Brt%5D%5Blat%5D=54.139110028283994&bounds%5Brt%5D%5Blong%5D=30.813903808593754" # Also looks like Onliner? Assuming Kufar Mogilev needed
}

# !! Placeholder Kufar URLs - Replace with actual Kufar search URLs !!
# Example format (replace with real searches):
# SOURCES["Минск (Kufar - Собств.)"] = "https://www.kufar.by/l/r~minsk/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1" # oph=1 might be 'only owner'
# SOURCES["Брест (Kufar - Собств.)"] = "https://www.kufar.by/l/r~brest/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1"
# SOURCES["Витебск (Kufar - Собств.)"] = "https://www.kufar.by/l/r~vitebsk/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1"
# SOURCES["Гродно (Kufar - Собств.)"] = "https://www.kufar.by/l/r~grodno/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1"
# SOURCES["Могилев (Kufar - Собств.)"] = "https://www.kufar.by/l/r~mogilev/kvartiry-dolgosrochnaya-arenda?sort=lst.d&cur=BYN&oph=1"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
HEADERS = {'User-Agent': USER_AGENT}

# --- Helper Functions ---
async def fetch_url(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Fetches content from a URL asynchronously with basic CAPTCHA check."""
    await asyncio.sleep(random.uniform(1, 3)) # Random delay
    try:
        async with session.get(url, headers=HEADERS, timeout=20) as response:
            response.raise_for_status() # Raise exception for bad status codes
            content = await response.text()
            # Basic CAPTCHA detection based on title
            if "<title>" in content.lower():
                 title_match = re.search(r'<title>(.*?)</title>', content, re.IGNORECASE)
                 if title_match:
                     title_text = title_match.group(1).lower()
                     captcha_keywords = ["защита", "check", "captcha", "доступ", "verify"]
                     if any(keyword in title_text for keyword in captcha_keywords):
                         logger.warning(f"Potential CAPTCHA detected at {url} (Title: {title_text})")
                         return None # Indicate CAPTCHA or block page
            logger.info(f"Successfully fetched {url} (status: {response.status})")
            return content
    except aiohttp.ClientResponseError as e:
        logger.error(f"HTTP Error fetching {url}: {e.status} {e.message}")
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching {url}")
    except aiohttp.ClientError as e:
        logger.error(f"Client Error fetching {url}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching {url}: {e}", exc_info=True)
    return None

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

def parse_onliner(html_content: str, city: str) -> List[Dict[str, str]]:
    """Parses apartment listings from Onliner HTML content."""
    listings = []
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        # Find listing items - adjust selector if Onliner changes structure
        results = soup.select('div.resultset div.classified') # Example selector, adjust if needed

        if not results:
            logger.warning(f"No Onliner listings found for {city}. Structure might have changed or page was empty/blocked.")
            # Check for common 'no results' messages if possible
            no_results_msg = soup.find(text=re.compile("По вашему запросу ничего не найдено", re.IGNORECASE))
            if no_results_msg:
                 logger.info(f"Onliner search for {city} returned 'no results'.")
            return []


        for item in results:
            try:
                link_tag = item.select_one('a.classified__handle')
                title_tag = item.select_one('a.classified__handle') # Often same as link
                price_tag = item.select_one('.classified__price-value span:first-child') # Get main price value

                if link_tag and link_tag.get('href'):
                    url = urllib.parse.urljoin("https://r.onliner.by", link_tag['href'])
                    title = title_tag.text.strip() if title_tag else "Нет заголовка"
                    price = price_tag.text.strip().replace('&nbsp;', ' ') if price_tag else "Нет цены"

                    listings.append({
                        "url": url,
                        "title": title,
                        "price": price,
                        "source": "onliner",
                        "city": city
                    })
                else:
                    logger.warning(f"Skipping item in Onliner ({city}) due to missing link/href.")

            except Exception as e:
                logger.error(f"Error parsing individual Onliner item in {city}: {e}", exc_info=False) # Keep log concise
                continue # Skip this item

    except Exception as e:
        logger.error(f"General error parsing Onliner content for {city}: {e}", exc_info=True)
    logger.info(f"Parsed {len(listings)} listings from Onliner ({city})")
    return listings


def parse_kufar(html_content: str, city: str) -> List[Dict[str, str]]:
    """Parses apartment listings from Kufar HTML content."""
    listings = []
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        # Find links that look like Kufar item links
        # This targets <a> tags whose href starts with /item/ - common on Kufar
        link_tags = soup.select('a[href^="/item/"]')

        if not link_tags:
            logger.warning(f"No Kufar listings found for {city} using a[href^='/item/']. Structure might have changed or page was empty/blocked.")
            # Check for common 'no results' messages if possible
            no_results_msg = soup.find(text=re.compile("ничего не найдено", re.IGNORECASE))
            if no_results_msg:
                 logger.info(f"Kufar search for {city} returned 'no results'.")
            return []

        processed_urls = set() # Avoid duplicates if multiple links point to the same item

        for link_tag in link_tags:
            try:
                href = link_tag.get('href')
                if not href:
                    continue

                url = urllib.parse.urljoin("https://www.kufar.by", href)
                if url in processed_urls:
                    continue # Skip if already processed

                # Try to find the title and price relative to the link
                # Kufar structure varies, these are common patterns - INSPECT KUFAR'S HTML if this fails
                parent_article = link_tag.find_parent(['article', 'section', 'div']) # Find a container element
                title = "Нет заголовка"
                price = "Нет цены"

                if parent_article:
                    # Try finding title within the container
                    title_tag = parent_article.select_one('h3, div[class*="title"], div[class*="name"]') # Common title elements
                    if title_tag:
                        title = title_tag.text.strip()
                    elif link_tag.string: # Sometimes the link text itself is the title
                         title = link_tag.text.strip()


                    # Try finding price within the container
                    price_tag = parent_article.select_one('span[class*="price"], div[class*="price"]') # Common price elements
                    if price_tag:
                        # Extract text, remove currency symbols/nbsp if needed
                        price_text_parts = [part.strip() for part in price_tag.stripped_strings]
                        price = " ".join(price_text_parts) if price_text_parts else "Нет цены"

                # Fallback if title still not found from parent
                if title == "Нет заголовка" and link_tag.string and link_tag.string.strip():
                     title = link_tag.string.strip()


                listings.append({
                    "url": url,
                    "title": title,
                    "price": price,
                    "source": "kufar",
                    "city": city
                })
                processed_urls.add(url)

            except Exception as e:
                logger.error(f"Error parsing individual Kufar item in {city}: {e}", exc_info=False) # Keep log concise
                continue # Skip this item

    except Exception as e:
        logger.error(f"General error parsing Kufar content for {city}: {e}", exc_info=True)
    logger.info(f"Parsed {len(listings)} listings from Kufar ({city})")
    return listings

# --- Core Scraping Logic ---
async def scrape_source(session: aiohttp.ClientSession, key: str, url: str) -> List[Dict[str, str]]:
    """Scrapes a single source URL."""
    logger.info(f"Starting scrape for: {key}")
    html_content = await fetch_url(session, url)
    if not html_content:
        logger.warning(f"Failed to fetch content for {key}, skipping.")
        return []

    city = extract_city_from_key(key)
    source_type = extract_source_type_from_key(key)

    if source_type == "onliner":
        return parse_onliner(html_content, city)
    elif source_type == "kufar":
         # Check if the provided URL is actually a Kufar URL
         if "kufar.by" not in url:
              logger.error(f"Misconfigured source: '{key}' is marked as Kufar but URL is '{url}'. Please provide a valid Kufar URL.")
              return []
         return parse_kufar(html_content, city)
    else:
        logger.warning(f"Unknown source type for key: {key}")
        return []

async def scrape_all_sources(user_id: Optional[int] = None) -> List[Dict[str, str]]:
    """Scrapes all configured sources and stores new findings."""
    logger.info(f"--- Starting scheduled scrape cycle (triggered by user: {user_id or 'System'}) ---")
    all_new_listings = []
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Get existing URLs to check for duplicates
            cur.execute("SELECT url FROM listings")
            existing_urls = {row['url'] for row in cur.fetchall()}
            logger.info(f"Found {len(existing_urls)} existing listings in DB.")

            async with aiohttp.ClientSession() as session:
                tasks = [scrape_source(session, key, url) for key, url in SOURCES.items()]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Scraping task failed: {result}", exc_info=result)
                        continue
                    if isinstance(result, list):
                        for listing in result:
                            if listing['url'] not in existing_urls:
                                all_new_listings.append(listing)
                                existing_urls.add(listing['url']) # Add to set to avoid duplicates within this run
                            else:
                                # Update last_seen_at for existing listings if needed (optional)
                                try:
                                    update_sql = sql.SQL("""
                                        UPDATE listings SET last_seen_at = CURRENT_TIMESTAMP
                                        WHERE url = %s
                                    """)
                                    cur.execute(update_sql, (listing['url'],))
                                except Exception as db_update_err:
                                     logger.error(f"Failed to update last_seen_at for {listing['url']}: {db_update_err}")


            logger.info(f"Found {len(all_new_listings)} new listings across all sources.")

            # Insert new listings into the database
            if all_new_listings:
                insert_query = """
                    INSERT INTO listings (url, title, price, source, city, added_at, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (url) DO NOTHING;
                """
                # Prepare data for executemany
                data_to_insert = [
                    (
                        listing['url'],
                        listing['title'],
                        listing['price'],
                        listing['source'],
                        listing['city'],
                    )
                    for listing in all_new_listings
                ]
                try:
                    cur.executemany(insert_query, data_to_insert)
                    conn.commit() # Commit after executemany
                    logger.info(f"Successfully inserted {len(data_to_insert)} new listings into the database.")
                except Exception as db_err:
                     logger.error(f"Database insert failed: {db_err}", exc_info=True)
                     conn.rollback() # Rollback on error


    except psycopg2.Error as db_err:
         logger.error(f"Database error during scraping cycle: {db_err}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error during scraping cycle: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
            logger.info("Database connection closed after scrape cycle.")

    logger.info(f"--- Finished scrape cycle ---")
    return all_new_listings # Return new ones found in this cycle

# --- Telegram Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message with a button to open the Mini App."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username or 'NoUsername'}) started the bot.")

    # !! Replace 'YOUR_WEBAPP_URL' with the actual URL where your Flask app (mini_app.html) will be hosted !!
    # If using a service like Render, this will be your service's URL. If running locally with ngrok, use the ngrok URL.
    # Example for Render: https://your-app-name.onrender.com/mini_app
    # Example for local ngrok: https://your_ngrok_subdomain.ngrok.io/mini_app
    WEBAPP_URL = "https://your-flask-app-url.com/mini_app" # <<< IMPORTANT: SET THIS

    keyboard = [
        [InlineKeyboardButton("🔍 Открыть поиск квартир", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_html(
        rf"Привет, {user.mention_html()}! Нажмите кнопку ниже, чтобы найти квартиры.",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays help information."""
    await update.message.reply_text("Нажмите /start, чтобы открыть приложение для поиска квартир.")

async def set_commands(application: Application) -> None:
    """Sets the bot commands visible in Telegram."""
    commands = [
        BotCommand("start", "🚀 Запустить поиск квартир"),
        BotCommand("help", "ℹ️ Помощь")
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands set successfully.")
    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}")


# --- Flask Web Application (for Mini App Hosting & API) ---
flask_app = Flask(__name__, static_folder='static', static_url_path='/static') # Define static folder

@flask_app.route('/')
def index():
    # Redirect root to mini_app or show a simple status page
    # return "Bot backend is running. Use Telegram to interact."
    return flask_app.send_static_file('mini_app.html')


@flask_app.route('/mini_app')
def mini_app_route():
    """Serves the main Mini App HTML file."""
    # Ensure the file is named mini_app.html and is in the static folder
    return send_from_directory(flask_app.static_folder, 'mini_app.html')

@flask_app.route('/initiate_scrape', methods=['POST'])
async def initiate_scrape_endpoint():
    """Endpoint called by Mini App to trigger scraping."""
    # Optional: Get user ID if needed for user-specific logic later
    user_data = request.json
    user_id = user_data.get('userId') if user_data else None

    logger.info(f"Received scrape initiation request from Mini App (User ID: {user_id})")

    # Run scraping in the background - don't block the request
    asyncio.create_task(scrape_all_sources(user_id=user_id))

    return jsonify({"status": "scrape_initiated"}), 202 # Accepted

@flask_app.route('/get_listings', methods=['GET'])
def get_listings_endpoint():
    """Endpoint called by Mini App to fetch latest listings."""
    # Optional: Add user ID filtering if listings should be user-specific
    # user_id = request.args.get('userId')
    limit = request.args.get('limit', 30, type=int) # Get limit or default to 30
    limit = max(1, min(limit, 100)) # Clamp limit between 1 and 100

    logger.info(f"Received request to get listings (limit: {limit})")
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Fetch latest listings ordered by when they were added
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
            # Convert datetime to string for JSON serialization
            for listing in listings:
                if isinstance(listing.get('added_at'), datetime.datetime):
                     listing['added_at'] = listing['added_at'].isoformat()

        return jsonify({"listings": listings}), 200

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
    config.bind = ["0.0.0.0:8080"]  # Bind to all interfaces on port 8080 (adjust if needed)
    config.use_reloader = False # Important for production/asyncio loop
    config.loglevel = "info"

    logger.info("Starting Hypercorn server for Flask app...")
    try:
        # Pass the Flask app object directly to hypercorn.asyncio.serve
        await hypercorn.asyncio.serve(flask_app, config)
    except Exception as e:
        logger.error(f"Hypercorn server failed: {e}", exc_info=True)
        raise # Re-raise to potentially stop the main loop

# --- Main Application Setup ---
async def post_init(application: Application):
    """Tasks to run after the bot application is initialized."""
    await set_commands(application)
    init_db() # Initialize DB schema on startup
    logger.info("Bot post-initialization complete.")

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
        .post_init(post_init)
        .build()
    )

    # --- Register Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    # Add other handlers if needed (e.g., MessageHandler, CallbackQueryHandler)

    # --- Run Flask App and Bot Polling Concurrently ---
    flask_task = asyncio.create_task(run_flask_app(), name="FlaskHypercornServer")
    polling_task = asyncio.create_task(application.run_polling(allowed_updates=Update.ALL_TYPES), name="TelegramPolling")

    # Keep running until one task fails or is cancelled
    done, pending = await asyncio.wait(
        [flask_task, polling_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Handle task completion/failure
    for task in done:
        try:
            await task # Check for exceptions raised within the task
            logger.info(f"Task {task.get_name()} completed normally.")
        except Exception as e:
            logger.error(f"Task {task.get_name()} failed: {e}", exc_info=True)

    # Cancel pending tasks if one has finished/failed
    for task in pending:
        logger.info(f"Cancelling pending task: {task.get_name()}")
        task.cancel()
        try:
            await task # Wait for cancellation to complete
        except asyncio.CancelledError:
            logger.info(f"Task {task.get_name()} cancelled successfully.")
        except Exception as e:
            logger.error(f"Error during cancellation of task {task.get_name()}: {e}", exc_info=True)

    # Optional: Graceful shutdown for the application object if needed
    # await application.shutdown()

    logger.info("--- Application Shutdown ---")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Application stopped manually (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Application crashed: {e}", exc_info=True)
    finally:
        # Gracefully cancel all running tasks on exit
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        logger.info("Event loop closed.")
