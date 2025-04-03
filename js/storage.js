// js/storage.js
class Storage {
    static save(key, value) {
        localStorage.setItem(key, JSON.stringify(value));
    }

    static load(key, defaultValue) {
        const data = localStorage.getItem(key);
        return data ? JSON.parse(data) : defaultValue;
    }
}