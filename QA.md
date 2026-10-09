# Проверка локального генератора

Проверка выполнена 8 октября 2026 года. **81 автоматический тест пройден.** Проверены декодирование, полный JSON, автоматические обновления, точные ID, офлайн-режим, повреждённые снимки, кэш изображений, модули Зиллиакса, фоны, масштабирование и локальный HTTP-сервер.

Библиотека каждого языка содержит 36 219 записей: 36 022 из HSJSON build 253932 и 197 дополнительных официальных записей. Для русского и английского языков реально загружены 206 открытых карт нового набора из публичной галереи Blizzard. OAuth для этого не использовался. Пять ранее отсутствующих ID Harold Rogue — 128389, 129340, 130112, 130761, 131456 — найдены по точному ID, с настоящими официальными изображениями.

После исправлений масштабирования и плашек заново созданы три настоящих PNG: Harold Rogue на русском и английском, колода мага пользователя на русском. Все по 30 карт, 3000 × 2344, без предупреждений. Rogue — 7280 пыли, Mage — 6320. Повторный русский Harold Rogue создан с запрещёнными сетевыми вызовами; SHA-256 PNG полностью совпал. Путь по умолчанию не читает API-ключи. Результаты и размеры исходных/масштабированных карт записаны в `verification-local.json`.

Сжатые данные двух языков занимают 3 764 237 байт. Кэш изображений после проверки — 104 384 161 байт, меньше лимита 100 МиБ. Проверены сохранение неизменившегося снимка после HTTP 304, обновление стоимости/текста/изображения, сохранение полного набора полей и переход официального дополнения в HSJSON. Отдельные тесты проверяют обновление только изменённой картинки и удаление старого кэша после завершения всех параллельных генераций.

Изображения разного разрешения теперь увеличиваются или уменьшаются до одной области с сохранением пропорций после удаления прозрачных полей. Плашка количества прилегает к нижнему краю рамки под типом карты: прозрачные поля и выступающие круги характеристик учитываются отдельно. Регрессионные тесты сравнивают реальный монтаж одной карты при двух разрешениях, подтверждают сохранность типа/текста/характеристик и проверяют сетку для 1, 17, 30, 40, 50 и 100 позиций.

Правый нижний угол остаётся пустым. Фоны проверены для всех 11 классов. Очистка чёрной подложки и диагностического текста сохраняет рамку, внутренний чёрный цвет, текст и характеристики карты.

## Предыдущая расширенная выборка

До последних изменений на публичных колодах были созданы 15 из 15 PNG: 12 на русском, 3 на английском; 427 позиций и 248 разных карт, все 11 классов. Проверки этой выборки и источники приведены ниже. После изменения расположения плашек повторно проверены три пользовательских монтажа и автоматические проверки сетки; вся историческая выборка повторно не скачивалась.

| Колода | Язык | Карты / сайдборд | Типы | Размер | Результат |
|---|---|---:|---|---|---|
| Death Knight / Corpse / Standard | ruRU | 30 / 2 | HERO, MINION, SPELL, WEAPON | 3000 × 2344 | Пройдено |
| Demon Hunter / No Minion / Standard | ruRU | 30 / 0 | LOCATION, MINION, SPELL, WEAPON | 3000 × 2344 | Пройдено |
| Druid / Owlonius / Standard | ruRU | 30 / 2 | LOCATION, MINION, SPELL | 3000 × 2344 | Пройдено |
| Hunter / Discover / Standard | ruRU | 30 / 0 | LOCATION, MINION, SPELL, WEAPON | 3000 × 2344 | Пройдено |
| Mage / Arkwing Quest / Standard | ruRU | 30 / 2 | LOCATION, MINION, SPELL | 3000 × 2344 | Пройдено |
| Paladin / Aura / Standard | ruRU | 30 / 2 | MINION, SPELL, WEAPON | 3000 × 2344 | Пройдено |
| Priest / XL Questline Reno / Wild | ruRU | 40 / 5 | HERO, LOCATION, MINION, SPELL | 3000 × 2344 | Пройдено |
| Rogue / Imbue / Standard | ruRU | 30 / 2 | MINION, SPELL, WEAPON | 3000 × 2344 | Пройдено |
| Shaman / XL Reno Shudderwock / Wild | ruRU | 40 / 5 | HERO, LOCATION, MINION, SPELL | 3000 × 2344 | Пройдено |
| Warlock / XL Quest Reno / Wild | ruRU | 40 / 3 | HERO, MINION, SPELL | 3000 × 2344 | Пройдено |
| Warrior / XL Reno / Wild | ruRU | 40 / 5 | HERO, MINION, SPELL, WEAPON | 3000 × 2344 | Пройдено |
| User Mage deck | ruRU | 30 / 0 | MINION, SPELL | 3000 × 2344 | Пройдено |
| Death Knight / Corpse / Standard | enUS | 30 / 2 | HERO, MINION, SPELL, WEAPON | 3000 × 2344 | Пройдено |
| Priest / XL Questline Reno / Wild | enUS | 40 / 5 | HERO, LOCATION, MINION, SPELL | 3000 × 2344 | Пройдено |
| User Mage deck | enUS | 30 / 0 | MINION, SPELL | 3000 × 2344 | Пройдено |

Проверяемая выборка содержит 248 разных карт: HERO — 2, LOCATION — 9, MINION — 118, SPELL — 110, WEAPON — 9. Проверка относится к этому набору колод, а не ко всей библиотеке Hearthstone.

Подготовлено 325 файлов карт для двух языков. Английских резервных изображений для русского языка: 0. Ошибок скачивания: 0.

Сайдборд в таблице содержит только игровые модули и карты: косметический модуль Зиллиакса исключён. Примеры жреца, шамана и воина проверяют несколько владельцев сайдборда; герои, локации, оружие и титаны представлены в наборе.

Источники:

- [Death Knight / Corpse / Standard](https://www.hearthstonetopdecks.com/decks/unholy-blood-corpse-death-knight-early-2-legend-%EB%9F%AC%EB%B8%94%EB%A6%AC%EC%A6%88%ED%99%94%EC%9D%B4%ED%8C%85-across-the-timeways/)
- [Demon Hunter / No Minion / Standard](https://www.hearthstonetopdecks.com/decks/no-minion-demon-hunter-early-16-legend-destruct-across-the-timeways/)
- [Druid / Owlonius / Standard](https://www.hearthstonetopdecks.com/decks/owlonius-druid-early-1-legend-rocky-across-the-timeways/)
- [Hunter / Discover / Standard](https://www.hearthstonetopdecks.com/decks/discover-hunter-1-legend-savor-across-the-timeways-2/)
- [Mage / Arkwing Quest / Standard](https://www.hearthstonetopdecks.com/decks/arkwing-quest-mage-38-legend-barrette-across-the-timeways/)
- [Paladin / Aura / Standard](https://www.hearthstonetopdecks.com/decks/aura-paladin-early-2-legend-boringpriesttt-across-the-timeways/)
- [Priest / XL Questline Reno / Wild](https://www.hearthstonetopdecks.com/decks/xl-questline-reno-priest-444-legend-mrtrenbolone-wild-s143/)
- [Rogue / Imbue / Standard](https://www.hearthstonetopdecks.com/decks/imbue-rogue-142-legend-unknown-across-the-timeways/)
- [Shaman / XL Reno Shudderwock / Wild](https://www.hearthstonetopdecks.com/decks/xl-reno-shudderwock-shaman-42-legend-chachaboizz-wild-s142/)
- [Warlock / XL Quest Reno / Wild](https://www.hearthstonetopdecks.com/decks/xl-quest-reno-warlock-127-legend-unknown-wild-s143/)
- [Warrior / XL Reno / Wild](https://www.hearthstonetopdecks.com/decks/xl-reno-warrior-1110-legend-taniaunivers-wild-s143/)
