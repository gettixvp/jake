const express = require('express');
const router = express.Router();
const jwt = require('jsonwebtoken');
const User = require('../models/User');
const authMiddleware = require('../middleware/authMiddleware');

router.post('/login', authMiddleware, async (req, res) => {
    try {
        const { id, username } = req.user;
        let user = await User.findOne({ telegramId: id });

        if (!user) {
            user = new User({
                telegramId: id,
                username: username || `Игрок_${id}`,
                balance: 5000
            });
            await user.save();
        }

        const token = jwt.sign({ userId: user._id }, process.env.JWT_SECRET || 'your_jwt_secret', { expiresIn: '1h' });
        res.json({ token, user: { id: user.telegramId, username: user.username, balance: user.balance } });
    } catch (error) {
        res.status(500).json({ error: 'Server error' });
    }
});

module.exports = router;