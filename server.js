const express = require('express');
const mongoose = require('mongoose');
const dotenv = require('dotenv');
const cors = require('cors');
const path = require('path');
const jwt = require('jsonwebtoken');
const User = require('./models/User');
const Winner = require('./models/Winner');
const Transaction = require('./models/Transaction');

dotenv.config();

const app = express();

// Middleware
app.use(cors());
app.use(express.json());

// Настройка EJS как шаблонизатора
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, 'views'));

// Middleware для проверки авторизации
const authenticateToken = async (req, res, next) => {
    const token = req.headers['authorization']?.split(' ')[1];
    if (!token) {
        req.user = null;
        return next();
    }

    try {
        const decoded = jwt.verify(token, process.env.JWT_SECRET || 'your_jwt_secret');
        const user = await User.findById(decoded.userId).populate('achievements');
        req.user = user ? {
            id: user.telegramId,
            username: user.username,
            balance: user.balance,
            achievements: user.achievements
        } : null;
        next();
    } catch (error) {
        req.user = null;
        next();
    }
};

// Routes
app.use('/api/auth', require('./routes/auth'));
app.use('/api/user', require('./routes/user'));
app.use('/api/game', require('./routes/game'));
app.use('/api/transactions', require('./routes/transaction'));

// Главный маршрут для рендеринга страницы
app.get('/', authenticateToken, async (req, res) => {
    try {
        const recentWinners = await Winner.find().sort({ date: -1 }).limit(3);
        let achievements = [];
        let transactions = [];
        if (req.user) {
            achievements = req.user.achievements;
            transactions = await Transaction.find({ userId: req.user._id }).sort({ date: -1 });
        }

        res.render('index', {
            user: req.user,
            recentWinners,
            achievements,
            transactions
        });
    } catch (error) {
        console.error('Error rendering page:', error);
        res.render('index', {
            user: null,
            recentWinners: [],
            achievements: [],
            transactions: []
        });
    }
});

// Connect to MongoDB
mongoose.connect(process.env.MONGO_URI, {
    useNewUrlParser: true,
    useUnifiedTopology: true
}).then(() => {
    console.log('Connected to MongoDB');
}).catch(err => {
    console.error('MongoDB connection error:', err);
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`Server running on port ${PORT}`);
});