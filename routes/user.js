const express = require('express');
const router = express.Router();
const User = require('../models/User');
const Transaction = require('../models/Transaction');
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

router.get('/profile', authenticateToken, async (req, res) => {
    try {
        const user = await User.findById(req.userId).populate('achievements');
        if (!user) return res.status(404).json({ error: 'User not found' });
        res.json({
            id: user.telegramId,
            username: user.username,
            balance: user.balance,
            achievements: user.achievements
        });
    } catch (error) {
        res.status(500).json({ error: 'Server error' });
    }
});

router.post('/deposit', authenticateToken, async (req, res) => {
    const { amount } = req.body;
    if (!amount || amount < 50) {
        return res.status(400).json({ error: 'Minimum deposit is 50' });
    }

    try {
        const user = await User.findById(req.userId);
        if (!user) return res.status(404).json({ error: 'User not found' });

        user.balance += amount;
        user.depositHistory.push({ amount });
        user.transactions.push({ type: 'deposit', amount });

        const transaction = new Transaction({
            userId: user._id,
            type: 'deposit',
            amount
        });
        await transaction.save();
        await user.save();

        res.json({ balance: user.balance });
    } catch (error) {
        res.status(500).json({ error: 'Server error' });
    }
});

router.post('/withdraw', authenticateToken, async (req, res) => {
    const { amount } = req.body;
    if (!amount || amount < 50) {
        return res.status(400).json({ error: 'Minimum withdrawal is 50' });
    }

    try {
        const user = await User.findById(req.userId);
        if (!user) return res.status(404).json({ error: 'User not found' });
        if (user.balance < amount) {
            return res.status(400).json({ error: 'Insufficient balance' });
        }

        user.balance -= amount;
        user.transactions.push({ type: 'withdraw', amount });

        const transaction = new Transaction({
            userId: user._id,
            type: 'withdraw',
            amount
        });
        await transaction.save();
        await user.save();

        res.json({ balance: user.balance });
    } catch (error) {
        res.status(500).json({ error: 'Server error' });
    }
});

router.get('/transactions', authenticateToken, async (req, res) => {
    try {
        const transactions = await Transaction.find({ userId: req.userId }).sort({ date: -1 });
        res.json(transactions);
    } catch (error) {
        res.status(500).json({ error: 'Server error' });
    }
});

module.exports = router;