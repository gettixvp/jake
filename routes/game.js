const express = require('express');
const router = express.Router();
const User = require('../models/User');
const Transaction = require('../models/Transaction');
const Winner = require('../models/Winner');
const Achievement = require('../models/Achievement');
const jwt = require('jsonwebtoken');

const authenticateToken = (req, res, next) => {
    const token = req.headers['authorization']?.split(' ')[1];
    if (!token) return res.status(401).json({ error: 'No token provided' });

    jwt.verify(token, process.env.JWT_SECRET || 'your_jwt_secret', (err, decoded) => {
        if (err) return res.status(403).json({ error: 'Invalid token' });
        req.userId = decoded.userId;
        next();
    });
};

const symbols = ['7', '🍒', '🍋', '🍊', '🍉', '💰'];

router.post('/spin', authenticateToken, async (req, res) => {
    const { bet } = req.body;
    if (!bet || bet < 10) {
        return res.status(400).json({ error: 'Minimum bet is 10' });
    }

    try {
        const user = await User.findById(req.userId);
        if (!user) return res.status(404).json({ error: 'User not found' });
        if (user.balance < bet) {
            return res.status(400).json({ error: 'Insufficient balance' });
        }

        user.balance -= bet;
        const transaction = new Transaction({
            userId: user._id,
            type: 'loss',
            amount: bet
        });
        await transaction.save();

        const reels = [
            symbols[Math.floor(Math.random() * symbols.length)],
            symbols[Math.floor(Math.random() * symbols.length)],
            symbols[Math.floor(Math.random() * symbols.length)]
        ];

        let winAmount = 0;
        if (reels[0] === reels[1] && reels[1] === reels[2]) {
            let multiplier = 0;
            if (reels[0] === '7') multiplier = 10;
            else if (reels[0] === '🍒') multiplier = 5;
            else if (reels[0] === '🍋') multiplier = 3;
            else if (reels[0] === '🍊') multiplier = 2;
            else multiplier = 1;

            winAmount = bet * multiplier;
            user.balance += winAmount;

            const winTransaction = new Transaction({
                userId: user._id,
                type: 'win',
                amount: winAmount
            });
            await winTransaction.save();

            const winner = new Winner({
                userId: user._id,
                username: user.username,
                amount: winAmount
            });
            await winner.save();

            // Проверка на достижения
            const transactions = await Transaction.find({ userId: user._id, type: 'win' });
            if (transactions.length === 1) {
                const achievement = new Achievement({
                    name: 'Новичок',
                    description: 'Выиграй свою первую игру',
                    icon: 'star'
                });
                await achievement.save();
                user.achievements.push(achievement._id);
            }
        }

        await user.save();
        res.json({ reels, winAmount, balance: user.balance });
    } catch (error) {
        res.status(500).json({ error: 'Server error' });
    }
});

router.get('/recent-winners', async (req, res) => {
    try {
        const winners = await Winner.find().sort({ date: -1 }).limit(3);
        res.json(winners);
    } catch (error) {
        res.status(500).json({ error: 'Server error' });
    }
});

module.exports = router;