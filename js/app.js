// js/app.js
const tg = window.Telegram.WebApp;
tg.ready();

// Глобальные переменные
let user = Storage.load('user', {
    id: tg.initDataUnsafe.user?.id || 'guest',
    balance: 150,
    depositHistory: [],
    winHistory: [],
    transactions: []
});

let settings = Storage.load('settings', {
    soundEnabled: true,
    theme: 'dark' // Новый дизайн всегда темный
});

// Полноэкранный режим (с проверкой поддержки)
function enableFullscreen() {
    if (typeof tg.requestFullscreen === 'function') {
        tg.requestFullscreen();
    } else {
        console.log('Fullscreen mode is not supported in this Telegram version.');
    }
}
enableFullscreen();

// Фиксация ориентации (с проверкой поддержки)
if (typeof tg.setDeviceOrientation === 'function') {
    tg.setDeviceOrientation('landscape');
} else {
    console.log('Device orientation setting is not supported in this Telegram version.');
}

// Обработка событий (с проверкой поддержки)
if (tg.onEvent) {
    tg.onEvent('fullscreenChanged', () => {
        console.log('Fullscreen mode changed:', tg.isFullscreen);
    });
    tg.onEvent('fullscreenFailed', () => {
        showModal('Не удалось включить полноэкранный режим.');
    });
    tg.onEvent('activated', () => {
        console.log('Mini App activated');
    });
    tg.onEvent('deactivated', () => {
        console.log('Mini App deactivated');
    });
}

// Авторизация через Telegram
async function authenticate() {
    const initData = tg.initData;
    if (!initData) {
        showModal('Не удалось авторизоваться. Пожалуйста, откройте приложение через Telegram.');
        return;
    }
    try {
        const response = await fetch('/auth', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData })
        });
        const data = await response.json();
        if (data.token) {
            user.id = tg.initDataUnsafe.user.id;
            user.username = tg.initDataUnsafe.user.username;
            Storage.save('user', user);
            showModal(`Добро пожаловать, ${user.username}!`);
        } else {
            showModal('Ошибка авторизации.');
        }
    } catch (error) {
        showModal('Ошибка авторизации: ' + error.message);
    }
}
authenticate();

// Обновление баланса
function updateBalance() {
    const balanceElement = document.querySelector('.balance');
    if (balanceElement) {
        balanceElement.textContent = user.balance;
    }
}
updateBalance();

// Рендеринг главного экрана
const games = [
    { name: 'Слоты', id: 'slots', icon: 'assets/images/slots.png' },
    { name: 'Рулетка', id: 'roulette', icon: 'assets/images/roulette.png' },
    { name: 'Блэкджек', id: 'blackjack', icon: 'assets/images/blackjack.png' },
    { name: 'Колесо Фортуны', id: 'wheel', icon: 'assets/images/wheel.png' }
];

function renderMainScreen() {
    const gameTiles = document.getElementById('game-tiles');
    if (!gameTiles) {
        console.error('Game tiles container not found!');
        return;
    }
    gameTiles.innerHTML = '';
    games.forEach(game => {
        const tile = document.createElement('div');
        tile.classList.add('tile');
        tile.innerHTML = `
            <img src="${game.icon}" alt="${game.name}" onclick="GameManager.startGame('${game.id}')">
            <h3>${game.name}</h3>
        `;
        gameTiles.appendChild(tile);
    });
}
renderMainScreen();

// Боковое меню через три точки
const sidebar = document.getElementById('sidebar');
if (tg.MainButton) {
    tg.MainButton.setText('Меню');
    tg.MainButton.show();
    tg.MainButton.onClick(() => {
        if (sidebar) {
            sidebar.classList.add('active');
        }
    });
}

const menuItems = [
    { name: 'Главный экран', action: renderMainScreen, icon: 'assets/images/home.png' },
    { name: 'Профиль', action: showProfile, icon: 'assets/images/profile.png' },
    { name: 'Пополнить', action: showDeposit, icon: 'assets/images/deposit.png' },
    { name: 'Вывести', action: showWithdraw, icon: 'assets/images/withdraw.png' },
    { name: 'История транзакций', action: showTransactionHistory, icon: 'assets/images/history.png' },
    { name: 'Настройки', action: showSettings, icon: 'assets/images/settings.png' }
];

const sidebarMenu = document.getElementById('sidebar-menu');
if (sidebarMenu) {
    sidebarMenu.innerHTML = menuItems.map(item => `
        <li onclick="${item.action.name}()">
            <img src="${item.icon}" alt="${item.name}">
            ${item.name}
        </li>
    `).join('');
}

const closeBtn = document.querySelector('.close-btn');
if (closeBtn) {
    closeBtn.addEventListener('click', () => {
        if (sidebar) {
            sidebar.classList.remove('active');
        }
    });
}

// Функции меню
function showProfile() {
    showModal(`Профиль:\nID: ${user.id}\nИмя: ${user.username}\nБаланс: ⭐ ${user.balance}`);
    sidebar.classList.remove('active');
}

function showDeposit() {
    const amount = prompt('Введите сумму для пополнения (мин. 50 Stars):', '');
    if (!amount || isNaN(amount) || amount < 50) {
        showModal('Некорректная сумма! Минимум 50 Stars.');
        return;
    }
    user.balance += parseInt(amount);
    user.depositHistory.push({ date: new Date().toLocaleString(), amount: parseInt(amount) });
    user.transactions.push({ type: 'deposit', amount: parseInt(amount), date: new Date().toLocaleString() });
    updateBalance();
    showModal(`Баланс пополнен на ⭐ ${amount}!`);
    Storage.save('user', user);
    sidebar.classList.remove('active');
}

function showWithdraw() {
    const amount = prompt('Введите сумму для вывода (мин. 50 Stars):', '');
    if (!amount || isNaN(amount) || amount < 50 || amount > user.balance) {
        showModal('Некорректная сумма! Минимум 50 Stars, не больше баланса.');
        return;
    }
    user.balance -= parseInt(amount);
    user.transactions.push({ type: 'withdraw', amount: parseInt(amount), date: new Date().toLocaleString() });
    updateBalance();
    showModal(`Вывод ⭐ ${amount} выполнен!`);
    Storage.save('user', user);
    sidebar.classList.remove('active');
}

function showTransactionHistory() {
    const historyText = user.transactions.length > 0 
        ? user.transactions.map(t => `${t.date}: ${t.type === 'deposit' ? 'Пополнение' : 'Вывод'} ⭐ ${t.amount}`).join('\n')
        : 'История транзакций пуста.';
    showModal(`История транзакций:\n${historyText}`);
    sidebar.classList.remove('active');
}

function showSettings() {
    const gameTiles = document.getElementById('game-tiles');
    if (gameTiles) {
        gameTiles.innerHTML = `
            <div class="game-screen">
                <h2>Настройки</h2>
                <div class="game-content">
                    <label>
                        Звук: 
                        <input type="checkbox" id="sound-toggle" ${settings.soundEnabled ? 'checked' : ''}>
                    </label>
                    <label>
                        Тема: 
                        <select id="theme-select">
                            <option value="dark" selected>Темная</option>
                        </select>
                    </label>
                    <button onclick="saveSettings()">Сохранить</button>
                    <button class="back-btn" onclick="renderMainScreen()">Назад</button>
                </div>
            </div>
        `;
    }
    sidebar.classList.remove('active');
}

function saveSettings() {
    settings.soundEnabled = document.getElementById('sound-toggle').checked;
    settings.theme = document.getElementById('theme-select').value;
    Storage.save('settings', settings);
    showModal('Настройки сохранены!');
}

// Модальное окно
function showModal(text) {
    const modal = document.getElementById('modal');
    const modalText = document.getElementById('modal-text');
    if (modal && modalText) {
        modalText.textContent = text;
        modal.style.display = 'flex';
    }
}

const modalCloseBtn = document.querySelector('.modal-close-btn');
if (modalCloseBtn) {
    modalCloseBtn.addEventListener('click', () => {
        const modal = document.getElementById('modal');
        if (modal) {
            modal.style.display = 'none';
        }
    });
}
