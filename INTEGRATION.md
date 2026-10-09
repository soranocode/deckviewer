# Подключение к дек-трекеру

Генератор отделён от страницы и HTTP-сервера: код колоды → числовые DBF ID и количество копий → записи карт → изображения → PNG.

## Готовая локальная библиотека

```python
from engine import resolve_deck, generate

deck = resolve_deck(deck_code, "ruRU")
result = generate(deck_code, "ruRU", output_dir=export_folder)
```

По умолчанию используются полные сжатые JSON из `library/`, с обновлениями в `cache/library/`. SQL-база и ключи API не нужны. Библиотека проверяет обновления раз в сутки, догружает отсутствующие числовые ID через публичную официальную галерею и сохраняет последний исправный снимок. Все поля HSJSON сохраняются, включая неизвестные будущие поля, героев и неколлекционные карты. Проверка и замена снимков выполняются до публикации; каждый язык имеет собственную блокировку.

```python
from pathlib import Path
from local_library import LocalLibrary

library = LocalLibrary(Path(cache_folder), Path(bundled_folder))
cards = library.get_cards({930, 128389, 129340}, "ruRU")
state = library.info("ruRU")
```

В HSJSON используется строковый символьный ID и числовой `dbfId`. Официальные дополнительные записи нормализованы из публичного JSON Blizzard с точным числовым ID; исходный ответ сохраняется в поле `blizzard`. Если HSJSON позже публикует такой ID, его полная запись заменяет дополнительную. Программа сохраняет `imageUrl` из официального ответа и не угадывает символьный ID карты.

## Данные и изображения трекера

```python
from card_library import index_json
from engine import generate

metadata = index_json(cards_json_bytes)
result = generate(deck_code, "ruRU", metadata=metadata,
                  image_paths={"CARD_SYMBOLIC_ID": local_png_path},
                  output_dir=export_folder)
```

`metadata` — словарь `{dbfId: card}`. Нужны основные карты, герой и все карты сайдборда. Запись содержит `dbfId`, безопасный строковый `id`, `name`, `cost`, `collectible`, `rarity`, `type`, `cardClass` и применимые `attack`, `health`, `text`, `runeCost`, `set`. Для героя обязательны `type="HERO"` и класс. `imageUrl` задаёт HTTPS-адрес полного изображения. `image_paths` имеет приоритет перед скачиванием; при передаче всех карт и изображений сеть не используется.

В проверенном Standard Tracker данные предоставляет HearthDb / `Database.GetCardFromDbfId(id, false)`: `false` сохраняет доступ к героям, токенам и модулям. Полные PNG трекер хранит отдельно от полосок иллюстраций. Его встроенный CardDefs build 251952 не содержит пяти новых карт Harold Rogue; новая библиотека уже содержит их. Перенос рендера на C# — отдельная задача; текущий Python-модуль можно вызывать локальным процессом.

`resolve_deck` возвращает `cards`, `sideboard`, `runeSlots`, `classKey`, `classId`, `cardCount`, `sideboardCount`, `dust`, `deckCode`, `heroId`, `format`, `source`. У каждой карты есть `count`. `generate` возвращает путь PNG и статистику. Ошибки для интерфейса имеют тип `DeckViewError`.

## Дополнительный адаптер разработчика

`BlizzardProvider` сохранён для явного подключения к Battle.net Game Data API через `provider=`. Этот дополнительный путь требует своих Client ID / Client Secret. Локальная страница его автоматически не включает и файл `blizzard.local.json` не читает. `ConfigureBlizzard.cmd` проверяет и сохраняет настройки только для такого дополнительного адаптера.

`CardLibrary` в `card_library.py` — необязательный SQLite-индекс полного JSON. Основной генератор не создаёт SQLite; модуль используется для строгой проверки формата JSON.
