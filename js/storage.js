// Временное хранилище данных
const Storage = {
    getHistory() {
        // В реальном приложении здесь будет запрос к серверу
        const saved = localStorage.getItem('casinoHistory');
        return saved ? JSON.parse(saved) : [
            { game: 'slots', date: '2023-05-15', amount: 150, win: true },
            { game: 'wheel', date: '2023-05-14', amount: -100, win: false },
            { game: 'slots', date: '2023-05-13', amount: 300, win: true },
            { game: 'slots', date: '2023-05-12', amount: -50, win: false }
        ];
    },
    
    addToHistory(item) {
        const history = this.getHistory();
        history.unshift(item);
        localStorage.setItem('casinoHistory', JSON.stringify(history));
    },
    
    renderHistory() {
        const history = this.getHistory();
        const historyList = document.getElementById('historyList');
        
        if (!historyList) return;
        
        historyList.innerHTML = history.map(item => `
            <div class="history-item">
                <div>
                    <span class="game-name">${item.game === 'slots' ? 'Слоты' : 'Колесо'}</span>
                    <span class="game-date">${item.date}</span>
                </div>
                <span class="game-amount ${item.win ? 'win' : 'lose'}">
                    ${item.win ? '+' : ''}${item.amount}₽
                </span>
            </div>
        `).join('');
    }
};
