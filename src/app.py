import logging
import asyncio
import re
import urllib.parse
import os
from typing import List, Dict, Optional
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import Forbidden, TimedOut
from bs4 import BeautifulSoup
import aiohttp
import hypercorn.asyncio
from hypercorn.config import Config
import psycopg2
from psycopg2.extras import DictCursor
import threading

# Настройки
TELEGRAM_TOKEN = "7846698102:AAFR2bhmjAkPiV-PjtnFIu_oRnzxYPP1xVo"
ADMIN_ID = 7756130972
DATABASE_URL = "postgresql://postgresql_6nv7_user:EQCCcg1l73t8S2g9sfF2LPVx6aA5yZts@dpg-cvlq2pggjchc738o29r0-a.frankfurt-postgres.render.com/postgresql_6nv7"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
REQUEST_TIMEOUT = 10
PARSE_INTERVAL = 30
KUFAR_LIMIT = 7
WEB_APP_URL = "https://jake-3.onrender.com/mini-app"

# Логирование
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Константы городов
CITIES = {
    "minsk": "🏙️ Минск",
    "brest": "🌇 Брест",
    "grodno": "🌃 Гродно",
    "gomel": "🌆 Гомель",
    "vitebsk": "🏙 Витебск",
    "mogilev": "🏞️ Могилев",
}

# Инициализация базы данных PostgreSQL
def init_db():
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS ads CASCADE")
                cur.execute("DROP TABLE IF EXISTS users CASCADE")
                cur.execute("DROP TABLE IF EXISTS pending_listings CASCADE")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ads (
                        link TEXT PRIMARY KEY,
                        source TEXT,
                        city TEXT,
                        price INTEGER,
                        rooms INTEGER,
                        address TEXT,
                        image TEXT,
                        description TEXT,
                        user_id INTEGER
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY,
                        first_name TEXT,
                        last_name TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pending_listings (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER,
                        title TEXT,
                        description TEXT,
                        price INTEGER,
                        rooms TEXT,
                        area INTEGER,
                        city TEXT,
                        address TEXT,
                        images TEXT
                    )
                """)
                conn.commit()
                logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

init_db()

app = Flask(__name__)

class ApartmentParser:
    @staticmethod
    async def fetch_ads(city: str, min_price: Optional[int] = None, max_price: Optional[int] = None, rooms: Optional[int] = None) -> List[Dict]:
        headers = {"User-Agent": USER_AGENT}
        results = []
        url = f"https://re.kufar.by/l/{city}/snyat/kvartiru-dolgosrochno"
        if rooms:
            url += f"/{rooms}k"
        query_params = {"cur": "USD"}
        if min_price and max_price:
            query_params["prc"] = f"r:{min_price},{max_price}"
        url += f"?{urllib.parse.urlencode(query_params, safe=':,')}"
        logger.info(f"Fetching Kufar ads from: {url}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as response:
                    response.raise_for_status()
                    soup = BeautifulSoup(await response.text(), "html.parser")
                    for ad in soup.select("section > a"):
                        try:
                            link = ad.get("href", "")
                            if not link:
                                continue
                            price = ApartmentParser._parse_price(ad)
                            room_count = ApartmentParser._parse_rooms(ad)
                            if ApartmentParser._check_filters(price, room_count, min_price, max_price, rooms):
                                results.append({
                                    "price": price,
                                    "rooms": room_count,
                                    "address": ApartmentParser._parse_address(ad),
                                    "link": link,
                                    "image": ApartmentParser._parse_image(ad),
                                    "description": ApartmentParser._parse_description(ad),
                                    "city": city,
                                    "source": "Kufar"
                                })
                        except Exception as e:
                            logger.error(f"Ошибка парсинга объявления Kufar: {e}")
            except Exception as e:
                logger.error(f"Ошибка запроса Kufar: {e}")
        return results

    @staticmethod
    def _parse_price(ad) -> Optional[int]:
        price_element = ad.select_one(".styles_price__usd__HpXMa")
        return int(re.sub(r"\D", "", price_element.text)) if price_element else None

    @staticmethod
    def _parse_rooms(ad) -> Optional[int]:
        rooms_element = ad.select_one(".styles_parameters__7zKlL")
        match = re.search(r"\d+", rooms_element.text) if rooms_element else None
        return int(match.group()) if match else None

    @staticmethod
    def _parse_address(ad) -> str:
        address_element = ad.select_one(".styles_address__l6Qe_")
        return address_element.text.strip() if address_element else "Адрес не указан 🏠"

    @staticmethod
    def _parse_image(ad) -> Optional[str]:
        image_element = ad.select_one("img")
        return image_element.get("src") if image_element else None

    @staticmethod
    def _parse_description(ad) -> str:
        desc_element = ad.select_one(".styles_body__5BrnC")
        return desc_element.text.strip() if desc_element else "Описание не указано 📝"

    @staticmethod
    def _check_filters(price: Optional[int], rooms: Optional[int], min_price: Optional[int], max_price: Optional[int], target_rooms: Optional[int]) -> bool:
        if price is None:
            return False
        price_valid = (min_price is None or price >= min_price) and (max_price is None or price <= max_price)
        rooms_valid = target_rooms is None or rooms == target_rooms
        return price_valid and rooms_valid

def store_ads(ads: List[Dict]):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                for ad in ads:
                    try:
                        cur.execute(
                            """
                            INSERT INTO ads (link, source, city, price, rooms, address, image, description, user_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (link) DO NOTHING
                            """,
                            (ad["link"], ad["source"], ad["city"], ad["price"], ad["rooms"], ad["address"], ad["image"], ad["description"], ad.get("user_id"))
                        )
                    except Exception as e:
                        logger.error(f"Ошибка сохранения объявления: {e}")
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка подключения к базе данных: {e}")

async def fetch_and_store_ads():
    for city in CITIES.keys():
        logger.info(f"Начало парсинга для города: {city}")
        try:
            kufar_ads = await ApartmentParser.fetch_ads(city)
            logger.info(f"Успешно получено {len(kufar_ads)} объявлений с Kufar для {city}")
            if kufar_ads:
                store_ads(kufar_ads)
                logger.info(f"Сохранено {len(kufar_ads)} объявлений для {city}")
            else:
                logger.warning(f"Нет объявлений для сохранения для {city}")
        except Exception as e:
            logger.error(f"Ошибка парсинга Kufar для {city}: {e}")

@app.route('/api/ads', methods=['GET'])
def get_ads():
    city = request.args.get('city')
    min_price = request.args.get('min_price', type=int)
    max_price = request.args.get('max_price', type=int)
    rooms = request.args.get('rooms', type=int)
    kufar_offset = request.args.get('kufar_offset', default=0, type=int)

    try:
        with psycopg2.connect(DATABASE_URL) as conn:
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
                if rooms is not None:
                    query += " AND rooms = %s"
                    params.append(rooms)

                cur.execute(query, params)
                all_ads = cur.fetchall()

                kufar_ads = [ad for ad in all_ads if ad["source"] == "Kufar"]
                kufar_limit = KUFAR_LIMIT if kufar_offset == 0 else 2
                kufar_slice = kufar_ads[kufar_offset:kufar_offset + kufar_limit]

                result = kufar_slice
                has_more_kufar = len(kufar_ads) > kufar_offset + kufar_limit

                for ad in result:
                    ad['has_more'] = has_more_kufar
                    ad['kufar_offset'] = kufar_offset + len(kufar_slice) if kufar_slice else kufar_offset

                return jsonify(result)
    except Exception as e:
        logger.error(f"Ошибка в /api/ads: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/register_user', methods=['POST'])
def register_user():
    data = request.json
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (id, first_name, last_name) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (data['telegram_id'], data.get('first_name'), data.get('last_name'))
                )
                conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Ошибка в /api/register_user: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/add_listing', methods=['POST'])
async def add_listing():
    try:
        user_id = request.form.get('telegram_id')
        title = request.form.get('title')
        description = request.form.get('description')
        price = int(request.form.get('price'))
        rooms = request.form.get('rooms')
        area = request.form.get('area')
        city = request.form.get('city')
        address = request.form.get('address')
        images = ','.join([file.filename for file in request.files.getlist('photos[]')]) if 'photos[]' in request.files else ''

        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_listings (user_id, title, description, price, rooms, area, city, address, images)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """,
                    (user_id, title, description, price, rooms, area, city, address, images)
                )
                listing_id = cur.fetchone()[0]
                conn.commit()

        bot = Application.builder().token(TELEGRAM_TOKEN).build().bot
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Принять", callback_data=f"approve_{listing_id}"),
             InlineKeyboardButton("Отклонить", callback_data=f"reject_{listing_id}")]
        ])
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"Новое объявление:\n{title}\n{description}\nЦена: ${price}\nКомнаты: {rooms}\nПлощадь: {area} м²\nГород: {city}\nАдрес: {address}",
            reply_markup=keyboard
        )
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Ошибка в /api/add_listing: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

class ApartmentBot:
    def __init__(self, application: Application):
        self.application = application
        self._setup_handlers()

    def _setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))

    async def setup_commands(self):
        commands = [BotCommand("start", "Запустить поиск квартир")]
        await self.application.bot.set_my_commands(commands)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        welcome_message = (
            f"👋 Привет, {user.first_name}!\n"
            "Добро пожаловать в Apartment Bot! Здесь ты можешь найти или добавить объявления о квартирах в Беларуси.\n"
            "Нажми кнопку ниже, чтобы начать поиск:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Открыть поиск квартир", web_app={"url": WEB_APP_URL})]
        ])
        await update.message.reply_text(welcome_message, reply_markup=keyboard)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data
        await query.answer()

        try:
            with psycopg2.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    if data.startswith("approve_"):
                        listing_id = int(data.split("_")[1])
                        cur.execute("SELECT * FROM pending_listings WHERE id = %s", (listing_id,))
                        listing = cur.fetchone()
                        if listing:
                            cur.execute(
                                """
                                INSERT INTO ads (link, source, city, price, rooms, address, image, description, user_id)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (f"user_listing_{listing_id}", "User", listing[7], listing[4], listing[5], listing[8], listing[9], listing[3], listing[1])
                            )
                            cur.execute("DELETE FROM pending_listings WHERE id = %s", (listing_id,))
                            conn.commit()
                            await query.edit_message_text("Объявление одобрено и добавлено!")
                    elif data.startswith("reject_"):
                        listing_id = int(data.split("_")[1])
                        cur.execute("DELETE FROM pending_listings WHERE id = %s", (listing_id,))
                        conn.commit()
                        await query.edit_message_text("Объявление отклонено.")
        except Exception as e:
            logger.error(f"Ошибка в handle_callback: {e}")

@app.route('/')
def index():
    return "Welcome to the Apartment Bot! Go to <a href='/mini-app'>Mini App</a> to search for apartments."

@app.route('/mini-app', strict_slashes=False)
def mini_app():
    current_dir = os.getcwd()
    logger.info(f"Current working directory: {current_dir}")
    logger.info(f"Files in current directory: {os.listdir(current_dir)}")
    
    logger.info("Attempting to open mini_app.html")
    try:
        with open("mini_app.html", "r", encoding="utf-8") as f:
            logger.info("Successfully opened mini_app.html")
            return f.read()
    except FileNotFoundError as e:
        logger.error(f"mini_app.html not found: {e}")
        return """
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <title>Ошибка</title>
        </head>
        <body>
            <h1>Mini App временно недоступен</h1>
            <p>Файл mini_app.html не найден. Обратитесь к администратору.</p>
        </body>
        </html>
        """, 200

async def main():
    # Инициализация Telegram бота
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    bot = ApartmentBot(application)
    await bot.setup_commands()

    # Инициализация планировщика
    scheduler = AsyncIOScheduler()
    scheduler.add_job(fetch_and_store_ads, 'interval', minutes=PARSE_INTERVAL)
    scheduler.start()

    # Выполняем первый парсинг
    await fetch_and_store_ads()

    # Настройка Hypercorn для Flask
    config = Config()
    port = os.environ.get("PORT", "5000")
    config.bind = ["0.0.0.0:" + port]
    config.debug = True
    logger.info(f"Starting Hypercorn on port {port}")

    # Запускаем Telegram-бот в отдельном потоке
    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        finally:
            loop.close()

    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()

    # Запускаем Flask (Hypercorn) в основном цикле событий
    await hypercorn.asyncio.serve(app, config)

    # Ожидаем завершения потока бота
    bot_thread.join()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
