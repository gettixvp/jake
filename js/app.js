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
    theme: 'light'
});

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
    document.querySelector('.balance').textContent = user.balance;
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
    gameTiles.innerHTML = '';
    games.forEach(game => {
        const tile = document.createElement('div');
        tile.classList.add('tile');
        tile.innerHTML = `
            <img src="${game.icon}" alt="${game.name}">
            <h3>${game.name}</h3>
            <button onclick="GameManager.startGame('${game.id}')">Играть</button>
        `;
        gameTiles.appendChild(tile);
    });
}
renderMainScreen();

// Боковое меню
const menuItems = [
    { name: 'Главный экран', action: renderMainScreen, icon: 'assets/images/home.png' },
    { name: 'Профиль', action: showProfile, icon: 'assets/images/profile.png' },
    { name: 'Пополнить', action: showDeposit, icon: 'assets/images/deposit.png' },
    { name: 'Вывести', action: showWithdraw, icon: 'assets/images/withdraw.png' },
    { name: 'История транзакций', action: showTransactionHistory, icon: 'assets/images/history.png' },
    { name: 'Настройки', action: showSettings, icon: 'assets/images/settings.png' }
];

const sidebarMenu = document.getElementById('sidebar-menu');
sidebarMenu.innerHTML = menuItems.map(item => `
    <li onclick="${item.action.name}()">
        <img src="${item.icon}" alt="${item.name}">
        ${item.name}
    </li>
`).join('');

const menuBtn = document.querySelector('.menu-btn');
const closeBtn = document.querySelector('.close-btn');
const sidebar = document.getElementById('sidebar');
menuBtn.addEventListener('click', () => sidebar.classList.add('active'));
closeBtn.addEventListener('click', () => sidebar.classList.remove('active'));

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
    gameTiles.innerHTML = `
        <div class="game-screen">
            <h2>Настройки</h2>
            <label>
                Звук: 
                <input type="checkbox" id="sound-toggle" ${settings.soundEnabled ? 'checked' : ''}>
            </label>
            <label>
                Тема: 
                <select id="theme-select">
                    <option value="light" ${settings.theme === 'light' ? 'selected' : ''}>Светлая</option>
                    <option value="dark" ${settings.theme === 'dark' ? 'selected' : ''}>Темная</option>
                </select>
            </label>
            <button onclick="saveSettings()">Сохранить</button>
            <button class="back-btn" onclick="renderMainScreen()">Назад</button>
        </div>
    `;
    sidebar.classList.remove('active');
}

function saveSettings() {
    settings.soundEnabled = document.getElementById('sound-toggle').checked;
    settings.theme = document.getElementById('theme-select').value;
    Storage.save('settings', settings);
    applyTheme();
    showModal('Настройки сохранены!');
}

function applyTheme() {
    document.body.className = settings.theme;
    if (settings.theme === 'dark') {
        document.body.style.background = '#1A1A1A';
        document.querySelector('.container').style.background = 'linear-gradient(180deg, #1A1A1A, #2A2A2A)';
    } else {
        document.body.style.background = '#F5F7FA';
        document.querySelector('.container').style.background = 'linear-gradient(180deg, #F5F7FA, #E8ECEF)';
    }
}
applyTheme();

// Модальное окно
function showModal(text) {
    document.getElementById('modal-text').textContent = text;
    document.getElementById('modal').style.display = 'flex';
}

document.querySelector('.modal-close-btn').addEventListener('click', () => {
    document.getElementById('modal').style.display = 'none';
});