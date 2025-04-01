import asyncio
import logging
import os
# Убираем sqlite3
import random
from datetime import datetime, timezone # Импортируем timezone для TIMESTAMP WITH TIME ZONE

# Добавляем psycopg2
import psycopg2
import psycopg2.extras # Для DictCursor

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

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

# --- Настройки Базы Данных PostgreSQL ---
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    logger.error("Не установлена переменная окружения DATABASE_URL! Свяжите PostgreSQL сервис с этим Web Service на Render.")
    # Можно установить локальный URL для тестов, если нужно
    # DATABASE_URL = "postgresql://postgresql_6nv7_user:EQCCcg1l73t8S2g9sfF2LPVx6aA5yZts@dpg-cvlq2pggjchc738o29r0-a.frankfurt-postgres.render.com/postgresql_6nv7"

KUFAR_LIMIT = 2
# ONLINER_LIMIT = 2

def get_db_connection():
    """Устанавливает соединение с PostgreSQL."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не установлена")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}", exc_info=True)
        raise

def init_db():
    """Инициализирует БД PostgreSQL, создает таблицу, если она не существует."""
    if not DATABASE_URL:
        logger.error("Невозможно инициализировать БД: DATABASE_URL не установлена.")
        return

    # Используем TIMESTAMP WITH TIME ZONE для PostgreSQL
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
        # Используем 'with' для автоматического закрытия соединения и курсора
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(create_table_query)
                # conn.commit() не нужен явно внутри 'with conn:', psycopg2 делает это автоматически
                logger.info("Таблица 'ads' в PostgreSQL успешно проверена/создана.")
    except (psycopg2.Error, ValueError) as e:
        logger.error(f"Ошибка при инициализации БД PostgreSQL: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при инициализации БД: {e}", exc_info=True)


# --- Парсер Kufar (без изменений) ---
async def parse_kufar(session, city_url="https://re.kufar.by/l/minsk/kupit/kvartiru", pages=2):
    """Асинхронно парсит объявления с Kufar."""
    ads_found = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    logger.info(f"Начинаем парсинг Kufar: {city_url}")
    for page in range(1, pages + 1):
        url = f"{city_url}?cur_page={page}"
        try:
            async with session.get(url, headers=headers, timeout=20) as response:
                response.raise_for_status()
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")
                sections = soup.find_all("section") # Пример

                if not sections:
                    logger.warning(f"Не найдены блоки объявлений на Kufar (страница {page}): {url}")
                    break

                logger.info(f"Найдено {len(sections)} потенциальных блоков на стр. {page}")

                for section in sections:
                    try:
                        link_tag = section.find("a", href=lambda href: href and "/vi/" in href)
                        if not link_tag: continue

                        link = link_tag['href']
                        if not link.startswith('http'):
                           link = "https://re.kufar.by" + link

                        price_tag = section.find("span", class_=lambda c: c and 'price' in c)
                        price = None
                        if price_tag:
                            price_text = ''.join(filter(str.isdigit, price_tag.text))
                            if price_text: price = int(price_text)

                        description_tag = section.find("div", class_=lambda c: c and 'description' in c)
                        description = description_tag.text.strip() if description_tag else "Нет описания"

                        rooms = None
                        if "1-комн" in description or "однокомнатная" in description.lower(): rooms = 1
                        elif "2-комн" in description or "двухкомнатная" in description.lower(): rooms = 2
                        elif "3-комн" in description or "трехкомнатная" in description.lower(): rooms = 3
                        elif "4-комн" in description or "четырехкомнатная" in description.lower(): rooms = 4

                        address = "Не указан"

                        img_tag = section.find("img")
                        image = img_tag['data-src'] if img_tag and 'data-src' in img_tag.attrs else (img_tag['src'] if img_tag and 'src' in img_tag.attrs else None)

                        city = city_url.split('/')[4]

                        ad_data = {
                            "link": link, "source": "Kufar", "city": city.capitalize(),
                            "price": price, "rooms": rooms, "address": address,
                            "image": image, "description": description,
                        }
                        ads_found.append(ad_data)

                    except Exception as e:
                        logger.error(f"Ошибка при парсинге блока Kufar: {e}", exc_info=False)
                        continue

            logger.info(f"Kufar, стр. {page}: Найдено {len(ads_found)} объявлений.")
            await asyncio.sleep(random.uniform(1, 3))

        except aiohttp.ClientError as e:
            logger.error(f"Ошибка сети Kufar {url}: {e}")
            break
        except asyncio.TimeoutError:
            logger.error(f"Таймаут Kufar {url}")
            break
        except Exception as e:
            logger.error(f"Ошибка парсинга Kufar {url}: {e}", exc_info=True)
            break

    logger.info(f"Парсинг Kufar завершен. Всего найдено: {len(ads_found)} объявлений.")
    return ads_found

# --- Сохранение в БД PostgreSQL ---
def save_ads_to_db(ads):
    """Сохраняет список объявлений в БД PostgreSQL, используя ON CONFLICT."""
    if not ads or not DATABASE_URL:
        return 0

    # Запрос с ON CONFLICT для атомарной вставки или игнорирования дубликатов
    insert_query = """
        INSERT INTO ads (link, source, city, price, rooms, address, image, description, parsed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (link) DO NOTHING;
    """
    # Подготавливаем данные для вставки
    data_to_insert = [
        (
            ad['link'], ad['source'], ad['city'], ad['price'],
            ad['rooms'], ad['address'], ad['image'], ad['description'],
            datetime.now(timezone.utc) # Используем UTC для parsed_at
        ) for ad in ads
    ]

    if not data_to_insert:
        return 0

    saved_count = 0
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # executemany более эффективен для массовой вставки
                # Но для получения количества вставленных строк нужен другой подход
                # Простой вариант: выполнить execute для каждой строки
                for record in data_to_insert:
                    cur.execute(insert_query, record)
                    # rowcount > 0 означает, что строка была вставлена (а не проигнорирована)
                    if cur.rowcount > 0:
                        saved_count += 1
                # conn.commit() не нужен явно
            logger.info(f"Попытка сохранения {len(data_to_insert)} объявлений. Успешно сохранено (новых): {saved_count}.")
    except (psycopg2.Error, ValueError) as e:
        logger.error(f"Ошибка при сохранении объявлений в PostgreSQL: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при сохранении в БД: {e}", exc_info=True)

    return saved_count

# --- Основная задача парсинга (без изменений) ---
async def run_parser():
    """Запускает парсеры и сохраняет результаты."""
    logger.info("Запуск периодической задачи парсинга...")
    async with aiohttp.ClientSession() as session:
        tasks = [
            parse_kufar(session, city_url="https://re.kufar.by/l/minsk/kupit/kvartiru"),
            parse_kufar(session, city_url="https://re.kufar.by/l/vitebsk/kupit/kvartiru"),
            # parse_onliner(session),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_new_ads = []
        for result in results:
            if isinstance(result, list):
                all_new_ads.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Ошибка в одном из парсеров: {result}", exc_info=True)

        logger.info(f"Всего найдено {len(all_new_ads)} объявлений перед сохранением.")
        saved_count = save_ads_to_db(all_new_ads)
        logger.info(f"Парсинг завершен. Сохранено {saved_count} новых объявлений.")

# --- Flask Приложение ---
app = Flask(__name__, template_folder='templates')

# Инициализируем БД при старте Flask (создаем таблицу если нужно)
init_db()

@app.route('/')
def index_redirect():
    return "Flask backend is running. Access Mini App via Telegram."

@app.route('/mini-app')
def mini_app():
    """Отдает HTML страницу Mini App."""
    try:
        return render_template('index.html')
    except Exception as e:
         logger.error(f"Ошибка при рендеринге шаблона index.html: {e}", exc_info=True)
         return "Ошибка загрузки приложения.", 500

@app.route('/api/ads')
def get_ads():
    """API эндпоинт для получения объявлений из PostgreSQL."""
    if not DATABASE_URL:
        return jsonify({"error": "Database not configured"}), 500

    try:
        city = request.args.get('city')
        min_price = request.args.get('min_price', type=int)
        max_price = request.args.get('max_price', type=int)
        rooms = request.args.get('rooms')
        kufar_offset = request.args.get('kufar_offset', default=0, type=int)
        # onliner_offset = ...

        # Строим WHERE часть запроса динамически
        where_clauses = ["source = %s"] # Начинаем с Kufar
        params = ['Kufar']

        if city and city.lower() != 'любой':
            where_clauses.append("lower(city) = %s")
            params.append(city.lower())
        if min_price is not None:
            where_clauses.append("price >= %s")
            params.append(min_price)
        if max_price is not None:
            where_clauses.append("price <= %s")
            params.append(max_price)
        if rooms:
            if rooms == '4+':
                where_clauses.append("rooms >= 4")
            elif rooms.isdigit():
                where_clauses.append("rooms = %s")
                params.append(int(rooms))

        where_sql = " AND ".join(where_clauses)

        # --- Запрос для получения данных ---
        data_query = f"""
            SELECT link, source, city, price, rooms, address, image, description
            FROM ads
            WHERE {where_sql}
            ORDER BY parsed_at DESC
            LIMIT %s OFFSET %s
        """
        data_params = params + [KUFAR_LIMIT, kufar_offset]

        # --- Запрос для подсчета общего количества ---
        count_query = f"SELECT COUNT(*) FROM ads WHERE {where_sql}"
        count_params = params # Те же параметры фильтрации

        kufar_ads = []
        kufar_total_matching = 0

        with get_db_connection() as conn:
            # Используем DictCursor для получения результатов в виде словарей
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Считаем общее количество
                try:
                    cur.execute(count_query, count_params)
                    kufar_total_matching = cur.fetchone()[0]
                except psycopg2.Error as e:
                    logger.error(f"Ошибка при подсчете Kufar в PostgreSQL: {e}")
                    raise # Передаем ошибку дальше

                # Получаем текущую пачку объявлений
                try:
                    cur.execute(data_query, data_params)
                    # Преобразуем строки DictRow в обычные словари
                    kufar_ads = [dict(row) for row in cur.fetchall()]
                except psycopg2.Error as e:
                    logger.error(f"Ошибка при получении объявлений Kufar из PostgreSQL: {e}")
                    raise # Передаем ошибку дальше

        result_ads = kufar_ads
        next_kufar_offset = kufar_offset + len(kufar_ads)
        has_more = next_kufar_offset < kufar_total_matching

        logger.info(f"API (PG): city={city}, price={min_price}-{max_price}, rooms={rooms}, kuf_off={kufar_offset}. Found: {len(result_ads)}, Total Kufar: {kufar_total_matching}, Has More: {has_more}")

        return jsonify({
            "ads": result_ads,
            "next_kufar_offset": next_kufar_offset,
            "has_more": has_more
        })

    except (psycopg2.Error, ValueError) as e: # Ловим ошибки psycopg2 и ValueError (от get_db_connection)
        logger.error(f"Ошибка БД PostgreSQL в API /api/ads: {e}", exc_info=True)
        return jsonify({"error": "Database error"}), 500
    except Exception as e:
        logger.error(f"Непредвиденная ошибка в API /api/ads: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# --- Настройка Telegram Bot и Webhook (без изменений) ---
try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
    tg_bot_initialized = True
except ImportError:
    logger.warning("Библиотека python-telegram-bot не установлена. Функции бота не будут работать.")
    tg_bot_initialized = False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message: await update.message.reply_text("Привет! Нажми кнопку меню для поиска квартир.")
async def app_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/mini-app" if RENDER_EXTERNAL_HOSTNAME else "URL не определен"
    if update.message: await update.message.reply_text(f"Ссылка на Mini App: {app_url}")
async def webhook_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass

application = None
if tg_bot_initialized and TELEGRAM_TOKEN:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("app", app_link))
    application.add_handler(MessageHandler(filters.ALL, webhook_handler))

# --- Запуск Планировщика и Веб-сервера (без изменений) ---
async def main():
    scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
    scheduler.add_job(run_parser, 'interval', hours=4, next_run_time=datetime.now())
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
            else: logger.error("Не удалось установить вебхук.")
            await application.start()
            logger.info("Telegram бот инициализирован и готов принимать вебхуки.")
        except Exception as e:
            logger.error(f"Ошибка при инициализации Telegram бота или вебхука: {e}", exc_info=True)
    # ... (проверки токена и URL) ...

if __name__ != '__main__':
    loop = asyncio.get_event_loop()
    if loop.is_running(): loop.create_task(main())
    else: asyncio.run(main())
