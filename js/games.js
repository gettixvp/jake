// Модуль для игры в слоты
const Slots = {
    symbols: ['🍒', '🍋', '🍊', '🍇', '🔔', '⭐', '7️⃣', '💰'],
    reels: [],
    isSpinning: false,
    currentBet: 0,
    
    init() {
        this.createReels();
        this.setupEventListeners();
    },
    
    createReels() {
        const slotsArea = document.getElementById('slotsArea');
        slotsArea.innerHTML = '';
        
        // Создаем 3 барабана
        for (let i = 0; i < 3; i++) {
            const reel = document.createElement('div');
            reel.className = 'slots-reel';
            reel.dataset.reelIndex = i;
            
            // Создаем символы в каждом барабане (3 видимых + 2 скрытых для анимации)
            for (let j = 0; j < 5; j++) {
                const symbol = document.createElement('div');
                symbol.className = 'slots-symbol';
                symbol.textContent = this.symbols[Math.floor(Math.random() * this.symbols.length)];
                reel.appendChild(symbol);
            }
            
            slotsArea.appendChild(reel);
            this.reels.push(reel);
        }
    },
    
    setupEventListeners() {
        document.getElementById('playBtn').addEventListener('click', () => {
            if (!this.isSpinning) {
                App.showModal('bet');
            }
        });
    },
    
    start(bet) {
        this.currentBet = bet;
        this.isSpinning = true;
        document.getElementById('playBtn').disabled = true;
        
        // Запускаем вибрацию
        if (navigator.vibrate) {
            navigator.vibrate([100, 50, 100]);
        }
        
        // Запускаем анимацию каждого барабана
        this.reels.forEach((reel, index) => {
            this.spinReel(reel, index);
        });
        
        // Проигрываем звук вращения (если есть)
        const spinSound = document.getElementById('spinSound');
        if (spinSound) {
            spinSound.currentTime = 0;
            spinSound.play();
        }
    },
    
    spinReel(reel, index) {
        const symbols = reel.querySelectorAll('.slots-symbol');
        const spinDuration = 2000 + index * 500; // Каждый следующий барабан останавливается позже
        
        // Анимация вращения
        let startTime = null;
        const spin = (timestamp) => {
            if (!startTime) startTime = timestamp;
            const progress = timestamp - startTime;
            
            // Сдвигаем символы вниз
            symbols.forEach(symbol => {
                const yPos = (progress / 50) % 100;
                symbol.style.transform = `translateY(-${yPos}%)`;
            });
            
            if (progress < spinDuration) {
                requestAnimationFrame(spin);
            } else {
                this.stopReel(reel, index);
            }
        };
        
        requestAnimationFrame(spin);
    },
    
    stopReel(reel, index) {
        const symbols = Array.from(reel.querySelectorAll('.slots-symbol'));
        
        // Генерируем случайный символ для остановки
        const randomSymbol = this.symbols[Math.floor(Math.random() * this.symbols.length)];
        
        // Устанавливаем центральный символ
        symbols[2].textContent = randomSymbol;
        symbols[1].textContent = this.symbols[(this.symbols.indexOf(randomSymbol) - 1 >= 0 ? 
            this.symbols.indexOf(randomSymbol) - 1 : this.symbols.length - 1];
        symbols[3].textContent = this.symbols[(this.symbols.indexOf(randomSymbol) + 1) % this.symbols.length];
        
        // Сбрасываем позиции
        symbols.forEach((symbol, i) => {
            symbol.style.transform = `translateY(${(i - 2) * 100 - 50}%)`;
        });
        
        // Проверяем, все ли барабаны остановились
        if (index === this.reels.length - 1) {
            setTimeout(() => {
                this.checkResult();
            }, 500);
        }
    },
    
    checkResult() {
        const results = [];
        this.reels.forEach(reel => {
            const symbols = reel.querySelectorAll('.slots-symbol');
            results.push(symbols[2].textContent);
        });
        
        // Простая логика выигрыша: 3 одинаковых символа
        if (results[0] === results[1] && results[1] === results[2]) {
            const winMultiplier = this.getWinMultiplier(results[0]);
            const winAmount = this.currentBet * winMultiplier;
            
            App.userBalance += winAmount;
            App.updateBalance();
            App.addToHistory('slots', winAmount, true);
            
            // Вибрация выигрыша
            if (navigator.vibrate) {
                navigator.vibrate([200, 100, 200, 100, 200]);
            }
            
            // Анимация выигрыша
            this.animateWin();
            
            // Проигрываем звук выигрыша
            const winSound = document.getElementById('winSound');
            if (winSound) {
                winSound.currentTime = 0;
                winSound.play();
            }
            
            App.showNotification(`🎉 Вы выиграли ${winAmount}₽!`);
        } else {
            App.addToHistory('slots', this.currentBet, false);
            
            // Вибрация проигрыша
            if (navigator.vibrate) {
                navigator.vibrate([300]);
            }
            
            // Проигрываем звук проигрыша
            const loseSound = document.getElementById('loseSound');
            if (loseSound) {
                loseSound.currentTime = 0;
                loseSound.play();
            }
            
            App.showNotification("Повезёт в следующий раз!", 2000);
        }
        
        this.isSpinning = false;
        document.getElementById('playBtn').disabled = false;
    },
    
    getWinMultiplier(symbol) {
        const multipliers = {
            '🍒': 2,
            '🍋': 3,
            '🍊': 4,
            '🍇': 5,
            '🔔': 10,
            '⭐': 15,
            '7️⃣': 20,
            '💰': 50
        };
        return multipliers[symbol] || 1;
    },
    
    animateWin() {
        const reels = document.querySelectorAll('.slots-reel');
        reels.forEach(reel => {
            reel.classList.add('win-animation');
            setTimeout(() => {
                reel.classList.remove('win-animation');
            }, 1000);
        });
    }
};

// Модуль для колеса фортуны (заглушка)
const Wheel = {
    init() {
        console.log("Wheel initialized");
    },
    
    start(bet) {
        console.log(`Starting wheel with bet: ${bet}`);
    }
};
