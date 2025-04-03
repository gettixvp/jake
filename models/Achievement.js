const mongoose = require('mongoose');

const achievementSchema = new mongoose.Schema({
    name: { type: String, required: true },
    description: { type: String, required: true },
    icon: { type: String },
    unlocked: { type: Boolean, default: false }
});

module.exports = mongoose.model('Achievement', achievementSchema);