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
        this.reels = [];
        const reelsContainer = document.getElementById('slotsReels');
        reelsContainer.innerHTML = '';
        
        for (let i = 0; i < 3; i++) {
            const reel = document.createElement('div');
            reel.className = 'slots-reel';
            reel.id = `reel${i+1}`;
            
            // Создаем 5 символов в каждом барабане (3 видимых + 2 для анимации)
            for (let j = 0; j < 5; j++) {
                const symbol = document.createElement('div');
                symbol.className = 'slots-symbol';
                symbol.textContent = this.getRandomSymbol();
                symbol.style.transform = `translateY(${(j - 2) * 100}%)`;
                reel.appendChild(symbol);
            }
            
            reelsContainer.appendChild(reel);
            this.reels.push(reel);
        }
    },
    
    getRandomSymbol() {
        return this.symbols[Math.floor(Math.random() * this.symbols.length)];
    },
    
    setupEventListeners() {
        document.getElementById('spinBtn').addEventListener('click', () => {
            if (!this.isSpinning) {
                App.showModal('bet');
            }
        });
    },
    
    start(bet) {
        this.currentBet = bet;
        this.isSpinning = true;
        document.getElementById('spinBtn').disabled = true;
        
        // Вибрация
        if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
        
        // Звук вращения
        App.playSound('spin');
        
        // Анимация барабанов
        this.reels.forEach((reel, index) => {
            this.spinReel(reel, index);
        });
    },
    
    spinReel(reel, index) {
        const symbols = reel.querySelectorAll('.slots-symbol');
        const spinDuration = 2000 + index * 500;
        const startTime = performance.now();
        
        const animate = (currentTime) => {
            const elapsed = currentTime - startTime;
            const progress = Math.min(elapsed / spinDuration, 1);
            
            // Плавное замедление
            const easeOut = 1 - Math.pow(1 - progress, 3);
            
            symbols.forEach((symbol, i) => {
                const yPos = (i - 2 + easeOut * 10) * 100;
                symbol.style.transform = `translateY(${yPos}%)`;
                
                // Обновляем символы при прокрутке
                if (yPos > (i + 1) * 100) {
                    symbol.textContent = this.getRandomSymbol();
                }
            });
            
            if (progress < 1) {
                requestAnimationFrame(animate);
            } else {
                this.stopReel(reel, index);
            }
        };
        
        requestAnimationFrame(animate);
    },
    
    stopReel(reel, index) {
        const symbols = reel.querySelectorAll('.slots-symbol');
        const resultSymbol = this.getRandomSymbol();
        
        // Устанавливаем центральные символы
        symbols[2].textContent = resultSymbol;
        symbols[1].textContent = this.getPrevSymbol(resultSymbol);
        symbols[3].textContent = this.getNextSymbol(resultSymbol);
        
        // Сбрасываем позиции
        symbols.forEach((symbol, i) => {
            symbol.style.transform = `translateY(${(i - 2) * 100}%)`;
        });
        
        // Проверяем результаты после остановки последнего барабана
        if (index === this.reels.length - 1) {
            setTimeout(() => this.checkResult(), 500);
        }
    },
    
    getPrevSymbol(symbol) {
        const index = this.symbols.indexOf(symbol);
        return this.symbols[index > 0 ? index - 1 : this.symbols.length - 1];
    },
    
    getNextSymbol(symbol) {
        const index = this.symbols.indexOf(symbol);
        return this.symbols[(index + 1) % this.symbols.length];
    },
    
    checkResult() {
        const results = [];
        this.reels.forEach(reel => {
            const symbols = reel.querySelectorAll('.slots-symbol');
            results.push(symbols[2].textContent);
        });
        
        // Проверка выигрышной комбинации
        if (results[0] === results[1] && results[1] === results[2]) {
            const winMultiplier = this.getWinMultiplier(results[0]);
            const winAmount = this.currentBet * winMultiplier;
            
            App.userBalance += winAmount;
            App.updateBalance();
            App.addToHistory('slots', winAmount, true);
            
            // Анимация выигрыша
            this.animateWin();
            
            // Вибрация и звук
            if (navigator.vibrate) navigator.vibrate([200, 100, 200, 100, 200]);
            App.playSound('win');
            
            App.showNotification(`🎉 Вы выиграли ${winAmount.toLocaleString()}₽!`);
        } else {
            App.addToHistory('slots', this.currentBet, false);
            
            // Вибрация и звук проигрыша
            if (navigator.vibrate) navigator.vibrate(300);
            App.playSound('lose');
            
            App.showNotification("Повезёт в следующий раз!", 2000);
        }
        
        this.isSpinning = false;
        document.getElementById('spinBtn').disabled = false;
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
            }, 1500);
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
