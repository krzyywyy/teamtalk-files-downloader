# TT Downloader Bot

Bot dla TeamTalk 5, który po komendzie **„pobierz pliki”** pobiera wszystkie pliki
z jednego lub wielu kanałów na serwerze i zapisuje je na dysku w folderach
nazwanych tak jak kanały.

Bot korzysta z TeamTalk SDK (`TeamTalk_DLL`) oraz wrappera Pythona (`TeamTalkPy`).

## Funkcje

- logowanie na serwer TT5 i dołączanie do zadanego kanału startowego,
- obsługa wielu profili serwera (host, porty, login, hasła),
- obsługa profili kanałów do pobrania (lista ścieżek kanałów + hasła),
- tryb ręczny:
  - wybierasz z jakich kanałów mają być pobrane pliki,
  - możesz dodać dowolną liczbę kanałów,
- tryb automatyczny:
  - bot sam przechodzi po wszystkich kanałach na serwerze,
  - jeśli kanał wymaga hasła, bot pyta o nie w wiadomości,
- pobieranie plików z każdego kanału do osobnego folderu (nazwa kanału),
- informacja:
  - co 10 plików (łącznie),
  - po zakończeniu każdego kanału,
  - po zakończeniu wszystkich kanałów,
- komenda `pobierz pliki`:
  - jeśli napiszesz ją **prywatnie** do bota, bot odpowiada prywatnie,
  - jeśli napiszesz ją na **kanale**, bot odpowiada na kanale.

## Wymagania

- Windows,
- Python 3.8+,
- TeamTalk SDK:
  - katalog `TeamTalk_DLL` z `TeamTalk5.dll`, `TeamTalk5.lib`, `TeamTalk.h`,
  - katalog `TeamTalkPy` z wrapperem `TeamTalk5.py`,
- zależności Pythona:
  - tylko standardowa biblioteka (`ctypes`, `argparse`, `json`, itp.).

## Struktura repozytorium

```text
.
├── TeamTalk_DLL/
│   ├── TeamTalk5.dll
│   ├── TeamTalk5.lib
│   └── TeamTalk.h
├── TeamTalkPy/
│   ├── TeamTalk5.py
│   └── ...
├── tt_downloader_bot.py          # główny kod bota
├── tt-downloader-bot.py          # launcher (python tt-downloader-bot.py)
├── profiles/                     # profile serwera (JSON)
└── channel_profiles/             # profile kanałów (JSON)
```

## Uruchamianie

Standardowo używamy launchera:

```bash
python tt-downloader-bot.py
```

Jeśli uruchomisz **bez parametrów**, bot odpali **interaktywne menu**:

1. Utwórz profil serwera  
2. Wczytaj profil serwera  
3. Usuń profil serwera  
4. Wyjdź  

### Profil serwera

Profil serwera zawiera:

- nazwę profilu (np. `mój_serwer`),
- adres serwera (host/IP),
- port TCP i UDP,
- nazwę użytkownika i hasło,
- nick, którym bot będzie się logował,
- ścieżkę kanału startowego (np. `/Główny`),
- hasło kanału startowego (opcjonalne),
- informację, czy używać szyfrowania (SSL),
- katalog wyjściowy, gdzie zapisywać pliki.

Profile zapisywane są w katalogu `profiles/` jako pliki `.json`.

### Profil kanałów

Po wczytaniu profilu serwera otrzymasz drugie menu:

1. Użyj/utwórz profil kanałów do pobierania  
2. Wpisz kanały ręcznie (bez zapisywania)  
3. Automatyczne pobieranie ze wszystkich kanałów (hasła w PW)  
4. Usuń profil kanałów  
5. Uruchom bota z aktualnym wyborem  
6. Wróć do menu profili serwera  

#### Tryb „profil kanałów”

- Jeśli profil kanałów dla tego serwera istnieje, zobaczysz listę kanałów.
- Możesz potwierdzić użycie istniejącego profilu lub zbudować nowy.
- Kanał opisujesz:
  - ścieżka kanału (np. `/Główny/Pliki`),
  - hasło kanału (jeśli jest wymagane).

Profil jest zapisywany w `channel_profiles/<nazwa_profilu_serwera>.json`.

#### Tryb „ręczny”

Jak wyżej, ale lista kanałów jest używana tylko w tej sesji – nie zapisuje się na dysku.

#### Tryb „auto_all”

- Bot pobiera listę wszystkich kanałów z serwera.
- Dla każdego kanału:
  - jeśli nie wymaga hasła – od razu próbuje pobrać pliki,
  - jeśli wymaga hasła – pyta o nie wiadomością (PW lub kanałową – w zależności skąd przyszło `pobierz pliki`).
- Odpowiedź:
  - samo hasło – bot zapisuje i używa,
  - `pomiń` / `skip` / ENTER – kanał jest pomijany.

## Komenda „pobierz pliki”

Po uruchomieniu bota i zalogowaniu na serwer:

1. Bot dołącza do kanału startowego z profilu.
2. Czeka biernie na komendę.

Komenda ma postać **dokładnie**:

```text
pobierz pliki
```

Możesz ją wysłać:

- **prywatnie** do bota – bot odpowiada na PW,
- na **kanale**.

Po komendzie:

- bot przygotuje kolejkę kanałów (wg wybranego trybu),
- dla każdego kanału:
  - wypisze, że zaczyna pobierać z danego kanału,
  - jeśli kanał nie ma plików – poinformuje i przejdzie dalej,
  - jeśli są pliki – pobierze je do folderu o nazwie kanału (w katalogu wyjściowym),
  - po **zakończeniu kanału** wyśle informację:
    - na PW lub na kanale – zależnie od tego, skąd wywołano komendę,
- co 10 pobranych plików globalnie bot wysyła informację:
  - `Pobrano łącznie X plików...`,
- po zakończeniu wszystkich kanałów:
  - `Skończyłem pobieranie ze wszystkich kanałów. Łącznie pobrano X plików.`

## Tryb CLI (bez profili)

Możesz też uruchomić bota z parametrami, np.:

```bash
python tt-downloader-bot.py \
  --host 127.0.0.1 \
  --tcp-port 10333 \
  --udp-port 10333 \
  --username moj_login \
  --password moje_haslo \
  --nickname "TT Downloader Bot" \
  --channel-path "/Główny/Pliki" \
  --output-dir "./pliki"
```

W tym trybie bot działa w trybie `single` – pobiera tylko z kanału startowego po komendzie `pobierz pliki`.

## Uwaga

- Bot wymaga uprawnień do pobierania plików na serwerze TeamTalk (USERRIGHT_DOWNLOAD_FILES).
- Bot nie usuwa plików z serwera – tylko je pobiera.
- Katalogi `profiles/` i `channel_profiles/` są tworzone automatycznie.

