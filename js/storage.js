class Storage {
    static load(key, defaultValue) {
        try {
            const data = localStorage.getItem(key);
            return data ? JSON.parse(data) : defaultValue;
        } catch {
            return defaultValue;
        }
    }

    static save(key, value) {
        localStorage.setItem(key, JSON.stringify(value));
    }
}
