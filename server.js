// server.js
require('dotenv').config();
const express = require('express');
const { Telegraf } = require('telegraf');
const { validate } = require('@telegram-apps/init-data-node');
const jwt = require('jsonwebtoken');
const path = require('path');

const app = express();
const bot = new Telegraf(process.env.TELEGRAM_TOKEN);

// Middleware для обработки JSON и статических файлов
app.use(express.json());
app.use(express.static(path.join(__dirname, '.')));

// Настройка webhook
const webhookPath = `/bot${process.env.TELEGRAM_TOKEN}`;
bot.telegram.setWebhook(`${process.env.WEBAPP_URL}${webhookPath}`);

// Обработка входящих сообщений
bot.start((ctx) => {
    ctx.reply('Добро пожаловать в Star Casino! 🎰', {
        reply_markup: {
            inline_keyboard: [
                [{ text: 'Открыть казино', web_app: { url: process.env.WEBAPP_URL } }]
            ]
        }
    });
});

// Валидация initData и генерация JWT
app.post('/auth', async (req, res) => {
    const initData = req.body.initData;
    try {
        validate(initData, process.env.TELEGRAM_TOKEN, { expiresIn: 3600 });
        const userData = new URLSearchParams(initData).get('user');
        const user = JSON.parse(userData);
        const token = jwt.sign({ id: user.id, username: user.username }, process.env.TELEGRAM_TOKEN, { expiresIn: '1h' });
        res.json({ token });
    } catch (error) {
        res.status(401).json({ error: 'Invalid initData' });
    }
});

// Webhook для Telegram
app.use(bot.webhookCallback(webhookPath));

// Запуск сервера
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`Server running on port ${PORT}`);
});