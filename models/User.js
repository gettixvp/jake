const mongoose = require('mongoose');

const userSchema = new mongoose.Schema({
    telegramId: { type: String, required: true, unique: true },
    username: { type: String, required: true },
    balance: { type: Number, default: 5000 },
    depositHistory: [{ amount: Number, date: { type: Date, default: Date.now } }],
    transactions: [{ type: String, amount: Number, date: { type: Date, default: Date.now } }],
    achievements: [{ type: mongoose.Schema.Types.ObjectId, ref: 'Achievement' }],
    createdAt: { type: Date, default: Date.now }
});

module.exports = mongoose.model('User', userSchema);