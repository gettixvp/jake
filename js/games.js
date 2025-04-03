class GameManager {
    static start(gameId) {
        document.getElementById('sidebar').classList.remove('active');
        
        switch(gameId) {
            case 'slots':
                this.startSlots();
                break;
            case 'roulette':
                this.startRoulette();
                break;
            case 'blackjack':
                this.startBlackjack();
                break;
            case 'wheel':
                this.startWheel();
                break;
        }
    }

    static startSlots() {
        if (user.balance < 50) return App.showModal('Ошибка', 'Недостаточно средств!');
        user.balance -= 50;
        
        const results = [
            Math.floor(Math.random() * 5),
            Math.floor(Math.random() * 5),
            Math.floor(Math.random() * 5)
        ];

        const win = results[0] === results[1] && results[1] === results[2];
        if (win) {
            user.balance += 200;
            App.showModal('Победа!', 'Вы выиграли 200 Stars!');
        } else {
            App.showModal('Повезет в следующий раз!', 'Попробуйте еще раз');
        }
        
        App.updateBalance();
        this.playSound(win ? 'win.mp3' : 'lose.mp3');
    }

    static playSound(file) {
        new Audio(`assets/sounds/${file}`).play().catch(() => {});
    }
}
