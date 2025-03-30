document.addEventListener('DOMContentLoaded', () => {
    // Инициализация Telegram WebApp SDK
    const tg = window.Telegram.WebApp;
    tg.ready(); // Сообщаем Telegram, что приложение готово
    tg.expand(); // Раскрываем Mini App на всю высоту
    tg.enableClosingConfirmation(); // Предупреждать перед закрытием, если есть несохраненные данные (например, в форме)

    // Адаптация к теме Telegram
    document.body.classList.toggle('dark-mode', tg.colorScheme === 'dark');
    // Установка цвета хедера (если нужно)
    // tg.setHeaderColor(tg.themeParams.secondary_bg_color || '#f0f0f0');

    // --- Переменные состояния ---
    let currentFilters = { // Текущие активные фильтры
        city: '',
        rooms: '',
        min_price: '',
        max_price: ''
    };
    let currentOffsets = { // Текущие смещения для пагинации
        kufar: 0,
        onliner: 0,
        user: 0
    };
    let isLoading = false; // Флаг текущей загрузки
    let hasMore = true; // Флаг наличия доп. объявлений

    // Константа городов (дублирует бэкенд для удобства)
    const CITIES = {
        "minsk": "🏙️ Минск", "brest": "🌇 Брест", "grodno": "🌃 Гродно",
        "gomel": "🌆 Гомель", "vitebsk": "🏙 Витебск", "mogilev": "🏞️ Могилев",
    };


    // --- DOM Элементы ---
    const citySelect = document.getElementById('city-select');
    const roomsSelect = document.getElementById('rooms-select');
    const minPriceInput = document.getElementById('min-price');
    const maxPriceInput = document.getElementById('max-price');
    const applyFiltersBtn = document.getElementById('apply-filters-btn');
    const adsListContainer = document.getElementById('ads-list');
    const loadingIndicator = document.getElementById('loading-indicator');
    const loadMoreBtn = document.getElementById('load-more-btn');
    const noResultsMsg = document.getElementById('no-results');
    const submitAdBtn = document.getElementById('submit-ad-btn');
    const modal = document.getElementById('submit-ad-modal');
    const closeModalBtn = modal.querySelector('.close-button');
    const submitAdForm = document.getElementById('submit-ad-form');
    const submitFormBtn = document.getElementById('submit-form-btn'); // Кнопка отправки формы
    const submitMessage = document.getElementById('submit-message');
    const adCitySelect = document.getElementById('ad-city');
    const userIdInput = document.getElementById('user-id'); // Скрытое поле для ID пользователя


    // --- Заполнение выпадающих списков городов ---
    function populateCitySelects() {
        const createOption = (value, text) => {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = text;
            return option;
        };

        // Селект в фильтрах
        citySelect.innerHTML = '<option value="">Все города</option>'; // Сброс
        for (const [key, value] of Object.entries(CITIES)) {
            citySelect.appendChild(createOption(key, value));
        }

        // Селект в модальном окне
        adCitySelect.innerHTML = '<option value="" disabled selected>Выберите город...</option>'; // Сброс
         for (const [key, value] of Object.entries(CITIES)) {
            adCitySelect.appendChild(createOption(key, value));
        }
    }


    // --- Функция запроса объявлений к API ---
    async function fetchAds(filters, offsets, isLoadMore = false) {
        if (isLoading) {
            console.log("Запрос уже выполняется, пропуск.");
            return; // Предотвращение параллельных запросов
        }
        isLoading = true;
        showLoading(true, isLoadMore); // Показать индикатор загрузки
        noResultsMsg.style.display = 'none'; // Скрыть сообщение "нет результатов"

        // Формирование параметров URL запроса
        const params = new URLSearchParams();
        if (filters.city) params.append('city', filters.city);
        if (filters.rooms) params.append('rooms', filters.rooms);
        // Проверяем, что цены - числа перед отправкой
        if (filters.min_price && !isNaN(filters.min_price)) params.append('min_price', filters.min_price);
        if (filters.max_price && !isNaN(filters.max_price)) params.append('max_price', filters.max_price);

        // Добавление смещений для пагинации
        // Отправляем 0, если смещение null или undefined (для первого запроса)
        params.append('kufar_offset', offsets?.kufar ?? 0);
        params.append('onliner_offset', offsets?.onliner ?? 0);
        params.append('user_offset', offsets?.user ?? 0);


        try {
            // Логирование запроса
            console.log(`Запрос: /api/ads?${params.toString()}`);
            const response = await fetch(`/api/ads?${params.toString()}`);

            if (!response.ok) {
                // Обработка HTTP ошибок
                const errorData = await response.json().catch(() => ({})); // Попытка получить тело ошибки
                console.error(`HTTP ошибка! Статус: ${response.status}`, errorData);
                throw new Error(`HTTP ошибка ${response.status}: ${errorData.error || response.statusText}`);
            }

            const data = await response.json();
            console.log("Получены данные:", data); // Логирование полученных данных

            renderAds(data.ads || [], !isLoadMore); // Отрисовка объявлений (очистка, если не "загрузить еще")

            // Обновление состояния для следующей загрузки
            currentOffsets = data.next_offsets || { kufar: null, onliner: null, user: null };
            // Устанавливаем hasMore = false только если ВСЕ next_offsets равны null
            hasMore = Object.values(currentOffsets).some(offset => offset !== null);

            // Показать/скрыть кнопку "Загрузить еще"
            loadMoreBtn.style.display = hasMore ? 'block' : 'none';

             // Показать сообщение "Нет результатов", если это первый запрос и список пуст
            if (!isLoadMore && (!data.ads || data.ads.length === 0)) {
                noResultsMsg.style.display = 'block';
            }

        } catch (error) {
            console.error("Ошибка при получении объявлений:", error);
            tg.HapticFeedback.notificationOccurred('error'); // Виброотклик об ошибке
            noResultsMsg.textContent = `Ошибка загрузки: ${error.message}. Попробуйте позже.`;
            noResultsMsg.style.display = 'block';
            loadMoreBtn.style.display = 'none'; // Скрыть кнопку при ошибке
        } finally {
            isLoading = false; // Сброс флага загрузки
            showLoading(false, isLoadMore); // Скрыть индикатор загрузки
        }
    }

    // --- Функции отрисовки ---
    function renderAds(ads, clearPrevious = false) {
        if (clearPrevious) {
            // Очищаем контейнер, но сохраняем служебные элементы (загрузчик, нет рез-в)
            const serviceElements = adsListContainer.querySelectorAll('.loading-indicator, .no-results');
            adsListContainer.innerHTML = '';
            serviceElements.forEach(el => adsListContainer.appendChild(el));
        }

        if (!ads || ads.length === 0) {
             // Если объявлений нет, сообщение "Нет результатов" будет показано в fetchAds
             return;
        }

        const fragment = document.createDocumentFragment(); // Используем фрагмент для эффективности
        ads.forEach(ad => {
            // Создаем карточку как ссылку
            const cardLink = createAdCardLink(ad);
            fragment.appendChild(cardLink);
        });

        // Вставляем новые объявления ПЕРЕД индикатором загрузки
        adsListContainer.insertBefore(fragment, loadingIndicator);
    }

    function createAdCardLink(ad) {
        // Создаем ссылку <a> вместо <div>
        const cardLink = document.createElement('a');
        cardLink.className = 'ad-card-link';
        cardLink.href = ad.link; // Устанавливаем ссылку на объявление
        cardLink.target = '_blank'; // Открывать в новой вкладке (или использовать tg.openLink)
        cardLink.rel = 'noopener noreferrer'; // Атрибуты безопасности для target="_blank"
        cardLink.addEventListener('click', (e) => {
             e.preventDefault(); // Предотвращаем стандартный переход по ссылке
             tg.HapticFeedback.impactOccurred('light'); // Виброотклик при клике
             if(ad.link.startsWith('user_ad_')) {
                // Если это пользовательское объявление, может быть, открыть его детали внутри приложения?
                // Пока просто открываем ссылку, если она есть (хотя для user_ad это заглушка)
                console.log("Клик по пользовательскому объявлению (ссылка-заглушка):", ad.link);
                // Здесь можно реализовать показ деталей внутри Mini App
                tg.showAlert(`Детали объявления:\nИсточник: ${ad.source}\nЦена: $${ad.price}\nТелефон: ${ad.phone || 'не указан'}`);
             } else {
                tg.openLink(ad.link); // Открываем внешнюю ссылку через Telegram
             }
        });

        // Изображение или плейсхолдер
        const imageWrapper = document.createElement('div');
        imageWrapper.className = 'ad-image-wrapper';
        if (ad.image && ad.image.startsWith('/')) {
            // Если путь относительный (загруженное фото), формируем полный URL
             // Используем location.origin для получения базового URL бэкенда
             ad.image = `${window.location.origin}${ad.image}`;
        }
        imageWrapper.innerHTML = ad.image
             ? `<img src="${ad.image}" alt="Фото квартиры" loading="lazy" onerror="this.style.display='none'; this.parentElement.querySelector('.image-placeholder').style.display='flex';"> <div class="image-placeholder" style="display:none;">🖼️</div>` // Показываем плейсхолдер при ошибке загрузки img
             : `<div class="image-placeholder" style="display:flex;">🖼️</div>`; // Плейсхолдер, если изображения нет

        // Контент карточки
        const content = document.createElement('div');
        content.className = 'ad-content';

        // Определение текста для комнат
        let roomsText = '? комн.';
        if (ad.rooms === 0) roomsText = 'Студия';
        else if (ad.rooms === 1) roomsText = '1 комната';
        else if (ad.rooms >= 2 && ad.rooms <= 4) roomsText = `${ad.rooms} комнаты`;
        else if (ad.rooms >= 5) roomsText = `${ad.rooms} комнат`;

        content.innerHTML = `
            <div> <div class="ad-price">$${ad.price?.toLocaleString('ru-RU') ?? '???'}</div>
                <div class="ad-details">
                    ${ad.rooms !== null ? `<span><span class="icon">🛏️</span> ${roomsText}</span>` : ''}
                    ${ad.address ? `<span><span class="icon">📍</span> ${ad.address}</span>` : ''}
                </div>
                 <div class="ad-source">
                     <span class="icon">${ad.source === 'User' ? '👤' : '🌐'}</span>
                     ${ad.source === 'User' ? 'Частное объявление' : ad.source}
                     ${ad.source === 'User' && ad.phone ? `<a href="tel:${ad.phone}" class="ad-phone-link" onclick="event.stopPropagation(); tg.HapticFeedback.impactOccurred('light');">(тел.)</a>` : ''}
                 </div>
            </div>
            ${ad.description ? `<p class="ad-description">${ad.description}</p>` : ''}
        `;

        cardLink.appendChild(imageWrapper);
        cardLink.appendChild(content);

        return cardLink;
    }

    function showLoading(show, isLoadMore = false) {
        if (show) {
            if (isLoadMore) {
                // При дозагрузке показываем спиннер внизу и скрываем кнопку
                loadMoreBtn.style.display = 'none';
                loadingIndicator.style.display = 'flex'; // Используем flex для центрирования
            } else {
                // При первичной загрузке или смене фильтров очищаем список и показываем спиннер
                 const serviceElements = adsListContainer.querySelectorAll('.loading-indicator, .no-results');
                 adsListContainer.innerHTML = ''; // Очистить
                 serviceElements.forEach(el => adsListContainer.appendChild(el)); // Вернуть служебные
                 loadingIndicator.style.display = 'flex';
                 noResultsMsg.style.display = 'none';
            }
        } else {
            // Скрываем спиннер
            loadingIndicator.style.display = 'none';
            // Показываем кнопку "Загрузить еще", если есть еще объявления и не идет загрузка
             if (hasMore && !isLoading) {
                 loadMoreBtn.style.display = 'block';
             } else if (!hasMore) {
                 loadMoreBtn.style.display = 'none'; // Убедимся, что кнопка скрыта, если больше нет
             }
        }
    }

    // --- Обработчики событий ---
    applyFiltersBtn.addEventListener('click', () => {
        tg.HapticFeedback.impactOccurred('medium'); // Виброотклик при применении фильтров

        // Обновляем объект фильтров из полей ввода
        currentFilters.city = citySelect.value;
        currentFilters.rooms = roomsSelect.value;
        currentFilters.min_price = minPriceInput.value.trim(); // Удаляем пробелы
        currentFilters.max_price = maxPriceInput.value.trim();

        // Сбрасываем смещения и флаг 'hasMore' перед новым поиском
        currentOffsets = { kufar: 0, onliner: 0, user: 0 };
        hasMore = true; // Предполагаем, что результаты будут
        fetchAds(currentFilters, currentOffsets, false); // false = это не дозагрузка
    });

    loadMoreBtn.addEventListener('click', () => {
        tg.HapticFeedback.impactOccurred('light'); // Легкий виброотклик
        // Используем текущие фильтры и обновленные смещения
        if (hasMore && !isLoading) { // Доп. проверка перед запросом
           fetchAds(currentFilters, currentOffsets, true); // true = это дозагрузка
        }
    });

     // --- Логика модального окна ---
    submitAdBtn.addEventListener('click', () => {
        tg.HapticFeedback.impactOccurred('light');
        // Заполняем user_id из данных Telegram при открытии модалки
        if (tg.initDataUnsafe?.user?.id) {
             userIdInput.value = tg.initDataUnsafe.user.id;
             console.log("User ID установлен:", userIdInput.value);
         } else {
             // Обработка случая, если ID недоступен (не должно происходить в норм. окружении)
             console.warn("ID пользователя Telegram не найден в initDataUnsafe. Отправка может не сработать.");
             // Можно показать предупреждение пользователю
             submitMessage.textContent = 'Ошибка: Не удалось получить ID пользователя Telegram.';
             submitMessage.className = 'submit-message error';
             submitMessage.style.display = 'block';
             // Возможно, стоит заблокировать кнопку отправки
             submitFormBtn.disabled = true;
         }
        submitAdForm.reset(); // Очистить форму перед показом
        submitMessage.style.display = 'none'; // Скрыть предыдущие сообщения
        submitFormBtn.disabled = false; // Разблокировать кнопку
        modal.style.display = 'block'; // Показать модальное окно
        tg.BackButton.show(); // Показать стандартную кнопку Назад Telegram
    });

    const closeModal = () => {
        modal.style.display = 'none';
        tg.BackButton.hide(); // Скрыть кнопку Назад Telegram
    }

    closeModalBtn.addEventListener('click', closeModal);

    // Закрытие модалки при клике вне её области
    window.addEventListener('click', (event) => {
        if (event.target == modal) {
            closeModal();
        }
    });

    // Обработка клика по стандартной кнопке "Назад" Telegram
    tg.BackButton.onClick(closeModal);


    // --- Отправка формы подачи объявления ---
    submitAdForm.addEventListener('submit', async (event) => {
        event.preventDefault(); // Предотвращаем стандартную отправку формы
        tg.HapticFeedback.impactOccurred('medium'); // Виброотклик при отправке

        // Блокируем кнопку и показываем сообщение об отправке
        submitFormBtn.disabled = true;
        submitMessage.textContent = 'Отправка данных...';
        submitMessage.className = 'submit-message'; // Убираем классы success/error
        submitMessage.style.display = 'block';

        const formData = new FormData(submitAdForm); // Собираем данные формы

        // Логирование данных формы (для отладки, можно убрать в продакшене)
        // console.log("Отправляемые данные формы:");
        // for (let [key, value] of formData.entries()) {
        //     // Для файлов выводим имя и размер
        //     if (value instanceof File) {
        //         console.log(`${key}: ${value.name} (${value.size} bytes)`);
        //     } else {
        //         console.log(`${key}: ${value}`);
        //     }
        // }

        try {
            const response = await fetch('/api/submit_user_ad', {
                method: 'POST',
                body: formData // FormData отправляется как multipart/form-data
                // Заголовки Content-Type устанавливаются автоматически для FormData
            });

            const result = await response.json(); // Пытаемся разобрать ответ как JSON

            if (response.ok) { // Статус 2xx (например, 201 Created)
                submitMessage.textContent = result.message || 'Объявление успешно отправлено на модерацию!';
                submitMessage.className = 'submit-message success';
                tg.HapticFeedback.notificationOccurred('success'); // Виброотклик успеха
                // Очищаем форму после успешной отправки
                submitAdForm.reset();
                // Закрываем модальное окно через пару секунд
                setTimeout(() => {
                    closeModal();
                }, 2500);
                // Можно опционально обновить список объявлений (но user ad появится только после модерации)
                // applyFiltersBtn.click();

            } else { // Статус 4xx или 5xx
                 let errorText = `Ошибка ${response.status}: ${result.error || 'Не удалось отправить объявление.'}`;
                 // Если есть детали валидации, добавляем их
                 if (result.details) {
                     errorText += " Детали: " + Object.values(result.details).join(' ');
                 }
                 submitMessage.textContent = errorText;
                 submitMessage.className = 'submit-message error';
                 tg.HapticFeedback.notificationOccurred('error'); // Виброотклик ошибки
                 submitFormBtn.disabled = false; // Разблокируем кнопку для повторной попытки
            }

        } catch (error) { // Сетевые ошибки или ошибки парсинга JSON
            console.error("Ошибка при отправке формы:", error);
            submitMessage.textContent = `Сетевая ошибка или ошибка ответа сервера: ${error.message}. Попробуйте снова.`;
            submitMessage.className = 'submit-message error';
            tg.HapticFeedback.notificationOccurred('error');
            submitFormBtn.disabled = false; // Разблокируем кнопку
        }
    });


    // --- Начальная загрузка данных ---
    populateCitySelects(); // Заполняем списки городов
    fetchAds(currentFilters, currentOffsets); // Загружаем первые объявления без фильтров

     // Опционально: Отмечаем объявления как просмотренные при загрузке приложения
     fetch('/api/mark_ads_viewed', { method: 'POST' })
        .then(response => response.ok ? response.json() : Promise.reject(`Статус: ${response.status}`))
        .then(data => console.log("Отметка о просмотре:", data.message || "OK"))
        .catch(error => console.error("Ошибка при отметке объявлений как просмотренных:", error));

     // Пример использования MainButton Telegram (если нужна главная кнопка действия)
     // tg.MainButton.setText("Обновить список");
     // tg.MainButton.show();
     // tg.MainButton.onClick(() => {
     //     tg.HapticFeedback.impactOccurred('light');
     //     // Выполняем действие, например, принудительно обновляем с текущими фильтрами
     //     fetchAds(currentFilters, { kufar: 0, onliner: 0, user: 0 }, false);
     // });

});