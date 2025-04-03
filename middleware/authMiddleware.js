const crypto = require('crypto');
const jwt = require('jsonwebtoken');

const validateTelegramData = (initData, botToken) => {
    const params = new URLSearchParams(initData);
    const hash = params.get('hash');
    params.delete('hash');

    const sortedParams = Array.from(params.entries())
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([key, value]) => `${key}=${value}`)
        .join('\n');

    const secretKey = crypto.createHmac('sha256', 'WebAppData')
        .update(botToken)
        .digest();
    const computedHash = crypto.createHmac('sha256', secretKey)
        .update(sortedParams)
        .digest('hex');

    return computedHash === hash;
};

const authMiddleware = (req, res, next) => {
    const { initData } = req.body;
    if (!initData) {
        return res.status(401).json({ error: 'No initData provided' });
    }

    const botToken = process.env.TELEGRAM_BOT_TOKEN;
    if (!validateTelegramData(initData, botToken)) {
        return res.status(401).json({ error: 'Invalid Telegram initData' });
    }

    const params = new URLSearchParams(initData);
    const user = JSON.parse(params.get('user'));
    req.user = user;
    next();
};

module.exports = authMiddleware;