// Инициализация Telegram WebApp
const tgApp = Telegram.WebApp;

// Основной объект приложения
const App = {
    currentGame: null,
    userBalance: 1000,
    
    init() {
        tgApp.expand(); // Разворачиваем на весь экран
        tgApp.enableClosingConfirmation(); // Включаем подтверждение закрытия
        
        this.setupEventListeners();
        this.updateUserData();
        this.showNotification("Добро пожаловать в Star Casino!");
        Storage.renderHistory();
    },
    
    setupEventListeners() {
        // Навигация
        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const section = btn.getAttribute('data-section');
                this.navigateTo(section);
            });
        });
        
        // Игры на главной
        document.querySelectorAll('.game-card').forEach(card => {
            card.addEventListener('click', () => {
                const game = card.getAttribute('data-game');
                this.startGame(game);
            });
        });
        
        // Кнопки профиля
        document.getElementById('depositBtn').addEventListener('click', () => {
            this.showModal('deposit');
        });
        
        document.getElementById('withdrawBtn').addEventListener('click', () => {
            this.showModal('withdraw');
        });
        
        // Закрытие модальных окон
        document.querySelector('.close-modal').addEventListener('click', () => {
            this.hideModal();
        });
        
        // Ставки в модальном окне
        document.querySelectorAll('.chip').forEach(chip => {
            chip.addEventListener('click', () => {
                const bet = parseInt(chip.getAttribute('data-bet'));
                this.placeBet(bet);
            });
        });
    },
    
    navigateTo(section) {
        // Скрываем все секции
        document.querySelectorAll('.page-section').forEach(sec => {
            sec.classList.remove('active');
        });
        
        // Показываем нужную секцию
        document.getElementById(`${section}Section`).classList.add('active');
        
        // Обновляем активную кнопку навигации
        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.classList.remove('active');
            if (btn.getAttribute('data-section') === section) {
                btn.classList.add('active');
            }
        });
        
        // Если это страница игры, инициализируем игру
        if (section === 'games' && this.currentGame) {
            this.initGameUI(this.currentGame);
        }
    },
    
    startGame(game) {
        this.currentGame = game;
        this.navigateTo('games');
    },
    
    showNotification(text, duration = 3000) {
        const notification = document.getElementById('notification');
        notification.textContent = text;
        notification.style.display = 'block';
        
        setTimeout(() => {
            notification.style.display = 'none';
        }, duration);
    },
    
    showModal(type) {
        const modal = document.getElementById('betModal');
        modal.style.display = 'flex';
    },
    
    hideModal() {
        document.querySelectorAll('.modal').forEach(modal => {
            modal.style.display = 'none';
        });
    },
    
    placeBet(bet) {
        if (this.userBalance >= bet) {
            this.userBalance -= bet;
            this.updateBalance();
            this.hideModal();
            this.showNotification(`Ставка ${bet}₽ принята!`);
            
            // Здесь будет запуск игры с выбранной ставкой
            if (this.currentGame === 'slots') {
                Slots.start(bet);
            }
        } else {
            this.showNotification("Недостаточно средств!", 2000);
        }
    },
    
    updateUserData() {
        if (tgApp.initDataUnsafe && tgApp.initDataUnsafe.user) {
            const user = tgApp.initDataUnsafe.user;
            document.getElementById('userName').textContent = user.first_name || 'Игрок';
            
            if (user.photo_url) {
                document.getElementById('userAvatar').src = user.photo_url;
            }
        }
        
        this.updateBalance();
    },
    
    updateBalance() {
        document.getElementById('userBalance').textContent = this.userBalance;
    },
    
    addToHistory(game, amount, isWin) {
        const historyItem = {
            game,
            date: new Date().toISOString().split('T')[0],
            amount: isWin ? amount : -amount,
            win: isWin
        };
        
        Storage.addToHistory(historyItem);
        Storage.renderHistory();
    },
    
    initGameUI(game) {
        const gameContainer = document.getElementById('slotsGame');
        
        gameContainer.innerHTML = `
            <h2>${game === 'slots' ? 'Слоты' : 'Колесо фортуны'}</h2>
            <div class="game-area" id="${game}Area">
                ${game === 'slots' ? 
                    '<div class="slots-reel"></div><div class="slots-reel"></div><div class="slots-reel"></div>' : 
                    '<div class="wheel-container"></div>'}
            </div>
            <button class="play-btn" id="playBtn">Играть</button>
        `;
        
        document.getElementById('playBtn').addEventListener('click', () => {
            this.showModal('bet');
        });
    }
};

// Запускаем приложение
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});
