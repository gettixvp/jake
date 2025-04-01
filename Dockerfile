# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем системные зависимости, включая Chromium
RUN apt-get update && apt-get install -y \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем requirements.txt и устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Указываем путь к Chromium для pyppeteer
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Указываем команду запуска
CMD ["hypercorn", "app:app", "-b", "0.0.0.0:$PORT"]