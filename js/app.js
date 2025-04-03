const tg = window.Telegram.WebApp;
let user = null;

class App {
    static init() {
        tg.ready();
        tg.expand();
        
        user = Storage.load('user', {
            id: tg.initDataUnsafe.user?.id || Math.random().toString(36).substr(2, 9),
            balance: 1000,
            gamesPlayed: 0,
            lastLogin: new Date()
        });

        this.initUI();
        this.bindEvents();
        this.updateBalance();
    }

    static initUI() {
        const games = [
            { id: 'slots', name: 'Слоты', icon: 'assets/images/slots.png' },
            { id: 'roulette', name: 'Рулетка', icon: 'assets/images/roulette.png' },
            { id: 'blackjack', name: 'Блэкджек', icon: 'assets/images/blackjack.png' },
            { id: 'wheel', name: 'Колесо Фортуны', icon: 'assets/images/wheel.png' }
        ];

        const container = document.getElementById('game-tiles');
        container.innerHTML = games.map(game => `
            <div class="game-card" onclick="GameManager.start('${game.id}')">
                <img src="${game.icon}" class="game-icon">
                <h3>${game.name}</h3>
            </div>
        `).join('');
    }

    static bindEvents() {
        document.querySelector('.close-btn').addEventListener('click', () => {
            document.getElementById('sidebar').classList.remove('active');
        });
        
        tg.MainButton.setText('Меню');
        tg.MainButton.show();
        tg.MainButton.onClick(() => {
            document.getElementById('sidebar').classList.add('active');
        });
    }

    static updateBalance() {
        document.querySelector('.balance').textContent = user.balance;
        Storage.save('user', user);
    }

    static showModal(title, text) {
        const modal = document.getElementById('modal');
        modal.querySelector('.modal-title').textContent = title;
        modal.querySelector('.modal-text').textContent = text;
        modal.style.display = 'flex';
        
        modal.querySelector('.modal-close').onclick = () => {
            modal.style.display = 'none';
        };
    }
}

// Инициализация приложения
document.addEventListener('DOMContentLoaded', App.init);
