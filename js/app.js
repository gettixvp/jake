// Инициализация Telegram WebApp
const tgApp = Telegram.WebApp;

// Основной объект приложения
const App = {
    currentGame: null,
    userBalance: 5000,
    userId: null,
    
    init() {
        tgApp.expand();
        tgApp.enableClosingConfirmation();
        tgApp.MainButton.setText('МЕНЮ').show();
        
        this.setupEventListeners();
        this.updateUserData();
        this.showNotification("Добро пожаловать в Star Casino!");
        Storage.renderHistory();
        
        // Инициализация звуков
        this.setupSounds();
    },
    
    setupEventListeners() {
        // Навигация
        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this.playSound('click');
                const section = btn.getAttribute('data-section');
                this.navigateTo(section);
            });
        });
        
        // Игры на главной
        document.querySelectorAll('.game-card').forEach(card => {
            card.addEventListener('click', () => {
                this.playSound('click');
                const game = card.getAttribute('data-game');
                this.startGame(game);
            });
        });
        
        // Кнопки профиля
        document.getElementById('depositBtn').addEventListener('click', () => {
            this.playSound('click');
            this.showModal('deposit');
        });
        
        document.getElementById('withdrawBtn').addEventListener('click', () => {
            this.playSound('click');
            this.showModal('withdraw');
        });
        
        // Модальные окна
        document.querySelector('.close-modal').addEventListener('click', () => {
            this.playSound('click');
            this.hideModal();
        });
        
        // Ставки
        document.querySelectorAll('.chip').forEach(chip => {
            chip.addEventListener('click', () => {
                this.playSound('click');
                const bet = parseInt(chip.getAttribute('data-bet'));
                this.placeBet(bet);
            });
        });
        
        // Кнопка меню Telegram
        tgApp.MainButton.onClick(() => {
            this.playSound('click');
            this.showMainMenu();
        });
    },
    
    setupSounds() {
        this.sounds = {
            click: document.getElementById('clickSound'),
            spin: document.getElementById('spinSound'),
            win: document.getElementById('winSound'),
            lose: document.getElementById('loseSound')
        };
    },
    
    playSound(type) {
        if (this.sounds[type]) {
            this.sounds[type].currentTime = 0;
            this.sounds[type].play();
        }
    },
    
    navigateTo(section) {
        document.querySelectorAll('.page-section').forEach(sec => {
            sec.classList.remove('active');
        });
        
        document.getElementById(`${section}Section`).classList.add('active');
        
        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.classList.remove('active');
            if (btn.getAttribute('data-section') === section) {
                btn.classList.add('active');
            }
        });
        
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
            this.showNotification(`Ставка ${bet.toLocaleString()}₽ принята!`);
            
            if (navigator.vibrate) navigator.vibrate(50);
            
            if (this.currentGame === 'slots') {
                Slots.start(bet);
            } else if (this.currentGame === 'wheel') {
                Wheel.start(bet);
            }
        } else {
            this.showNotification("Недостаточно средств!", 2000);
            if (navigator.vibrate) navigator.vibrate(200);
        }
    },
    
    updateUserData() {
        if (tgApp.initDataUnsafe?.user) {
            const user = tgApp.initDataUnsafe.user;
            this.userId = user.id;
            
            document.getElementById('userName').textContent = 
                user.first_name || 'Игрок';
            document.getElementById('userId').textContent = user.id;
            
            if (user.photo_url) {
                document.getElementById('userAvatar').src = user.photo_url;
            }
        }
        
        this.updateBalance();
    },
    
    updateBalance() {
        document.getElementById('userBalance').textContent = 
            this.userBalance.toLocaleString();
    },
    
    addToHistory(game, amount, isWin) {
        const historyItem = {
            game,
            date: new Date().toLocaleDateString(),
            amount: isWin ? amount : -amount,
            win: isWin
        };
        
        Storage.addToHistory(historyItem);
        Storage.renderHistory();
    },
    
    initGameUI(game) {
        const gameContainer = document.getElementById('slotsGame');
        
        if (game === 'slots') {
            gameContainer.innerHTML = `
                <div class="slots-container">
                    <h2>Игровые автоматы</h2>
                    <div class="slots-reels" id="slotsReels">
                        <div class="slots-reel" id="reel1"></div>
                        <div class="slots-reel" id="reel2"></div>
                        <div class="slots-reel" id="reel3"></div>
                    </div>
                    <button class="spin-button" id="spinBtn">Крутить</button>
                </div>
            `;
            
            Slots.init();
        } else if (game === 'wheel') {
            gameContainer.innerHTML = `
                <div class="wheel-container">
                    <h2>Колесо фортуны</h2>
                    <!-- Колесо будет здесь -->
                </div>
            `;
            
            Wheel.init();
        }
    },
    
    showMainMenu() {
        // Можно реализовать дополнительное меню
        this.showNotification("Меню открыто");
    }
};

// Запуск приложения
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});
