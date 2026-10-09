"""Interactive local configuration; keys are never entered in the web UI."""
import getpass
import json

from blizzard_provider import BlizzardProvider, BlizzardProviderError
from engine import BASE_DIR, _atomic_write


def main():
    print("Настройка официального Blizzard API")
    print("Создай свой API-клиент: https://develop.battle.net/access/clients")
    client_id = input("Client ID: ").strip()
    client_secret = getpass.getpass("Client Secret (ввод скрыт): ").strip()
    region = input("Регион eu/us [eu]: ").strip() or "eu"
    try:
        provider = BlizzardProvider(client_id, client_secret, region=region)
        cards = provider.get_cards([559], "ruRU")
        if 559 not in cards:
            print("Blizzard не вернул проверочную карту. Настройки не сохранены.")
            return 1
    except BlizzardProviderError as error:
        print(str(error))
        return 1
    settings = {"clientId": client_id, "clientSecret": client_secret, "region": region}
    _atomic_write(BASE_DIR / "blizzard.local.json", json.dumps(settings).encode("utf-8"))
    print("Подключение проверено, настройки сохранены для дополнительного адаптера разработчика.")
    print("Локальная страница использует встроенную библиотеку и не требует этих ключей.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nНастройка отменена.")
        raise SystemExit(1)
