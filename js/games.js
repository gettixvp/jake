// js/games.js
class GameManager {
    static playSound(soundFile) {
        if (settings.soundEnabled) {
            const audio = new Audio(`assets/sounds/${soundFile}`);
            audio.play().catch(() => console.error('Error playing sound:', soundFile));
        }
    }

    static async notifyWin(userId, message) {
        try {
            await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    chat_id: userId,
                    text: message
                })
            });
        } catch (error) {
            console.error('Ошибка отправки уведомления:', error);
        }
    }

    static startGame(gameId) {
        const gameTiles = document.getElementById('game-tiles');
        if (!gameTiles) {
            console.error('Game tiles container not found!');
            return;
        }
        if (gameId === 'slots') {
            gameTiles.innerHTML = `
                <div class="game-screen">
                    <h2>Слоты</h2>
                    <div class="game-content">
                        <p id="slot-result">Нажми "Спин"!</p>
                        <button onclick="GameManager.spinSlots()">Спин (⭐ 10)</button>
                        <button class="back-btn" onclick="renderMainScreen()">Назад</button>
                    </div>
                </div>
            `;
        } else if (gameId === 'roulette') {
            gameTiles.innerHTML = `
                <div class="game-screen">
                    <h2>Рулетка</h2>
                    <div class="game-content">
                        <p id="roulette-result">Сделай ставку!</p>
                        <input type="number" id="roulette-bet" placeholder="Ставка (мин. 10)" min="10">
                        <button onclick="GameManager.spinRoulette()">Крутить</button>
                        <button class="back-btn" onclick="renderMainScreen()">Назад</button>
                    </div>
                </div>
            `;
        } else if (gameId === 'blackjack') {
            gameTiles.innerHTML = `
                <div class="game-screen">
                    <h2>Блэкджек</h2>
                    <div class="game-content">
                        <p id="blackjack-result">Нажми "Играть"!</p>
                        <button onclick="GameManager.playBlackjack()">Играть (⭐ 20)</button>
                        <button class="back-btn" onclick="renderMainScreen()">Назад</button>
                    </div>
                </div>
            `;
        } else if (gameId === 'wheel') {
            gameTiles.innerHTML = `
                <div class="game-screen">
                    <h2>Колесо Фортуны</h2>
                    <div class="game-content">
                        <p id="wheel-result">Крути колесо!</p>
                        <button onclick="GameManager.spinWheel()">Крутить (⭐ 5)</button>
                        <button class="back-btn" onclick="renderMainScreen()">Назад</button>
                    </div>
                </div>
            `;
        }
    }

    static spinSlots() {
        if (user.balance < 10) {
            showModal('Недостаточно Stars!');
            return;
        }
        user.balance -= 10;
        updateBalance();
        this.playSound('spin.mp3');
        const result = Math.random() > 0.5 ? 'Вы выиграли ⭐ 20!' : 'Попробуй еще!';
        if (result.includes('выиграли')) {
            user.balance += 20;
            user.winHistory.push({ date: new Date().toLocaleString(), amount: 20, game: 'Слоты' });
            this.playSound('win.mp3');
            this.notifyWin(user.id, '🎉 Поздравляем! Вы выиграли ⭐ 20 в Слотах!');
        } else {
            this.playSound('lose.mp3');
        }
        const slotResult = document.getElementById('slot-result');
        if (slotResult) {
            slotResult.textContent = result;
        }
        updateBalance();
        Storage.save('user', user);
    }

    static spinRoulette() {
        const betInput = document.getElementById('roulette-bet');
        if (!betInput) {
            console.error('Roulette bet input not found!');
            return;
        }
        const bet = parseInt(betInput.value);
        if (!bet || bet < 10 || bet > user.balance) {
            showModal('Некорректная ставка! Минимум 10 Stars, не больше баланса.');
            return;
        }
        user.balance -= bet;
        updateBalance();
        this.playSound('spin.mp3');
        const result = Math.random() > 0.7 ? `Вы выиграли ⭐ ${bet * 2}!` : 'Удача не на вашей стороне!';
        if (result.includes('выиграли')) {
            const winAmount = bet * 2;
            user.balance += winAmount;
            user.winHistory.push({ date: new Date().toLocaleString(), amount: winAmount, game: 'Рулетка' });
            this.playSound('win.mp3');
            this.notifyWin(user.id, `🎉 Поздравляем! Вы выиграли ⭐ ${winAmount} в Рулетке!`);
        } else {
            this.playSound('lose.mp3');
        }
        const rouletteResult = document.getElementById('roulette-result');
        if (rouletteResult) {
            rouletteResult.textContent = result;
        }
        updateBalance();
        Storage.save('user', user);
    }

    static playBlackjack() {
        if (user.balance < 20) {
            showModal('Недостаточно Stars!');
            return;
        }
        user.balance -= 20;
        updateBalance();
        this.playSound('spin.mp3');
        const playerScore = Math.floor(Math.random() * 11) + 10;
        const dealerScore = Math.floor(Math.random() * 11) + 10;
        let result;
        if (playerScore > 21) {
            result = 'Перебор! Вы проиграли.';
            this.playSound('lose.mp3');
        } else if (dealerScore > 21 || playerScore > dealerScore) {
            result = `Вы выиграли ⭐ 40! (Ваш счет: ${playerScore}, Дилер: ${dealerScore})`;
            user.balance += 40;
            user.winHistory.push({ date: new Date().toLocaleString(), amount: 40, game: 'Блэкджек' });
            this.playSound('win.mp3');
            this.notifyWin(user.id, `🎉 Поздравляем! Вы выиграли ⭐ 40 в Блэкджеке!`);
        } else {
            result = `Проигрыш! (Ваш счет: ${playerScore}, Дилер: ${dealerScore})`;
            this.playSound('lose.mp3');
        }
        const blackjackResult = document.getElementById('blackjack-result');
        if (blackjackResult) {
            blackjackResult.textContent = result;
        }
        updateBalance();
        Storage.save('user', user);
    }

    static spinWheel() {
        if (user.balance < 5) {
            showModal('Недостаточно Stars!');
            return;
        }
        user.balance -= 5;
        updateBalance();
        this.playSound('spin.mp3');
        const prizes = [0, 10, 20, 50, 100];
        const prize = prizes[Math.floor(Math.random() * prizes.length)];
        const result = prize > 0 ? `Вы выиграли ⭐ ${prize}!` : 'Без приза, попробуй еще!';
        if (prize > 0) {
            user.balance += prize;
            user.winHistory.push({ date: new Date().toLocaleString(), amount: prize, game: 'Колесо Фортуны' });
            this.playSound('win.mp3');
            this.notifyWin(user.id, `🎉 Поздравляем! Вы выиграли ⭐ ${prize} в Колесе Фортуны!`);
        } else {
            this.playSound('lose.mp3');
        }
        const wheelResult = document.getElementById('wheel-result');
        if (wheelResult) {
            wheelResult.textContent = result;
        }
        updateBalance();
        Storage.save('user', user);
    }
}
