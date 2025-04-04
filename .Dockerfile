# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install system dependencies, including those required by Google Chrome
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    curl \
    libglib2.0-0 \
    libnss3 \
    libgconf-2-4 \
    libfontconfig1 \
    libxrender1 \
    libxtst6 \
    libxi6 \
    libxss1 \
    fonts-liberation \
    libappindicator3-1 \
    xdg-utils \
    libasound2 \
    libvulkan1 \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome
RUN wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && dpkg -i /tmp/google-chrome.deb \
    && rm /tmp/google-chrome.deb

# Install Chromedriver (specific version for Chrome 135.0.7049.52)
RUN wget -q -O /tmp/chromedriver.zip "https://chromedriver.storage.googleapis.com/135.0.7049.52/chromedriver_linux64.zip" \
    && unzip /tmp/chromedriver.zip -d /usr/bin/ \
    && chmod +x /usr/bin/chromedriver \
    && rm /tmp/chromedriver.zip

# Copy project files
COPY requirements.txt .
COPY src/ .
COPY mini_app.html .
COPY favicon.ico .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 10000

# Command to run the application
CMD ["python", "app.py"]
