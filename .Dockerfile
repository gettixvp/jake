# Используем официальный образ Python
FROM python:3.9-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем requirements.txt и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY src/ .

# Указываем порт
EXPOSE 5000

# Команда для запуска приложения
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
