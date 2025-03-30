# app.py (начало файла)
# -*- coding: utf-8 -*-
import os
from dotenv import load_dotenv

# Загружаем переменные из .env файла в окружение
load_dotenv()
# -*- coding: utf-8 -*- # Указание кодировки для русских комментариев
import logging
import asyncio
import re
import urllib.parse
import sqlite3
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, send_from_directory, abort
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
# from telegram.error import Forbidden, TimedOut # Рассмотрите обработку конкретных ошибок при необходимости
from bs4 import BeautifulSoup
import aiohttp
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
import time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import hypercorn.asyncio
from hypercorn.config import Config
import os
import uuid # Для уникальных имен файлов
from werkzeug.utils import secure_filename # Для безопасных имен файлов
import pytz # Для поддержки часовых поясов в APScheduler
from datetime import datetime # Для времени создания


# --- Конфигурация ---
# --> ЛУЧШАЯ ПРАКТИКА: Загружать из переменных окружения <--
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_FALLBACK_TOKEN") # ВАЖНО: Замените резервное значение или убедитесь, что переменная окружения установлена
WEB_APP_URL = os.environ.get("WEB_APP_URL", "https://YOUR_APP_DOMAIN/mini-app") # ВАЖНО: Установите URL вашего развернутого приложения
DATABASE_NAME = os.environ.get("DATABASE_NAME", "ads.db") # Имя файла БД
UPLOAD_FOLDER = 'uploads' # Папка для загруженных изображений
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'} # Разрешенные расширения файлов

# Настройки скрейпинга (также могут быть переменными окружения)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
REQUEST_TIMEOUT = 15 # Немного увеличен
PARSE_INTERVAL = 30 # Интервал парсинга в минутах
KUFAR_LIMIT = 7 # Лимит объявлений с Куфара за раз
ONLINER_LIMIT = 7 # Лимит объявлений с Онлайнера за раз
SELENIUM_WAIT_TIMEOUT = 15 # Таймаут для явных ожиданий Selenium (увеличен)

# --- Логирование ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Предупреждения, если используются резервные значения
if TELEGRAM_TOKEN == "YOUR_FALLBACK_TOKEN":
    logger.warning("TELEGRAM_TOKEN использует резервное значение. Установите переменную окружения.")
if WEB_APP_URL == "https://YOUR_APP_DOMAIN/mini-app":
     logger.warning("WEB_APP_URL использует резервное значение. Установите переменную окружения.")

# --- Константы ---
CITIES = { # Словарь городов
    "minsk": "🏙️ Минск", "brest": "🌇 Брест", "grodno": "🌃 Гродно",
    "gomel": "🌆 Гомель", "vitebsk": "🏙 Витебск", "mogilev": "🏞️ Могилев",
}
# URL для Онлайнера - базовые, фильтры лучше применять через UI/API Selenium
ONLINER_CITY_URLS = {
    "minsk": "https://r.onliner.by/ak/apartments", # Основная страница аренды
}

# --- База данных ---
def init_db():
    # ВНИМАНИЕ: DROP TABLE удаляет все существующие данные. Используйте с осторожностью.
    # В продакшене рассмотрите использование миграций (например, Alembic) для изменений схемы.
    logger.warning(f"Инициализация базы данных '{DATABASE_NAME}'. Удаление существующей таблицы 'ads', если она есть.")
    with sqlite3.connect(DATABASE_NAME) as conn:
        # conn.execute("DROP TABLE IF EXISTS ads") # Раскомментируйте, только если НУЖНО стирать данные при каждом запуске
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                link TEXT PRIMARY KEY, -- Ссылка на объявление (уникальный ключ)
                source TEXT NOT NULL, -- Источник (Kufar, Onliner, User)
                city TEXT NOT NULL, -- Город (ключ из CITIES)
                price INTEGER, -- Цена в USD
                rooms INTEGER, -- Количество комнат
                address TEXT, -- Адрес
                image TEXT, -- URL основного изображения
                description TEXT, -- Описание
                status TEXT DEFAULT 'approved' CHECK(status IN ('approved', 'pending', 'rejected')), -- Статус (одобрено, ожидает, отклонено)
                new INTEGER DEFAULT 0, -- Флаг нового объявления (1 - новое, 0 - просмотренное)
                user_id TEXT, -- ID пользователя, добавившего объявление (для источника 'User')
                phone TEXT,   -- Телефон для объявлений 'User'
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP -- Время добавления/обновления в БД
            )
        """)
        # --> Добавьте индексы для производительности (раскомментируйте при необходимости) <--
        # logger.info("Создание индексов базы данных...")
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_city_status ON ads (city, status);")
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_price ON ads (price);")
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_rooms ON ads (rooms);")
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_source_status ON ads (source, status);")
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_new_status ON ads (new, status);")
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_created_at ON ads (created_at DESC);") # Индекс для сортировки по дате
        conn.commit()
    logger.info("База данных инициализирована.")

init_db() # Вызов инициализации БД при старте

# --- Приложение Flask ---
# Обслуживание статики из ../client/build (путь относительно скрипта Python)
static_folder_path = os.path.join(os.path.dirname(__file__), '..', 'client', 'build')
app = Flask(__name__, static_folder=static_folder_path, static_url_path='/')

# Создание папки для загрузок, если ее нет
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Функция проверки разрешенных расширений
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Парсеры ---
class ApartmentParser: # Парсер Kufar
    @staticmethod
    async def fetch_ads(city: str, min_price: Optional[int] = None, max_price: Optional[int] = None, rooms: Optional[int] = None) -> List[Dict]:
        headers = {"User-Agent": USER_AGENT}
        results = []
        # Базовый URL для долгосрочной аренды квартир
        # Используем имя города из CITIES для построения пути
        city_path = CITIES.get(city, "minsk").split(' ')[1].lower() # Резервный Минск, если ключ не найден
        base_url = f"https://re.kufar.by/l/{city_path}/snyat/kvartiru-dolgosrochno"

        # Параметры запроса
        query_params = {"cur": "USD", "sort": "lst.d"} # Валюта USD, сортировка по дате добавления (сначала новые)

        if rooms is not None: # Kufar использует путь для комнат
            base_url += f"/{rooms}k"

        # Формирование параметра цены Kufar 'prc=r:min,max'
        price_filters = []
        if min_price is not None: price_filters.append(str(min_price))
        else: price_filters.append("") # Пустая строка для 'от' если задано только 'до'
        if max_price is not None: price_filters.append(str(max_price))
        else: price_filters.append("") # Пустая строка для 'до' если задано только 'от'

        if price_filters[0] or price_filters[1]: # Добавляем параметр цены только если задан min или max
             query_params["prc"] = f"r:{price_filters[0]},{price_filters[1]}"

        url = f"{base_url}?{urllib.parse.urlencode(query_params, safe=':,')}"

        logger.info(f"Запрос объявлений Kufar с URL: {url}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as response:
                    response.raise_for_status() # Проверка на HTTP ошибки (4xx, 5xx)
                    soup = BeautifulSoup(await response.text(), "html.parser")
                    # Более точный селектор для карточек объявлений
                    ad_elements = soup.select("section > a[href^='https://re.kufar.by/vi/']")

                    logger.info(f"Найдено {len(ad_elements)} потенциальных элементов объявлений Kufar для {CITIES[city]}.")
                    count = 0
                    for ad in ad_elements:
                        if count >= KUFAR_LIMIT:
                            logger.info(f"Достигнут лимит {KUFAR_LIMIT} для Kufar ({CITIES[city]}).")
                            break
                        try:
                            link = ad.get("href", "")
                            if not link: continue # Пропустить, если нет ссылки

                            # Парсим основные данные с карточки
                            price = ApartmentParser._parse_price(ad)
                            room_count = ApartmentParser._parse_rooms(ad)

                            # Проверяем фильтры уже *после* парсинга базовой информации
                            # Это позволяет не применять фильтры к некорректно распарсенным карточкам
                            if ApartmentParser._check_filters(price, room_count, min_price, max_price, rooms):
                                results.append({
                                    "price": price,
                                    "rooms": room_count,
                                    "address": ApartmentParser._parse_address(ad),
                                    "link": link,
                                    "image": ApartmentParser._parse_image(ad),
                                    "description": ApartmentParser._parse_description(ad), # Описание с карточки может быть кратким
                                    "city": city, # Используем ключ города
                                    "source": "Kufar",
                                    "status": "approved", # Одобрено по умолчанию для скрейпинга
                                    "new": 1 # Помечаем как новое при первом скрейпинге
                                })
                                count += 1
                        except Exception as e:
                            # Логируем ошибку парсинга конкретной карточки, но продолжаем цикл
                            logger.error(f"Ошибка парсинга отдельного объявления Kufar: {e}", exc_info=False) # Убрал traceback для краткости логов
            except aiohttp.ClientError as e:
                logger.error(f"HTTP Ошибка при запросе Kufar для {CITIES[city]}: {e}")
            except asyncio.TimeoutError:
                 logger.error(f"Тайм-аут при запросе Kufar для {CITIES[city]}")
            except Exception as e:
                logger.error(f"Общая ошибка при запросе/парсинге Kufar для {CITIES[city]}: {e}", exc_info=True)

        logger.info(f"Успешно обработано {len(results)} валидных объявлений Kufar для {CITIES[city]}.")
        return results

    # --- Вспомогательные методы парсинга Kufar (селекторы могут меняться!) ---
    @staticmethod
    def _parse_price(ad) -> Optional[int]:
        try:
            # Селектор может измениться, проверяйте актуальность
            price_element = ad.select_one("div > span[class^='styles_price__']")
            if price_element and "$" in price_element.text:
                price_str = re.sub(r"[^\d]", "", price_element.text)
                return int(price_str) if price_str else None
            # Попытка найти альтернативный селектор, если первый не сработал
            price_alt = ad.select_one("span[class*='--usd']")
            if price_alt:
                 price_str = re.sub(r"[^\d]", "", price_alt.text)
                 return int(price_str) if price_str else None
        except Exception as e:
             logger.warning(f"Не удалось распарсить цену Kufar: {e}")
        return None

    @staticmethod
    def _parse_rooms(ad) -> Optional[int]:
        try:
            # Селектор может измениться
            params_element = ad.select_one("div[class^='styles_parameters__']")
            if params_element:
                match = re.search(r"(\d+)[-\s]комн", params_element.text)
                if match:
                    return int(match.group(1))
                if "студия" in params_element.text.lower():
                    return 0 # Или 1, в зависимости от вашей классификации студий
        except Exception as e:
            logger.warning(f"Не удалось распарсить кол-во комнат Kufar: {e}")
        return None # Возвращаем None, если не удалось определить

    @staticmethod
    def _parse_address(ad) -> str:
         try:
             # Селектор может измениться
             address_element = ad.select_one("div[class^='styles_address__']")
             if address_element:
                 return address_element.text.strip()
         except Exception as e:
             logger.warning(f"Не удалось распарсить адрес Kufar: {e}")
         return "Адрес не указан 🏠"

    @staticmethod
    def _parse_image(ad) -> Optional[str]:
        try:
            image_element = ad.select_one("img[data-testid^='image-']")
            if image_element:
                src = image_element.get("data-src") or image_element.get("src")
                # Простая проверка, что URL похож на реальный
                if src and ("kufar.by" in src or src.startswith('http')):
                    return src
        except Exception as e:
            logger.warning(f"Не удалось распарсить изображение Kufar: {e}")
        return None

    @staticmethod
    def _parse_description(ad) -> str:
         # Полное описание обычно на странице самого объявления. С карточки берем что есть.
         try:
            desc_element = ad.select_one("div[class^='styles_body__']") # Может содержать заголовок/параметры
            if desc_element:
                 return desc_element.text.strip()
         except Exception as e:
             logger.warning(f"Не удалось распарсить краткое описание Kufar: {e}")
         return "Описание не указано 📝" # Возвращаем плейсхолдер

    @staticmethod
    def _check_filters(price: Optional[int], rooms: Optional[int], min_price: Optional[int], max_price: Optional[int], target_rooms: Optional[int]) -> bool:
        # Объявление без цены бесполезно для фильтрации
        if price is None:
             logger.debug("Фильтр: Отброшено объявление без цены.")
             return False

        # Проверка цены
        price_ok = True
        if min_price is not None and price < min_price: price_ok = False
        if max_price is not None and price > max_price: price_ok = False
        if not price_ok:
             logger.debug(f"Фильтр: Отброшено объявление по цене ${price} (Фильтр: {min_price}-{max_price}).")
             return False

        # Проверка комнат (только если фильтр по комнатам активен)
        rooms_ok = True
        if target_rooms is not None:
            if rooms is None: # Если фильтр задан, но комнаты не распознаны - отбросить
                rooms_ok = False
                logger.debug(f"Фильтр: Отброшено объявление, т.к. фильтр по комнатам ({target_rooms}) активен, а комнаты не распознаны.")
            elif rooms != target_rooms: # Если комнаты не совпадают с фильтром - отбросить
                 rooms_ok = False
                 logger.debug(f"Фильтр: Отброшено объявление по комнатам {rooms} (Фильтр: {target_rooms}).")

        return price_ok and rooms_ok


class OnlinerParser: # Парсер Onliner - УЛУЧШЕН с явными ожиданиями Selenium
    @staticmethod
    def fetch_ads(city: str, min_price: Optional[int] = None, max_price: Optional[int] = None, rooms: Optional[int] = None) -> List[Dict]:
        results = []
        base_url = "https://r.onliner.by/ak" # Начинаем с главной страницы аренды
        logger.info(f"Попытка запроса объявлений Onliner для {CITIES[city]} с фильтрами: цена=(${min_price}-${max_price}), комнаты={rooms}")

        chrome_options = Options()
        chrome_options.add_argument("--headless") # Безголовый режим
        chrome_options.add_argument("--no-sandbox") # Часто необходимо в контейнерах
        chrome_options.add_argument("--disable-dev-shm-usage") # Преодолевает проблемы с ограниченными ресурсами
        chrome_options.add_argument(f"user-agent={USER_AGENT}")
        chrome_options.add_argument("--disable-gpu") # Иногда помогает в headless режиме
        chrome_options.add_argument("--window-size=1920,1080") # Установка размера окна

        driver = None # Инициализация переменной driver
        try:
            logger.info("Инициализация Selenium WebDriver...")
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            driver.get(base_url)
            logger.info(f"Открыта страница Onliner: {base_url}")

            # Явное ожидание
            wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)

            # --- Применение фильтров через Selenium ---
            # Эта часть сильно зависит от текущей структуры HTML Onliner и может требовать частых обновлений.
            # Взаимодействие с элементами фильтров обычно надежнее.
            # ВАЖНО: Селекторы XPath/CSS ниже - это ПРИМЕРЫ, их нужно проверять и адаптировать!

            # Пример: Применение фильтра Цены
            if min_price is not None or max_price is not None:
                try:
                    logger.info("Применение фильтра цены...")
                    # Найти и кликнуть кнопку/область фильтра цены
                    # Пример XPath, ищите актуальный селектор!
                    price_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class, 'input-style__pseudo') and contains(., 'Цена')] | //div[contains(@class, 'classifieds-filter-element__label') and contains(., 'Цена')]")))
                    price_button.click()
                    logger.info("Кликнули на область фильтра цены.")
                    await asyncio.sleep(0.7) # Небольшая пауза для появления полей

                    # Ввод минимальной цены
                    if min_price is not None:
                        # Пример XPath, ищите актуальный!
                        min_price_input = wait.until(EC.visibility_of_element_located((By.XPATH, "//input[@data-bind='facet.value.from'] | //input[@placeholder='от']")))
                        min_price_input.clear()
                        min_price_input.send_keys(str(min_price))
                        logger.info(f"Ввели минимальную цену: {min_price}")

                    # Ввод максимальной цены
                    if max_price is not None:
                        # Пример XPath, ищите актуальный!
                        max_price_input = wait.until(EC.visibility_of_element_located((By.XPATH, "//input[@data-bind='facet.value.to'] | //input[@placeholder='до']")))
                        max_price_input.clear()
                        max_price_input.send_keys(str(max_price))
                        logger.info(f"Ввели максимальную цену: {max_price}")

                    # Выбор валюты USD (если не по умолчанию)
                    # Пример XPath, ищите актуальный!
                    usd_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(@class, 'radio_button') and text()='$']//parent::label | //label[contains(.,'$')]")))
                    # Проверяем, не выбран ли уже USD
                    is_selected = False
                    try:
                        is_selected = usd_button.find_element(By.XPATH, ".//input").is_selected()
                    except NoSuchElementException:
                         # Если input не внутри label, может быть другая структура
                         pass
                    if not is_selected:
                         usd_button.click()
                         logger.info("Выбрана валюта USD.")
                    else:
                         logger.info("Валюта USD уже была выбрана.")

                    await asyncio.sleep(0.7) # Пауза для применения фильтров

                    # Закрытие выпадающего списка цены (может не требоваться)
                    # Например, кликнуть на заголовок для закрытия
                    # try:
                    #    header_element = driver.find_element(By.TAG_NAME, 'h1')
                    #    header_element.click()
                    #    logger.info("Закрыли выпадающий список цен (клик по заголовку).")
                    # except: pass # Игнорируем, если не сработало

                except (TimeoutException, NoSuchElementException) as e:
                    logger.warning(f"Не удалось применить фильтр цены Onliner: {e}")
                except Exception as e:
                    logger.error(f"Неожиданная ошибка при применении фильтра цены Onliner: {e}", exc_info=True)

            # Пример: Применение фильтра Комнат
            if rooms is not None:
                try:
                    logger.info(f"Применение фильтра комнат ({rooms})...")
                    # Найти и кликнуть главную кнопку фильтра комнат
                    # Пример XPath, ищите актуальный!
                    rooms_button_main = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class, 'input-style__pseudo') and contains(., 'комнат')] | //div[contains(@class, 'classifieds-filter-element__label') and contains(., 'комнат')]")))
                    rooms_button_main.click()
                    logger.info("Кликнули на область фильтра комнат.")
                    await asyncio.sleep(0.7) # Пауза

                    # Найти и кликнуть чекбокс/кнопку нужного количества комнат
                    # Требуется XPath для конкретного числа комнат (например, для '1-комнатные')
                    # Пример XPath, ищите актуальный! (Учтите разницу: 1-комнатная, 2-комнатные)
                    room_text = f"{rooms}-комнатн" # Общая часть для поиска
                    room_label_xpath = f"//label[contains(@class, 'checkbox-style__label') and contains(., '{room_text}')] | //label[contains(., '{room_text}')]"
                    room_checkbox = wait.until(EC.element_to_be_clickable((By.XPATH, room_label_xpath)))
                    # Проверяем, не выбран ли уже
                    is_selected = False
                    try:
                        is_selected = room_checkbox.find_element(By.XPATH, ".//input").is_selected()
                    except NoSuchElementException: pass

                    if not is_selected:
                        room_checkbox.click()
                        logger.info(f"Кликнули на фильтр для {rooms} комнат.")
                    else:
                         logger.info(f"Фильтр для {rooms} комнат уже был выбран.")

                    await asyncio.sleep(0.7) # Пауза

                    # Закрытие выпадающего списка комнат (может не требоваться)
                    # try:
                    #    header_element = driver.find_element(By.TAG_NAME, 'h1')
                    #    header_element.click()
                    #    logger.info("Закрыли выпадающий список комнат (клик по заголовку).")
                    # except: pass

                except (TimeoutException, NoSuchElementException) as e:
                     logger.warning(f"Не удалось применить фильтр комнат Onliner для {rooms}: {e}")
                except Exception as e:
                     logger.error(f"Неожиданная ошибка при применении фильтра комнат Onliner: {e}", exc_info=True)

            # Пример: Применение фильтра Города
            # На Onliner это часто делается через карту или поиск местоположения.
            if city and city in CITIES: # Применяем только если город задан и валиден
                try:
                    logger.info(f"Применение фильтра города: {CITIES[city]}...")
                    # Найти поле ввода местоположения
                    # Пример XPath, ищите актуальный!
                    location_input = wait.until(EC.visibility_of_element_located((By.XPATH, "//input[contains(@placeholder, 'Любой регион') or contains(@placeholder, 'адрес')]")))
                    location_input.click() # Клик для активации
                    await asyncio.sleep(0.3)
                    location_input.clear()
                    # Отправляем только имя города без эмодзи
                    city_name_only = CITIES[city].split(" ")[1]
                    location_input.send_keys(city_name_only)
                    logger.info(f"Ввели название города: {city_name_only}")
                    await asyncio.sleep(1.5) # Ждем появления подсказок

                    # Найти и кликнуть на подходящую подсказку
                    # Пример XPath, ищите актуальный!
                    suggestion_xpath = f"//div[contains(@class, 'classifieds-filter-location__item') and contains(., '{city_name_only}')] | //li[contains(., '{city_name_only}') and contains(@class, 'suggest')]"
                    city_suggestion = wait.until(EC.element_to_be_clickable((By.XPATH, suggestion_xpath)))
                    city_suggestion.click()
                    logger.info(f"Выбрана подсказка для города: {city_name_only}.")
                    await asyncio.sleep(1.5) # Ждем применения фильтра и перезагрузки результатов

                except (TimeoutException, NoSuchElementException) as e:
                    logger.warning(f"Не удалось применить фильтр города Onliner для {CITIES[city]}: {e}. Результаты могут включать другие города.")
                except Exception as e:
                    logger.error(f"Неожиданная ошибка при применении фильтра города Onliner: {e}", exc_info=True)


            # --- Ожидание загрузки результатов после применения фильтров ---
            logger.info("Ожидание загрузки отфильтрованных объявлений...")
            try:
                 # Ждем контейнер с результатами (настройте селектор!)
                 # Пример CSS селектора
                results_container_selector = "div.classifieds__list" # или "div#resultsTable" и т.п.
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, results_container_selector)))
                 # Дополнительно ждем появления хотя бы одной ссылки на объявление
                 # Пример CSS селектора
                ad_link_selector = "a.classified" # или "a.result__link" и т.п.
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ad_link_selector)))
                logger.info("Контейнер с объявлениями и объявления загружены.")
                await asyncio.sleep(2) # Дополнительная пауза на случай ленивой загрузки внутри списка
            except TimeoutException:
                 logger.warning(f"Тайм-аут ожидания результатов объявлений Onliner для {CITIES[city]} после фильтрации. Объявлений нет или страница не обновилась.")
                 # Если результатов нет, завершаем работу для этого города
                 # Важно выйти из `try...finally` корректно
                 if driver: driver.quit()
                 return results # Возвращаем пустой список


            # --- Парсинг загруженных объявлений ---
            soup = BeautifulSoup(driver.page_source, "html.parser")
            # Селектор должен указывать на отдельные контейнеры/ссылки объявлений в списке
            # Пример CSS селектора, ищите актуальный!
            ad_elements = soup.select("div.classifieds__item div.classified") # или "li.result" и т.п.
            logger.info(f"Найдено {len(ad_elements)} потенциальных элементов объявлений Onliner после фильтрации.")

            count = 0
            for ad_element in ad_elements:
                 if count >= ONLINER_LIMIT:
                     logger.info(f"Достигнут лимит {ONLINER_LIMIT} для Onliner ({CITIES[city]}).")
                     break
                 try:
                     # Извлечение данных из найденных элементов
                     # Пример CSS селектора, ищите актуальный!
                     link_tag = ad_element.select_one("a.classified__link")
                     link = link_tag.get("href") if link_tag else None
                     # Проверяем, что ссылка ведет на страницу квартиры
                     if not link or not link.startswith("/ak/apartments/"): continue

                     full_link = f"https://r.onliner.by{link}" # Собираем полный URL

                     # Парсим данные с карточки
                     price = OnlinerParser._parse_price(ad_element)
                     room_count = OnlinerParser._parse_rooms(ad_element)

                     # Повторная проверка фильтров (опционально, т.к. Selenium уже отфильтровал)
                     # if not OnlinerParser._check_filters(price, room_count, min_price, max_price, rooms):
                     #     continue

                     results.append({
                         "price": price,
                         "rooms": room_count,
                         "address": OnlinerParser._parse_address(ad_element),
                         "link": full_link,
                         "image": OnlinerParser._parse_image(ad_element),
                         "description": OnlinerParser._parse_description(ad_element),
                         "city": city, # Город считаем верным после фильтрации
                         "source": "Onliner",
                         "status": "approved",
                         "new": 1 # Помечаем как новое
                     })
                     count += 1
                 except Exception as e:
                      logger.error(f"Ошибка парсинга отдельного объявления Onliner: {e}", exc_info=False)

            logger.info(f"Успешно обработано {len(results)} валидных объявлений Onliner для {CITIES[city]}.")

        except TimeoutException:
             logger.error(f"Тайм-аут во время операций Selenium для Onliner ({CITIES[city]}).")
        except Exception as e:
             logger.error(f"Ошибка при запросе/парсинге Onliner для {CITIES[city]} с использованием Selenium: {e}", exc_info=True)
        finally:
             if driver:
                 driver.quit()
                 logger.info("Закрыли Selenium WebDriver.")
        return results


    # --- Вспомогательные методы парсинга Onliner (селекторы требуют обновления!) ---
    @staticmethod
    def _parse_price(ad) -> Optional[int]:
        try:
            # Цена на Онлайнере часто разделена, ищем часть с USD
            # Пример селектора, ищите актуальный!
            price_container = ad.select_one(".classified__price-value")
            if price_container:
                # Ищем спан с ценой в $
                usd_price_span = price_container.select_one("span:nth-of-type(1)") # Предполагаем, что первый спан - цена в USD
                if usd_price_span:
                    price_text = usd_price_span.text.strip()
                    # Удаляем символ '$', пробелы и т.д., берем только цифры
                    price_str = re.sub(r"[^\d]", "", price_text.split('&')[0]) # Учитываем возможные лишние символы
                    return int(price_str) if price_str else None
        except Exception as e:
            logger.warning(f"Не удалось распарсить цену Onliner: {e}")
        return None

    @staticmethod
    def _parse_rooms(ad) -> Optional[int]:
        try:
            # Ищем информацию о комнатах в описании/заголовке карточки
            # Пример селектора, ищите актуальный!
            caption_element = ad.select_one(".classified__caption") # или ".result__header"
            if caption_element:
                # Пример: "1-комнатная квартира"
                match = re.search(r"(\d+)[-\s]комнат", caption_element.text)
                if match:
                    return int(match.group(1))
                if "студия" in caption_element.text.lower():
                    return 0 # Или 1
        except Exception as e:
            logger.warning(f"Не удалось распарсить кол-во комнат Onliner: {e}")
        return None

    @staticmethod
    def _parse_address(ad) -> str:
        try:
            # Адрес часто находится внутри ссылки на карту
            # Пример селектора, ищите актуальный!
            address_element = ad.select_one(".classified__plain-text a[href*='/maps/'] span") # или ".address"
            if address_element:
                return address_element.text.strip()
            # Резервный вариант, если специфичный спан не найден
            address_container = ad.select_one(".classified__plain-text")
            if address_container:
                 # Пытаемся извлечь текст, избегая тегов script/style
                 return address_container.get_text(separator=" ", strip=True).split(',')[0] # Берем первую часть как предположение адреса
        except Exception as e:
             logger.warning(f"Не удалось распарсить адрес Onliner: {e}")
        return "Адрес не указан 🏠"

    @staticmethod
    def _parse_image(ad) -> Optional[str]:
        try:
            # Селектор для основного изображения
            # Пример селектора, ищите актуальный!
            image_element = ad.select_one(".classified__photo img") # или "img.result__image"
            if image_element:
                src = image_element.get("src") or image_element.get("data-src")
                if src and src.startswith('http'): # Простая валидация
                    return src
        except Exception as e:
            logger.warning(f"Не удалось распарсить изображение Onliner: {e}")
        return None

    @staticmethod
    def _parse_description(ad) -> str:
        # Получаем заголовок/основную часть описания с карточки
        try:
             # Пример селектора, ищите актуальный!
             desc_element = ad.select_one(".classified__caption")
             if desc_element:
                 title = desc_element.select_one("span") # Часто здесь комнаты
                 area_info = desc_element.select_one(".classified__caption-item_area") # Пример: получить площадь
                 # Собираем текст из разных частей
                 text_parts = [t for t in [title.text.strip() if title else None, area_info.text.strip() if area_info else None] if t]
                 return ", ".join(text_parts) if text_parts else "Описание не указано 📝"
        except Exception as e:
            logger.warning(f"Не удалось распарсить краткое описание Onliner: {e}")
        return "Описание не указано 📝"

    @staticmethod
    def _check_filters(price: Optional[int], rooms: Optional[int], min_price: Optional[int], max_price: Optional[int], target_rooms: Optional[int]) -> bool:
        # Та же логика, что и у Kufar, оставлена для консистентности
        if price is None: return False
        price_valid = (min_price is None or price >= min_price) and \
                      (max_price is None or price <= max_price)
        rooms_valid = True
        if target_rooms is not None:
            if rooms is None: rooms_valid = False
            elif rooms != target_rooms: rooms_valid = False
        return price_valid and rooms_valid

# --- Хранение данных ---
def store_ads(ads: List[Dict]):
    """Сохраняет список объявлений в БД, игнорируя дубликаты по link."""
    if not ads:
        return 0 # Нечего сохранять
    inserted_count = 0 # Счетчик реально добавленных новых объявлений
    with sqlite3.connect(DATABASE_NAME) as conn:
        cursor = conn.cursor()
        for ad in ads:
            # Пропускаем объявления без ссылки - они бесполезны
            if not ad.get("link"):
                 logger.warning("Пропущено объявление без ссылки.")
                 continue
            try:
                # Используем INSERT OR IGNORE для избежания дубликатов по PRIMARY KEY 'link'
                # Флаг 'new' устанавливаем в 1 только при реальной вставке новой записи
                # Добавляем время создания (CURRENT_TIMESTAMP по умолчанию)
                cursor.execute(
                     """
                     INSERT OR IGNORE INTO ads (link, source, city, price, rooms, address, image, description, status, new, user_id, phone)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                     """,
                    (
                        ad.get("link"), ad.get("source"), ad.get("city"), ad.get("price"), ad.get("rooms"),
                        ad.get("address"), ad.get("image"), ad.get("description"),
                        ad.get("status", "approved"), # Статус 'approved' для спарсенных
                        ad.get("user_id"), ad.get("phone") # Будут None для спарсенных
                    )
                )
                # rowcount > 0 означает, что была произведена вставка (а не ignore)
                if cursor.rowcount > 0:
                     inserted_count += 1
            except sqlite3.Error as e:
                 # Логируем ошибки БД, если они возникают несмотря на ON CONFLICT
                 logger.error(f"Ошибка БД при сохранении объявления {ad.get('link')}: {e}")
            except Exception as e:
                 # Ловим другие возможные ошибки (например, проблемы со словарем ad)
                 logger.error(f"Неожиданная ошибка при обработке объявления для сохранения {ad.get('link')}: {e}", exc_info=True)
        conn.commit() # Фиксируем транзакцию
        logger.info(f"Попытка сохранить {len(ads)} объявлений. Успешно вставлено {inserted_count} новых.")
    return inserted_count

# --- Периодическая задача ---
async def fetch_and_store_ads():
    """Основной цикл получения и сохранения объявлений для всех городов."""
    logger.info("Запуск цикла периодического получения объявлений.")
    total_new_ads_found = 0
    for city_key in CITIES.keys():
        logger.info(f"--- Получение объявлений для города: {CITIES[city_key]} ---")
        kufar_ads = []
        onliner_ads = []

        # Получение с Kufar
        try:
            # Для периодического общего сбора используем без фильтров (или с базовыми)
            kufar_ads = await ApartmentParser.fetch_ads(city_key)
            logger.info(f"Получено {len(kufar_ads)} объявлений с Kufar для {CITIES[city_key]}")
        except Exception as e:
            logger.error(f"Ошибка в задаче получения Kufar для {CITIES[city_key]}: {e}", exc_info=True)

        # Получение с Onliner (в отдельном потоке)
        try:
            onliner_ads = await asyncio.to_thread(OnlinerParser.fetch_ads, city_key)
            logger.info(f"Получено {len(onliner_ads)} объявлений с Onliner для {CITIES[city_key]}")
        except Exception as e:
             logger.error(f"Ошибка в задаче получения Onliner для {CITIES[city_key]}: {e}", exc_info=True)

        # Объединение и сохранение
        all_ads_for_city = kufar_ads + onliner_ads
        if all_ads_for_city:
             inserted_count = store_ads(all_ads_for_city)
             total_new_ads_found += inserted_count
             logger.info(f"Сохранены объявления для {CITIES[city_key]}. Добавлено новых: {inserted_count}")
        else:
            logger.info(f"Объявления для {CITIES[city_key]} в этом цикле не найдены или не получены.")

        await asyncio.sleep(3) # Небольшая задержка между городами, чтобы не нагружать сайты

    logger.info(f"Завершен цикл периодического получения объявлений. Всего найдено новых объявлений: {total_new_ads_found}")


# --- API Эндпоинты ---
@app.route('/api/ads', methods=['GET'])
def get_ads_api():
    """API для получения списка объявлений с фильтрацией и пагинацией."""
    # Получение параметров фильтрации из строки запроса
    city = request.args.get('city') # Ключ города (minsk, brest, ...)
    min_price = request.args.get('min_price', type=int)
    max_price = request.args.get('max_price', type=int)
    rooms_str = request.args.get('rooms')
    rooms = int(rooms_str) if rooms_str and rooms_str.isdigit() else None

    # Параметры пагинации (смещения) для "бесконечной" прокрутки или "загрузить еще"
    kufar_offset = request.args.get('kufar_offset', default=0, type=int)
    onliner_offset = request.args.get('onliner_offset', default=0, type=int)
    user_offset = request.args.get('user_offset', default=0, type=int)

    # Лимиты: сколько загружать при первом запросе и при "загрузить еще"
    initial_limit = 7 # Больше при первой загрузке
    load_more_limit = 5 # Меньше при последующих

    kufar_limit = initial_limit if kufar_offset == 0 else load_more_limit
    onliner_limit = initial_limit if onliner_offset == 0 else load_more_limit
    user_limit = initial_limit if user_offset == 0 else load_more_limit

    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            conn.row_factory = sqlite3.Row # Возвращать строки как словари
            cursor = conn.cursor()

            # Базовая часть запроса для одобренных объявлений
            base_query = "FROM ads WHERE status = 'approved'"
            params = [] # Список параметров для SQL запроса

            # Применение фильтров
            if city and city in CITIES: # Фильтруем только по валидным ключам городов
                base_query += " AND city = ?"
                params.append(city)
            if min_price is not None:
                base_query += " AND price >= ?"
                params.append(min_price)
            if max_price is not None:
                base_query += " AND price <= ?"
                params.append(max_price)
            if rooms is not None:
                base_query += " AND rooms = ?"
                params.append(rooms)

            # Получение объявлений Kufar
            kufar_query = f"SELECT * {base_query} AND source = 'Kufar' ORDER BY created_at DESC LIMIT ? OFFSET ?"
            cursor.execute(kufar_query, params + [kufar_limit, kufar_offset])
            kufar_ads = [dict(row) for row in cursor.fetchall()]

            # Получение объявлений Onliner
            onliner_query = f"SELECT * {base_query} AND source = 'Onliner' ORDER BY created_at DESC LIMIT ? OFFSET ?"
            cursor.execute(onliner_query, params + [onliner_limit, onliner_offset])
            onliner_ads = [dict(row) for row in cursor.fetchall()]

            # Получение объявлений User
            user_query = f"SELECT * {base_query} AND source = 'User' ORDER BY created_at DESC LIMIT ? OFFSET ?"
            cursor.execute(user_query, params + [user_limit, user_offset])
            user_ads = [dict(row) for row in cursor.fetchall()]

            # Объединение результатов (можно перемешивать или оставить по источникам)
            # Простой вариант: Kufar, затем Onliner, затем User
            result_ads = kufar_ads + onliner_ads + user_ads

            # Проверка, есть ли еще объявления для каждого источника (для кнопки "Загрузить еще")
            # Проверяем, существует ли хотя бы одно объявление после текущего смещения + лимита
            kufar_has_more_query = f"SELECT 1 {base_query} AND source = 'Kufar' LIMIT 1 OFFSET ?"
            cursor.execute(kufar_has_more_query, params + [kufar_offset + len(kufar_ads)]) # Используем len(kufar_ads) т.к. реальное кол-во может быть меньше лимита
            kufar_has_more = cursor.fetchone() is not None

            onliner_has_more_query = f"SELECT 1 {base_query} AND source = 'Onliner' LIMIT 1 OFFSET ?"
            cursor.execute(onliner_has_more_query, params + [onliner_offset + len(onliner_ads)])
            onliner_has_more = cursor.fetchone() is not None

            user_has_more_query = f"SELECT 1 {base_query} AND source = 'User' LIMIT 1 OFFSET ?"
            cursor.execute(user_has_more_query, params + [user_offset + len(user_ads)])
            user_has_more = cursor.fetchone() is not None

            # Формирование ответа
            response = {
                "ads": result_ads,
                "next_offsets": { # Следующие смещения для фронтенда
                    "kufar": kufar_offset + len(kufar_ads) if kufar_has_more else None, # None если больше нет
                    "onliner": onliner_offset + len(onliner_ads) if onliner_has_more else None,
                    "user": user_offset + len(user_ads) if user_has_more else None,
                },
                "has_more": kufar_has_more or onliner_has_more or user_has_more # Общий флаг наличия доп. объявлений
            }

            return jsonify(response)

    except sqlite3.Error as e:
         logger.error(f"Ошибка базы данных в /api/ads: {e}")
         return jsonify({"error": "Ошибка базы данных"}), 500
    except Exception as e:
         logger.error(f"Неожиданная ошибка в /api/ads: {e}", exc_info=True)
         return jsonify({"error": "Внутренняя ошибка сервера"}), 500


@app.route('/api/new_ads_count', methods=['GET'])
def get_new_ads_count_api():
    """Возвращает количество новых одобренных объявлений."""
    user_id = request.args.get('user_id') # Опционально: можно будет фильтровать для конкретного пользователя
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            # Считаем только одобренные и новые
            cursor.execute("SELECT COUNT(*) FROM ads WHERE status = 'approved' AND new = 1")
            count = cursor.fetchone()[0]
            return jsonify({"count": count})
    except sqlite3.Error as e:
         logger.error(f"Ошибка базы данных в /api/new_ads_count: {e}")
         return jsonify({"error": "Ошибка базы данных"}), 500
    except Exception as e:
         logger.error(f"Неожиданная ошибка в /api/new_ads_count: {e}", exc_info=True)
         return jsonify({"error": "Внутренняя ошибка сервера"}), 500


@app.route('/api/mark_ads_viewed', methods=['POST'])
def mark_ads_viewed_api():
    """Сбрасывает флаг 'new' для всех новых одобренных объявлений."""
    user_id = request.args.get('user_id') # Опционально: можно будет фильтровать для пользователя
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            # Обновляем только одобренные и новые
            cursor.execute("UPDATE ads SET new = 0 WHERE status = 'approved' AND new = 1")
            updated_count = cursor.rowcount # Количество обновленных строк
            conn.commit()
            logger.info(f"Отмечено как просмотренные {updated_count} объявлений.")
            return jsonify({"message": f"{updated_count} объявлений отмечено как просмотренные"}), 200
    except sqlite3.Error as e:
         logger.error(f"Ошибка базы данных в /api/mark_ads_viewed: {e}")
         return jsonify({"error": "Ошибка базы данных"}), 500
    except Exception as e:
         logger.error(f"Неожиданная ошибка в /api/mark_ads_viewed: {e}", exc_info=True)
         return jsonify({"error": "Внутренняя ошибка сервера"}), 500


@app.route('/api/submit_user_ad', methods=['POST'])
def submit_user_ad_api():
    """Принимает данные формы для нового объявления от пользователя."""
    # --- Базовая валидация данных формы ---
    user_id = request.form.get('user_id') # ID пользователя из формы (фронтенд должен его передать)
    city = request.form.get('city')
    rooms_str = request.form.get('rooms')
    price_str = request.form.get('price')
    address = request.form.get('address')
    description = request.form.get('description')
    phone = request.form.get('phone') # Телефон важен для пользовательских объявлений

    errors = {} # Словарь для ошибок валидации
    if not user_id: errors['user_id'] = "Отсутствует ID пользователя." # Должен приходить безопасно из Telegram WebApp InitData
    if not city or city not in CITIES: errors['city'] = "Требуется указать допустимый город."
    if not rooms_str or not rooms_str.isdigit() or not (0 <= int(rooms_str) <= 10): errors['rooms'] = "Требуется указать количество комнат (0-10)."
    if not price_str or not price_str.isdigit() or int(price_str) <= 0: errors['price'] = "Требуется указать корректную цену."
    if not address or len(address) < 5: errors['address'] = "Требуется указать адрес (мин. 5 символов)."
    if not description or len(description) < 10: errors['description'] = "Требуется указать описание (мин. 10 символов)."
    # Простая валидация телефона (цифры, возможно +, скобки, дефисы)
    if not phone or not re.match(r"^[\d\+\-\(\)\s]+$", phone) or len(phone) < 7: errors['phone'] = "Требуется указать корректный номер телефона."

    images = request.files.getlist('images') # Получаем список файлов
    # if not images: errors['images'] = "Рекомендуется загрузить хотя бы одно фото." # Сделать предупреждением или требованием?

    if errors:
        logger.warning(f"Ошибка валидации при подаче объявления пользователем {user_id}: {errors}")
        return jsonify({"error": "Ошибка валидации", "details": errors}), 400 # 400 Bad Request

    # Преобразование типов после валидации
    rooms = int(rooms_str)
    price = int(price_str)

    # --- Обработка файлов ---
    image_paths = [] # Сохраняем относительные пути для БД
    MAX_IMAGES = 5 # Максимальное кол-во изображений
    if len(images) > MAX_IMAGES:
         logger.warning(f"Пользователь {user_id} попытался загрузить {len(images)} изображений (макс. {MAX_IMAGES}).")
         return jsonify({"error": "Ошибка валидации", "details": {"images": f"Можно загрузить не более {MAX_IMAGES} изображений."}}), 400


    for image in images:
        if image and image.filename and allowed_file(image.filename):
            # Очистка имени файла и генерация уникального имени
            original_filename = secure_filename(image.filename)
            ext = original_filename.rsplit('.', 1)[1].lower()
            unique_filename = f"{uuid.uuid4()}.{ext}"
            try:
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                # TODO: Добавить проверку размера файла перед сохранением
                # TODO: Рассмотреть сжатие/оптимизацию изображений перед сохранением
                image.save(filepath)
                image_paths.append(f"/{UPLOAD_FOLDER}/{unique_filename}") # Сохраняем URL-путь
                logger.info(f"Сохранено загруженное изображение: {filepath}")
            except Exception as e:
                 logger.error(f"Не удалось сохранить загруженный файл {original_filename}: {e}")
                 # Решить, прерывать ли всю отправку или продолжить без этого изображения
                 return jsonify({"error": "Не удалось сохранить изображение"}), 500
        elif image and image.filename:
             # Логировать или сообщить пользователю о недопустимом типе файла
             logger.warning(f"Загружен файл недопустимого типа: {image.filename}")
             # Можно добавить ошибку в 'errors' и вернуть 400

    # --- Создание словаря объявления ---
    # Генерируем уникальную ссылку для пользовательских объявлений
    ad_link = f"user_ad_{uuid.uuid4()}"

    ad = {
        "link": ad_link,
        "source": "User",
        "city": city,
        "price": price,
        "rooms": rooms,
        "address": address,
        # Сохраняем путь к первому изображению, или обрабатываем несколько (например, сохраняем JSON-список)
        "image": image_paths[0] if image_paths else None,
        # TODO: Сохранить все пути к изображениям (например, в отдельном поле JSON)
        "description": description,
        "status": "pending", # Пользовательские объявления требуют модерации
        "new": 0, # Не считаются 'новыми' с точки зрения скрейпинга
        "user_id": user_id,
        "phone": phone
    }

    # --- Сохранение в базу данных ---
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
             cursor = conn.cursor()
             # Используем CURRENT_TIMESTAMP для created_at
             cursor.execute(
                """
                INSERT INTO ads (link, source, city, price, rooms, address, image, description, status, new, user_id, phone, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (ad["link"], ad["source"], ad["city"], ad["price"], ad["rooms"], ad["address"], ad["image"], ad["description"], ad["status"], ad["new"], ad["user_id"], ad["phone"])
             )
             conn.commit()
             logger.info(f"Пользовательское объявление {ad_link} от {user_id} отправлено и ожидает модерации.")
             # TODO: Уведомить администратора/модератора о новом ожидающем объявлении
             return jsonify({"status": "pending", "message": "Объявление отправлено на модерацию ✅"}), 201 # 201 Created

    except sqlite3.Error as e:
        logger.error(f"Ошибка БД при отправке пользовательского объявления {ad_link}: {e}")
        # Удалить сохраненные изображения, если вставка в БД не удалась?
        for img_path in image_paths:
             try: os.remove(img_path.lstrip('/'))
             except OSError: pass
        return jsonify({"error": "Ошибка базы данных при отправке"}), 500
    except Exception as e:
        logger.error(f"Неожиданная ошибка при отправке пользовательского объявления {ad_link}: {e}", exc_info=True)
        # Очистка изображений
        for img_path in image_paths:
             try: os.remove(img_path.lstrip('/'))
             except OSError: pass
        return jsonify({"error": "Внутренняя ошибка сервера при отправке"}), 500


# --- Эндпоинты модерации (Требуют Аутентификации/Авторизации!) ---
# ВНИМАНИЕ: Это заглушки. Добавьте проверки, чтобы только авторизованные пользователи могли их вызывать.
def is_user_admin(user_id):
     """Проверяет, является ли пользователь администратором (ЗАГЛУШКА)."""
     # Placeholder: Реализуйте реальную проверку (например, по списку админов в БД/конфиге)
     # НИКОГДА не полагайтесь только на user_id, отправленный клиентом, без верификации (например, через валидацию Telegram InitData)
     ADMIN_IDS = os.environ.get("ADMIN_IDS", "").split(',') # Получаем ID админов из переменной окружения
     logger.info(f"Проверка прав администратора для пользователя {user_id}. Список админов: {ADMIN_IDS}")
     return str(user_id) in ADMIN_IDS

@app.route('/api/moderate_ad', methods=['POST'])
def moderate_ad_api():
    """Одобряет или отклоняет объявление."""
    ad_link = request.args.get('link') # Используем link как уникальный ID
    action = request.args.get('action') # 'approve' или 'reject'
    moderator_id = request.args.get('moderator_id') # ID пользователя, выполняющего действие

    # ---> !! ВАЖНО: Добавьте проверку аутентификации/авторизации !! <---
    if not moderator_id or not is_user_admin(moderator_id):
         logger.warning(f"Неавторизованная попытка модерации объявления {ad_link} пользователем {moderator_id}")
         return jsonify({"error": "Неавторизованный доступ"}), 403 # 403 Forbidden

    if not ad_link or not action or action not in ['approve', 'reject']:
        return jsonify({"error": "Отсутствует 'link' или неверный параметр 'action'"}), 400

    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            if action == 'approve':
                 # При одобрении помечаем как 'новое', чтобы пользователи его увидели
                cursor.execute("UPDATE ads SET status = 'approved', new = 1 WHERE link = ? AND status = 'pending'", (ad_link,))
                message = "Объявление одобрено и помечено как новое."
            elif action == 'reject':
                # Вариант 1: Установить статус 'rejected' (сохраняет запись)
                # cursor.execute("UPDATE ads SET status = 'rejected' WHERE link = ? AND status = 'pending'", (ad_link,))
                # Вариант 2: Удалить объявление полностью
                cursor.execute("DELETE FROM ads WHERE link = ? AND status = 'pending'", (ad_link,))
                message = "Объявление отклонено/удалено."

            if cursor.rowcount == 0:
                 # Важно: commit нужен, даже если ничего не изменилось, чтобы завершить транзакцию
                 conn.commit()
                 logger.warning(f"Действие модерации '{action}' над объявлением {ad_link} не удалось (не найдено или статус не 'pending'?).")
                 return jsonify({"error": "Объявление не найдено или не находится в статусе ожидания"}), 404 # 404 Not Found
            else:
                conn.commit()
                logger.info(f"Объявление {ad_link} было '{action}' модератором {moderator_id}.")
                 # TODO: Уведомить автора объявления (ad['user_id']) об изменении статуса?
                return jsonify({"message": message}), 200 # 200 OK

    except sqlite3.Error as e:
         logger.error(f"Ошибка БД при модерации объявления {ad_link}: {e}")
         return jsonify({"error": "Ошибка базы данных"}), 500
    except Exception as e:
         logger.error(f"Неожиданная ошибка при модерации объявления {ad_link}: {e}", exc_info=True)
         return jsonify({"error": "Внутренняя ошибка сервера"}), 500

@app.route('/api/pending_ads', methods=['GET'])
def get_pending_ads_api():
    """Возвращает список объявлений, ожидающих модерации."""
    admin_id = request.args.get('admin_id')
    # ---> !! ВАЖНО: Добавьте проверку аутентификации/авторизации !! <---
    if not admin_id or not is_user_admin(admin_id):
         logger.warning(f"Неавторизованная попытка просмотра ожидающих объявлений пользователем {admin_id}")
         return jsonify({"error": "Неавторизованный доступ"}), 403

    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ads WHERE source = 'User' AND status = 'pending' ORDER BY created_at ASC") # Сначала старые
            ads = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Администратор {admin_id} запросил {len(ads)} ожидающих объявлений.")
            return jsonify(ads)
    except sqlite3.Error as e:
         logger.error(f"Ошибка БД при получении ожидающих объявлений: {e}")
         return jsonify({"error": "Ошибка базы данных"}), 500
    except Exception as e:
         logger.error(f"Неожиданная ошибка при получении ожидающих объявлений: {e}", exc_info=True)
         return jsonify({"error": "Внутренняя ошибка сервера"}), 500

# --- Обслуживание статических файлов ---
@app.route(f'/{UPLOAD_FOLDER}/<path:filename>')
def serve_uploaded_file(filename):
    """Безопасно отдает загруженные файлы из папки UPLOAD_FOLDER."""
    logger.debug(f"Попытка отдать загруженный файл: {filename}")
    try:
        # send_from_directory обеспечивает защиту от выхода за пределы папки
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
    except FileNotFoundError:
        logger.warning(f"Запрошенный загруженный файл не найден: {filename}")
        abort(404) # Возвращаем 404 ошибку

# Обслуживание главного HTML файла Mini App
@app.route('/mini-app')
def mini_app_route():
    """Отдает главный index.html для Mini App."""
    logger.info("Запрос точки входа Mini App (/mini-app)")
    # Путь к папке сборки фронтенда (../client/build относительно этого скрипта)
    build_dir = app.static_folder # Используем настроенный static_folder
    if not build_dir or not os.path.exists(os.path.join(build_dir, 'index.html')):
        logger.error(f"Файл index.html для Mini App не найден в ожидаемой директории: {build_dir}")
        return "Ошибка: Фронтенд Mini App не найден.", 404
    return send_from_directory(build_dir, 'index.html')

# Маршрут-"ловушка" для поддержки клиентской маршрутизации (например, React Router)
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_react_app(path):
    """Обслуживает статические файлы сборки или index.html для клиентской маршрутизации."""
    build_dir = app.static_folder
    # Полный путь к запрашиваемому файлу
    static_file_path = os.path.join(build_dir, path)

    # Проверяем, существует ли по запрошенному пути статический файл (css, js, png и т.д.)
    if path != "" and os.path.exists(static_file_path) and os.path.isfile(static_file_path):
        logger.debug(f"Отдача статического файла: {path}")
        return send_from_directory(build_dir, path)
    else:
        # Если это не статический файл, отдаем главный index.html для обработки маршрута на клиенте
        logger.debug(f"Путь '{path}' не найден как статический файл, отдаем index.html для клиентской маршрутизации.")
        index_path = os.path.join(build_dir, 'index.html')
        if not os.path.exists(index_path):
             logger.error(f"Файл index.html для Mini App не найден в {build_dir}")
             return "Ошибка: Фронтенд Mini App не найден.", 404
        return send_from_directory(build_dir, 'index.html')


# --- Класс Telegram Бота ---
class ApartmentBot:
    def __init__(self):
        logger.info("Инициализация Telegram бота.")
        if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_FALLBACK_TOKEN":
             logger.error("Токен Telegram не настроен. Завершение настройки бота.")
             raise ValueError("Отсутствует токен Telegram")

        # Настройка параметров Application Builder (например, таймауты)
        app_builder = Application.builder().token(TELEGRAM_TOKEN)
        # app_builder.connect_timeout(10).read_timeout(20) # Пример установки таймаутов

        self.application = app_builder.build()
        self._setup_handlers() # Настройка обработчиков команд
        logger.info("Telegram бот успешно инициализирован.")

    def _setup_handlers(self):
        """Настраивает обработчики команд."""
        self.application.add_handler(CommandHandler("start", self.start))
        # Добавьте другие обработчики при необходимости
        logger.info("Обработчики команд настроены.")

    async def _setup_commands(self):
        """Устанавливает список команд бота в Telegram."""
        commands = [
            BotCommand("start", "Открыть поиск квартир 🏠"),
            # Добавьте другие команды, если реализуете их
        ]
        try:
            await self.application.bot.set_my_commands(commands)
            logger.info("Команды бота успешно установлены.")
        except Exception as e:
            # Ошибки установки команд не критичны для работы, но стоит залогировать
            logger.error(f"Не удалось установить команды бота: {e}", exc_info=True)

    async def start(self, update, context):
        """Обработчик команды /start."""
        user = update.effective_user
        user_id = user.id
        logger.info(f"Пользователь {user.full_name} ({user_id}) вызвал /start.")

        # Проверка наличия URL для Web App
        if not WEB_APP_URL or WEB_APP_URL == "https://YOUR_APP_DOMAIN/mini-app":
            logger.error("URL для Web App (WEB_APP_URL) не настроен. Невозможно создать кнопку.")
            try:
                await update.message.reply_text(
                    "Извините, приложение для поиска сейчас недоступно из-за ошибки конфигурации. 🛠️"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение об ошибке конфигурации пользователю {user_id}: {e}")
            return

        # Создание кнопки, открывающей Web App
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🚀 Открыть поиск квартир",
                web_app=WebAppInfo(url=WEB_APP_URL) # Используем WebAppInfo
            )]
        ])
        try:
            await update.message.reply_text(
                f"Привет, {user.first_name}! 👋\n\n"
                "Нажмите кнопку ниже, чтобы открыть удобный поиск квартир в аренду.",
                reply_markup=keyboard
            )
            logger.info(f"Отправлено приветственное сообщение с кнопкой WebApp пользователю {user_id}")
        except Exception as e:
            # Обработка возможных ошибок отправки (например, бот заблокирован пользователем)
            logger.error(f"Не удалось отправить сообщение /start пользователю {user_id}: {e}", exc_info=True)

    async def run(self):
        """Запускает polling бота."""
        logger.info("Запуск polling Telegram бота...")
        # Установка команд перед запуском polling
        await self._setup_commands()
        try:
            # Запуск polling (allowed_updates=None - принимать все обновления)
            # drop_pending_updates=True - игнорировать обновления, полученные пока бот был оффлайн
            await self.application.run_polling(allowed_updates=None, drop_pending_updates=True)
            logger.info("Polling бота остановлен штатно.")
        except Exception as e:
            logger.error(f"Polling бота завершился с ошибкой: {e}", exc_info=True)
            # Решить, нужно ли перевыбрасывать исключение для перезапуска менеджером процессов
            raise # Перевыбрасываем для индикации критической ошибки

# --- Запуск Веб-сервера ---
async def run_flask():
    """Запускает веб-сервер Flask с использованием Hypercorn."""
    # Получаем порт из переменной окружения или используем 5000 по умолчанию
    port = int(os.environ.get('PORT', 5000))
    config = Config()
    # Привязка к 0.0.0.0 делает сервер доступным извне (например, в Docker/Render)
    config.bind = [f"0.0.0.0:{port}"]
    # Включение debug режима Flask на основе переменной окружения
    config.debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    logger.info(f"Запуск Flask сервера с Hypercorn на порту {port}. Debug режим: {config.debug}")
    try:
        await hypercorn.asyncio.serve(app, config)
        logger.info("Flask сервер остановлен.")
    except Exception as e:
        logger.error(f"Flask сервер завершился с ошибкой: {e}", exc_info=True)
        raise # Перевыбрасываем для индикации критической ошибки

# --- Основное выполнение ---
async def main():
    """Главная асинхронная функция запуска приложения."""
    logger.info("Запуск приложения...")

    # Убедимся, что папка для загрузок существует
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
        logger.info(f"Создана папка для загрузок: {UPLOAD_FOLDER}")

    # Инициализация и запуск планировщика для периодических задач
    try:
         # Установка часового пояса (важно для корректной работы интервалов)
         minsk_tz = pytz.timezone("Europe/Minsk")
         scheduler = AsyncIOScheduler(timezone=minsk_tz)
         # Запуск задачи с интервалом. `replace_existing=True` перезаписывает задачу с тем же ID.
         # `misfire_grace_time` - допустимое время опоздания запуска задачи.
         scheduler.add_job(
             fetch_and_store_ads,
             'interval',
             minutes=PARSE_INTERVAL,
             id='fetch_ads_job',
             replace_existing=True,
             misfire_grace_time=600 # 10 минут
         )
         # Можно добавить немедленный запуск при старте (опционально, т.к. интервал сработает)
         # scheduler.add_job(fetch_and_store_ads, 'date', run_date=datetime.now(minsk_tz))
         scheduler.start()
         logger.info(f"Планировщик запущен. Интервал парсинга: {PARSE_INTERVAL} минут.")
    except Exception as e:
         logger.error(f"Не удалось запустить планировщик: {e}", exc_info=True)
         return # Завершаем работу, если планировщик не стартовал

    # Инициализация Бота
    try:
        bot = ApartmentBot()
    except ValueError as e: # Ловим ошибку отсутствия токена
         logger.error(f"Не удалось инициализировать бота: {e}. Завершение работы.")
         if scheduler.running: scheduler.shutdown()
         return # Останавливаемся, если бот не инициализирован
    except Exception as e:
        logger.error(f"Неожиданная ошибка при инициализации бота: {e}", exc_info=True)
        if scheduler.running: scheduler.shutdown()
        return

    # Запуск Flask и Бота конкурентно
    logger.info("Запуск Flask сервера и Telegram бота...")
    flask_task = asyncio.create_task(run_flask(), name="FlaskTask")
    bot_task = asyncio.create_task(bot.run(), name="BotTask")

    # Ожидание завершения одной из задач (или обеих)
    done, pending = await asyncio.wait(
        [flask_task, bot_task],
        return_when=asyncio.FIRST_COMPLETED, # Ждем, пока не завершится первая задача
    )

    # Обработка завершенных задач (проверка на ошибки)
    for task in done:
        task_name = task.get_name()
        try:
            result = await task # Получаем результат или исключение
            logger.info(f"Задача {task_name} завершилась штатно с результатом: {result}")
        except Exception as e:
            logger.error(f"Задача {task_name} завершилась с ошибкой: {e}", exc_info=True)

    # Отмена ожидающих задач
    logger.info("Отмена ожидающих задач...")
    for task in pending:
        task_name = task.get_name()
        logger.info(f"Отменяем задачу {task_name}...")
        task.cancel()
        try:
            await task # Даем возможность задаче обработать отмену
        except asyncio.CancelledError:
            logger.info(f"Задача {task_name} успешно отменена.")
        except Exception as e:
             # Логируем ошибки, возникшие во время отмены
             logger.error(f"Ошибка во время отмены задачи {task_name}: {e}", exc_info=True)

    # Корректное завершение работы планировщика
    if scheduler.running:
         logger.info("Остановка планировщика...")
         scheduler.shutdown()
         logger.info("Планировщик остановлен.")

    logger.info("Приложение завершило работу.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Приложение остановлено пользователем (KeyboardInterrupt).")
    except Exception as e:
         # Логирование критических ошибок, не пойманных в main()
         logger.critical(f"Критическая ошибка в главном потоке выполнения: {e}", exc_info=True)