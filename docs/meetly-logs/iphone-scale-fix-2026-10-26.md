# Масштаб Mini App на iPhone

Пользователь сообщил о скачках масштаба и уточнил, что использует iPhone.
Поля наследовали шрифт 12–14px от form-field, что может вызывать приближение
при фокусе в iOS WebView. Воспроизведение на физическом iPhone недоступно.

В live.css добавлены 16px для полей input/textarea/select на мобильной ширине
и устройствах с coarse pointer, text-size-adjust:100%, стабильная минимальная
высота app-shell через svh и максимальная высота модального окна через dvh.
touch-action:manipulation на кнопках и ссылках убирает double-tap zoom;
ручной pinch-to-zoom не запрещён. maximum-scale/user-scalable не ограничивались.

Production обновлён до 536adf8a27fdcf4215b7584d7d9a31e08c05b5d9.
Health ok/webhook; записи, конфигурация и тестовый контейнер сохранены.
Backup: /opt/calendar-bot/backups/iphone-scale-2026-10-26.
Публичный live.css отвечает 200, SHA256 совпадает с исходником с нормализованными
переносами строк. git diff --check успешен. Backend не менялся, тесты backend
повторно не запускались. Проверка открытия/закрытия клавиатуры на iPhone ожидается.
