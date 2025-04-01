import asyncio
import logging
import os
from datetime import datetime, timezone
import time
import psycopg2
import psycopg2.extras
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
from pyppeteer import launch

# --- Настройки Логирования ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    logger.error("Не установлена переменная окружения TELEGRAM_TOKEN!")

RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}/webhook" if RENDER_EXTERNAL_HOSTNAME else None
if not WEBHOOK_URL:
    logger.warning("Не удалось определить WEBHOOK_URL из RENDER_EXTERNAL_HOSTNAME.")

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    logger.error("Не установлена переменная окружения DATABASE_URL!")

KUFAR_LIMIT = 7
ONLINER_LIMIT = 7

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не установлена")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}", exc_info=True)
        raise

def init_db():
    create_table_query = """
        CREATE TABLE IF NOT EXISTS ads (
            link TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            city TEXT,
            price INTEGER,
            rooms INTEGER,
            address TEXT,
            image TEXT,
            description TEXT,
            parsed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(create_table_query)
                logger.info("Таблица 'ads' в PostgreSQL успешно проверена/создана.")
    except (psycopg2.Error, ValueError) as e:
        logger.error(f"Ошибка при инициализации БД PostgreSQL: {e}", exc_info=True)

async def parse_kufar(session, city, min_price=None, max_price=None, rooms=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.kufar.by/",
    }
    url = f"https://www.kufar.by/l/r~{city}/kvartiry/snyat"
    params = {"cur": "USD"}
    if rooms:
        params["rooms"] = rooms
    if min_price and max_price:
        params["prc"] = f"r:{min_price},{max_price}"

    logger.info(f"Fetching Kufar ads from: {url} with params {params}")
    start_time = time.time()

    try:
        async with session.get(url, headers=headers, params=params, timeout=20) as response:
            response.raise_for_status()
            html = await response.text()
            soup = BeautifulSoup(html, "html.parser")
            ads_found = []

            listings = soup.select("article[class*='styles_wrapper__']")
            if not listings:
                logger.warning(f"Не найдены блоки объявлений на Kufar: {url}")
                with open(f"kufar_{city}.html", "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info(f"Сохранен HTML Kufar для {city} в kufar_{city}.html")
                return []

            for listing in listings:
                link_tag = listing.select_one("a[class*='styles_link__']")
                link = link_tag.get("href", "") if link_tag else ""
                if not link:
                    continue
                full_link = f"https://www.kufar.by{link}" if not link.startswith("http") else link

                price_text = listing.select_one("span[class*='styles_price__']").text if listing.select_one("span[class*='styles_price__']") else ""
                price = None
                try:
                    price_clean = ''.join(filter(str.isdigit, price_text))
                    price = int(price_clean) if price_clean else None
                except ValueError:
                    logger.warning(f"Некорректная цена в объявлении Kufar: {price_text}")
                    continue

                desc_text = listing.select_one("h3[class*='styles_title__']").text.strip() if listing.select_one("h3[class*='styles_title__']") else ""
                room_count = None
                if "1-комн" in desc_text or "однокомнатная" in desc_text or "1-к" in desc_text:
                    room_count = 1
                elif "2-комн" in desc_text or "двухкомнатная" in desc_text or "2-к" in desc_text:
                    room_count = 2
                elif "3-комн" in desc_text or "трехкомнатная" in desc_text or "3-к" in desc_text:
                    room_count = 3
                elif "4-комн" in desc_text or "четырехкомнатная" in desc_text or "4-к" in desc_text:
                    room_count = 4

                address = listing.select_one("span[class*='styles_address__']").text.strip() if listing.select_one("span[class*='styles_address__']") else "Адрес не указан"
                image = listing.select_one("img[class*='styles_image__']").get("src") if listing.select_one("img[class*='styles_image__']") else None
                description = desc_text or "Описание не указано"

                if not price or (rooms and room_count != int(rooms)) or (min_price and price < min_price) or (max_price and price > max_price):
                    continue

                ad_data = {
                    "link": full_link,
                    "source": "Kufar",
                    "city": city,
                    "price": price,
                    "rooms": room_count,
                    "address": address,
                    "image": image,
                    "description": description,
                }
                ads_found.append(ad_data)

            logger.info(f"Парсинг Kufar завершен за {time.time() - start_time:.2f} сек. Найдено: {len(ads_found)} объявлений.")
            return ads_found[:KUFAR_LIMIT]

    except aiohttp.ClientError as e:
        logger.error(f"Ошибка сети Kufar {url}: {e}")
        return []
    except asyncio.TimeoutError:
        logger.error(f"Таймаут Kufar {url}")
        return []

async def parse_onliner(city, min_price=None, max_price=None, rooms=None):
    ONLINER_CITY_URLS = {
        "minsk": "https://r.onliner.by/ak/#bounds[lb][lat]=53.820922446131&bounds[lb][long]=27.344970703125&bounds[rt][lat]=53.97547425743&bounds[rt][long]=27.77961730957",
        "brest": "https://r.onliner.by/ak/#bounds[lb][lat]=51.941725203142&bounds[lb][long]=23.492889404297&bounds[rt][lat]=52.234528294214&bounds[rt][long]=23.927536010742",
        "grodno": "https://r.onliner.by/ak/#bounds[lb][lat]=53.538267122397&bounds[lb][long]=23.629531860352&bounds[rt][lat]=53.820517109806&bounds[rt][long]=24.064178466797",
        "gomel": "https://r.onliner.by/ak/#bounds[lb][lat]=52.302600726968&bounds[lb][long]=30.732192993164&bounds[rt][lat]=52.593037841157&bounds[rt][long]=31.166839599609",
        "vitebsk": "https://r.onliner.by/ak/#bounds[lb][lat]=55.085834940707&bounds[lb][long]=29.979629516602&bounds[rt][lat]=55.357648391381&bounds[rt][long]=30.414276123047",
        "mogilev": "https://r.onliner.by/ak/#bounds[lb][lat]=53.74261986683&bounds[lb][long]=30.132064819336&bounds[rt][lat]=54.023503252809&bounds[rt][long]=30.566711425781",
    }
    base_url = ONLINER_CITY_URLS.get(city, "")
    if not base_url:
        logger.error(f"Неизвестный город для Onliner: {city}")
        return []

    params = {}
    if rooms:
        params["rent_type[]"] = f"{rooms}_room{'s' if rooms > 1 else ''}"
    if min_price and max_price:
        params["price[min]"] = min_price
        params["price[max]"] = max_price
        params["currency"] = "usd"

    url = f"https://r.onliner.by/ak/?{('&'.join(f'{k}={v}' for k, v in params.items()))}#{base_url.split('#')[1]}" if params else base_url
    logger.info(f"Fetching Onliner ads from: {url}")
    start_time = time.time()

    try:
        browser = await launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox'],
            executablePath=os.environ.get('PUPPETEER_EXECUTABLE_PATH', '/usr/bin/chromium')
        )
        page = await browser.newPage()
        await page.setUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0")
        await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 30000})
        await page.wait_for_selector(".classified", timeout=10000)

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        ads_found = []

        listings = soup.select('a[href*="/ak/apartments/"]')
        if not listings:
            logger.warning(f"Не найдены объявления на Onliner: {url}")
            with open(f"onliner_{city}.html", "w", encoding="utf-8") as f:
                f.write(html)
            logger.info(f"Сохранен HTML Onliner для {city} в onliner_{city}.html")
            await browser.close()
            return []

        for listing in listings:
            link = listing.get("href", "")
            if not link.startswith("https://r.onliner.by/ak/apartments/"):
                continue

            price_text = listing.select_one(".classified__price-value").text.strip() if listing.select_one(".classified__price-value") else ""
            price = None
            try:
                price_clean = ''.join(filter(str.isdigit, price_text))
                price = int(price_clean) if price_clean else None
            except ValueError:
                logger.warning(f"Некорректная цена в объявлении Onliner: {price_text}")
                continue

            rooms_text = listing.select_one(".classified__caption-item_type").text if listing.select_one(".classified__caption-item_type") else ""
            room_count = None
            try:
                rooms_match = [int(d) for d in rooms_text if d.isdigit()]
                room_count = rooms_match[0] if rooms_match else None
            except ValueError:
                logger.warning(f"Некорректное количество комнат в объявлении Onliner: {rooms_text}")

            address = listing.select_one(".classified__caption-item_adress").text.strip() if listing.select_one(".classified__caption-item_adress") else "Адрес не указан"
            image = listing.select_one(".classified__figure img").get("src") if listing.select_one(".classified__figure img") else None
            description = rooms_text or "Описание не указано"

            if not price or (rooms and room_count != int(rooms)) or (min_price and price < min_price) or (max_price and price > max_price):
                continue

            ad_data = {
                "link": link,
                "source": "Onliner",
                "city": city,
                "price": price,
                "rooms": room_count,
                "address": address,
                "image": image,
                "description": description,
            }
            ads_found.append(ad_data)

        await browser.close()
        logger.info(f"Парсинг Onliner завершен за {time.time() - start_time:.2f} сек. Найдено: {len(ads_found)} объявлений.")
        return ads_found[:ONLINER_LIMIT]

    except Exception as e:
        logger.error(f"Ошибка парсинга Onliner {url}: {e}", exc_info=True)
        if 'browser' in locals():
            await browser.close()
        return []

def save_ads_to_db(ads):
    if not ads or not DATABASE_URL:
        return 0

    insert_query = """
        INSERT INTO ads (link, source, city, price, rooms, address, image, description, parsed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (link) DO NOTHING;
    """
    data_to_insert = [
        (
            ad["link"], ad["source"], ad["city"], ad["price"],
            ad["rooms"], ad["address"], ad["image"], ad["description"],
            datetime.now(timezone.utc)
        ) for ad in ads
    ]

    saved_count = 0
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for record in data_to_insert:
                    try:
                        cur.execute(insert_query, record)
                        if cur.rowcount > 0:
                            saved_count += 1
                    except psycopg2.Error as e:
                        logger.error(f"Ошибка сохранения записи {record[0]}: {e}")
                        conn.rollback()
                    else:
                        conn.commit()
        logger.info(f"Сохранено {saved_count} новых объявлений из {len(data_to_insert)}.")
    except (psycopg2.Error, ValueError) as e:
        logger.error(f"Ошибка при сохранении в PostgreSQL: {e}", exc_info=True)

    return saved_count

async def run_parser():
    logger.info("Запуск периодической задачи парсинга...")
    async with aiohttp.ClientSession() as session:
        cities = ["minsk", "vitebsk", "brest", "grodno", "gomel", "mogilev"]
        all_new_ads = []

        for city in cities:
            kufar_task = parse_kufar(session, city)
            onliner_task = parse_onliner(city)
            results = await asyncio.gather(kufar_task, onliner_task, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    all_new_ads.extend(result)
                elif isinstance(result, Exception):
                    logger.error(f"Ошибка в парсере: {result}", exc_info=True)

        logger.info(f"Всего найдено {len(all_new_ads)} объявлений перед сохранением.")
        saved_count = save_ads_to_db(all_new_ads)
        logger.info(f"Парсинг завершен. Сохранено {saved_count} новых объявлений.")

app = Flask(__name__, template_folder='templates')

init_db()

@app.route('/')
def index_redirect():
    return "Flask backend is running. Access Mini App via Telegram."

@app.route('/mini-app')
def mini_app():
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Ошибка при рендеринге шаблона index.html: {e}", exc_info=True)
        return "Ошибка загрузки приложения.", 500

@app.route('/api/ads')
def get_ads():
    if not DATABASE_URL:
        return jsonify({"error": "Database not configured"}), 500

    try:
        city = request.args.get('city', 'minsk')
        min_price = request.args.get('min_price', type=int)
        max_price = request.args.get('max_price', type=int)
        rooms = request.args.get('rooms', type=int)
        offset = request.args.get('offset', default=0, type=int)
        limit = request.args.get('limit', default=7, type=int)

        where_clauses = []
        params = []

        if city:
            where_clauses.append("city = %s")
            params.append(city)
        if min_price is not None:
            where_clauses.append("price >= %s")
            params.append(min_price)
        if max_price is not None:
            where_clauses.append("price <= %s")
            params.append(max_price)
        if rooms is not None:
            where_clauses.append("rooms = %s")
            params.append(rooms)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        data_query = f"""
            SELECT link, source, city, price, rooms, address, image, description, parsed_at
            FROM ads
            WHERE {where_sql}
            ORDER BY parsed_at DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])

        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(data_query, params)
                ads = [dict(row) for row in cur.fetchall()]

        logger.info(f"API: city={city}, price={min_price}-{max_price}, rooms={rooms}, offset={offset}, limit={limit}. Found: {len(ads)}")
        return jsonify({"ads": ads})

    except (psycopg2.Error, ValueError) as e:
        logger.error(f"Ошибка БД PostgreSQL в API /api/ads: {e}", exc_info=True)
        return jsonify({"error": "Database error"}), 500

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

    tg_bot_initialized = True
except ImportError:
    logger.warning("Библиотека python-telegram-bot не установлена.")
    tg_bot_initialized = False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("Привет! Нажми кнопку меню для поиска квартир.")

async def app_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/mini-app" if RENDER_EXTERNAL_HOSTNAME else "URL не определен"
    if update.message:
        await update.message.reply_text(f"Ссылка на Mini App: {app_url}")

async def webhook_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pass

application = None
if tg_bot_initialized and TELEGRAM_TOKEN:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("app", app_link))
    application.add_handler(MessageHandler(filters.ALL, webhook_handler))

async def main():
    await run_parser()
    scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
    scheduler.add_job(run_parser, 'interval', hours=4)
    scheduler.start()
    logger.info("Планировщик задач запущен.")

    if application and WEBHOOK_URL:
        logger.info(f"Установка вебхука Telegram на URL: {WEBHOOK_URL}")
        try:
            await application.initialize()
            await application.bot.delete_webhook(drop_pending_updates=True)
            logger.info("Старый вебхук удален (если был).")
            webhook_set = await application.bot.set_webhook(url=WEBHOOK_URL, allowed_updates=Update.ALL_TYPES)
            if webhook_set:
                webhook_info = await application.bot.get_webhook_info()
                logger.info(f"Вебхук успешно установлен: {webhook_info}")
            await application.start()
            logger.info("Telegram бот инициализирован.")
        except Exception as e:
            logger.error(f"Ошибка при инициализации Telegram бота: {e}", exc_info=True)

if __name__ == '__main__':
    asyncio.run(main())
else:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(main())
    else:
        asyncio.run(main())