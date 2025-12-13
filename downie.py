import argparse
import getpass
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

PROFILE_DIR = os.path.join(SCRIPT_DIR, "profiles")
CHANNEL_PROFILE_DIR = os.path.join(SCRIPT_DIR, "channel_profiles")

from TeamTalkPy.TeamTalk5 import (  # type: ignore
    TeamTalk,
    Channel,
    RemoteFile,
    FileTransfer,
    FileTransferStatus,
    TextMessage,
    TextMsgType,
    buildTextMessage,
)


def sanitize_for_fs(name: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join(c for c in name if c not in invalid)
    cleaned = cleaned.strip().rstrip(". ")
    return cleaned or "kanał"


class TTDownloaderBot(TeamTalk):
    def __init__(
        self,
        host: str,
        tcp_port: int,
        udp_port: int,
        username: str,
        password: str,
        nickname: str,
        channel_path: str,
        channel_password: str,
        encrypted: bool,
        output_dir: str,
        channel_mode: str = "single",
        channels_to_download: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        super().__init__()
        self.host = host
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.username = username
        self.password = password
        self.nickname = nickname
        self.channel_path = channel_path
        self.channel_password = channel_password
        self.encrypted = encrypted
        self.output_dir = os.path.abspath(output_dir)
        self.channel_mode = channel_mode
        self.channels_to_download: List[Dict[str, str]] = channels_to_download or []

        self.running = True
        self.logged_in = False
        self.channel_id: Optional[int] = None
        self.join_cmd_id: Optional[int] = None

        self._expected_downloads: Dict[Tuple[int, str], str] = {}
        self._completed_downloads: set[Tuple[int, str]] = set()
        self._downloads_started = False

        self._download_queue: List[Dict[str, object]] = []
        self._current_channel_id: Optional[int] = None
        self._channel_paths: Dict[int, str] = {}
        self._channel_pending_counts: Dict[int, int] = {}
        self._channel_completed_counts: Dict[int, int] = {}
        self._total_completed_files: int = 0
        self._notify_every: int = 10

        self._request_user_id: Optional[int] = None
        self._request_channel_id: Optional[int] = None
        self._request_origin: Optional[str] = None  # "private" lub "channel"

        self._awaiting_password_channel_id: Optional[int] = None

    # --- główny loop ---

    def start(self) -> int:
        print(f"Łączenie z serwerem {self.host}:{self.tcp_port} (encrypted={self.encrypted})...")
        ok = self.connect(
            self.host,
            self.tcp_port,
            self.udp_port,
            0,
            0,
            self.encrypted,
        )
        if not ok:
            print("Nie udało się zainicjować połączenia (Connect zwrócił False).")
            return 1

        try:
            while self.running:
                self.runEventLoop(500)
        except KeyboardInterrupt:
            print("\nPrzerwano przez użytkownika (Ctrl+C).")
        finally:
            self.disconnect()
        return 0

    # --- callbacki TeamTalk ---

    def onConnectSuccess(self) -> None:
        print("Połączono z serwerem, logowanie...")
        cmd_id = self.doLogin(
            self.nickname,
            self.username,
            self.password,
            "TTDownloaderBot",
        )
        if cmd_id <= 0:
            print("Nie udało się zainicjować logowania (doLogin zwrócił <= 0).")
            self.running = False

    def onConnectFailed(self) -> None:
        print("Połączenie z serwerem nieudane.")
        self.running = False

    def onConnectionLost(self) -> None:
        print("Utracono połączenie z serwerem.")
        self.running = False

    def onCmdError(self, cmdId: int, errmsg) -> None:
        try:
            msg = errmsg.szErrorMsg
        except Exception:
            msg = ""
        print(f"Błąd komendy (id={cmdId}): {msg}")
        if cmdId == self.join_cmd_id:
            print("Nie udało się dołączyć do kanału startowego.")
            self.running = False

    def onCmdMyselfLoggedIn(self, userid: int, useraccount) -> None:
        self.logged_in = True
        print("Zalogowano na serwer. Dołączanie do kanału startowego...")
        self._join_target_channel()

    def onCmdMyselfLoggedOut(self) -> None:
        print("Wylogowano z serwera.")
        self.running = False

    def onCmdSuccess(self, cmdId: int) -> None:
        if cmdId == self.join_cmd_id:
            print(
                "Dołączono do kanału startowego. "
                "Czekam na wiadomość 'pobierz pliki' (prywatna lub kanałowa)."
            )

    def onCmdUserTextMessage(self, textmessage: TextMessage) -> None:
        print(
            f"Odebrano wiadomość tekstową: typ={textmessage.nMsgType}, "
            f"from={textmessage.nFromUserID}, to={textmessage.nToUserID}, "
            f"kanał={textmessage.nChannelID}, treść='{textmessage.szMessage}'"
        )

        content_raw = textmessage.szMessage or ""
        content = content_raw.strip().lower()
        from_user_id = textmessage.nFromUserID

        try:
            my_user_id = self.getMyUserID()
        except Exception:
            my_user_id = 0

        if from_user_id == my_user_id:
            return

        origin_private = (
            textmessage.nMsgType == TextMsgType.MSGTYPE_USER
            and textmessage.nToUserID == my_user_id
        )
        origin_channel = (
            textmessage.nMsgType == TextMsgType.MSGTYPE_CHANNEL
            and textmessage.nChannelID != 0
        )

        # oczekiwanie na hasło (auto_all)
        if self._awaiting_password_channel_id is not None:
            chan_id = self._awaiting_password_channel_id
            chan_path = self._channel_paths.get(chan_id, f"channel_{chan_id}")

            if self._request_origin == "private":
                if not origin_private or from_user_id != self._request_user_id:
                    return
            elif self._request_origin == "channel":
                if not origin_channel or textmessage.nChannelID != self._request_channel_id:
                    return

            if content in ("pomin", "pomiń", "pomiń kanał", "skip", ""):
                self._send_to_request_target(f"Pomijam kanał '{chan_path}' (bez hasła).")
                self._awaiting_password_channel_id = None
                self._start_next_channel_download()
                return

            for task in self._download_queue:
                if int(task.get("id", -1)) == chan_id:
                    task["password"] = content_raw.strip()
            self._send_to_request_target(f"Hasło do kanału '{chan_path}' zapisane.")
            self._awaiting_password_channel_id = None
            self._start_downloads_for_channel(chan_id, chan_path)
            return

        if content != "pobierz pliki":
            return

        if origin_private:
            self._request_origin = "private"
            self._request_user_id = from_user_id
            self._request_channel_id = None
        elif origin_channel:
            self._request_origin = "channel"
            self._request_user_id = None
            self._request_channel_id = textmessage.nChannelID
        else:
            return

        if self._downloads_started or self._download_queue:
            self._send_to_request_target("Pobieranie plików już trwa lub jest w toku.")
            return

        if self.channel_id is None:
            self._send_to_request_target("Nie jestem jeszcze w kanale. Spróbuj ponownie za chwilę.")
            return

        self._expected_downloads.clear()
        self._completed_downloads.clear()
        self._channel_paths.clear()
        self._channel_pending_counts.clear()
        self._channel_completed_counts.clear()
        self._total_completed_files = 0
        self._download_queue = []

        if self.channel_mode in ("manual_list", "single"):
            self._prepare_manual_queue()
        elif self.channel_mode == "auto_all":
            self._prepare_auto_queue()
        else:
            self._prepare_manual_queue()

        if not self._download_queue:
            self._send_to_request_target("Brak skonfigurowanych kanałów do pobrania.")
            return

        self._downloads_started = True
        first = self._download_queue[0]
        self._send_to_request_target(
            f"Zaczynam pobieranie z {len(self._download_queue)} kanału/kanałów. "
            f"Najpierw zajmę się kanałem '{first.get('path')}'."
        )
        self._start_next_channel_download()

    def onFileTransfer(self, ft: FileTransfer) -> None:
        key = (ft.nChannelID, ft.szRemoteFileName)
        if key not in self._expected_downloads:
            return

        filename = ft.szRemoteFileName
        local_path = self._expected_downloads[key]
        chan_id = ft.nChannelID

        status = ft.nStatus
        if status == FileTransferStatus.FILETRANSFER_ACTIVE:
            print(
                f"Pobieranie {filename}: {ft.nTransferred}/{ft.nFileSize} bajtów",
                end="\r",
                flush=True,
            )
            return

        if key in self._completed_downloads:
            return

        if status == FileTransferStatus.FILETRANSFER_FINISHED:
            print(f"\nPobieranie zakończone: {filename} -> {local_path}")
        elif status == FileTransferStatus.FILETRANSFER_ERROR:
            print(f"\nBłąd podczas pobierania pliku: {filename}")
        elif status == FileTransferStatus.FILETRANSFER_CLOSED:
            print(f"\nTransfer zamknięty: {filename}")

        self._completed_downloads.add(key)
        self._total_completed_files += 1
        self._channel_completed_counts[chan_id] = self._channel_completed_counts.get(chan_id, 0) + 1

        if self._total_completed_files % self._notify_every == 0:
            self._send_to_request_target(f"Pobrano łącznie {self._total_completed_files} plików...")

        pending = self._channel_pending_counts.get(chan_id)
        completed = self._channel_completed_counts.get(chan_id)
        if pending is not None and completed is not None and completed >= pending:
            chan_path = self._channel_paths.get(chan_id, f"channel_{chan_id}")
            self._send_to_request_target(
                f"Zakończyłem pobieranie plików z kanału '{chan_path}'."
            )
            if self._current_channel_id == chan_id:
                self._start_next_channel_download()

    # --- logika pomocnicza ---

    def _join_target_channel(self) -> None:
        if not self.channel_path:
            print("Nie podano ścieżki kanału startowego.")
            self.running = False
            return

        chan_id = self.getChannelIDFromPath(self.channel_path)
        if chan_id <= 0:
            print(f"Nie znaleziono kanału o ścieżce: {self.channel_path}")
            self.running = False
            return

        self.channel_id = chan_id
        self.join_cmd_id = self.doJoinChannelByID(self.channel_id, self.channel_password)
        if self.join_cmd_id <= 0:
            print("Nie udało się zainicjować dołączania do kanału startowego.")
            self.running = False

    def _prepare_manual_queue(self) -> None:
        self._download_queue = []

        if self.channel_mode == "single":
            chan_id = self.channel_id or self.getChannelIDFromPath(self.channel_path)
            if not chan_id or chan_id <= 0:
                return
            path = self.channel_path or self._safe_channel_path(chan_id)
            self._channel_paths[chan_id] = path
            self._download_queue.append({"id": chan_id, "path": path, "password": self.channel_password})
            return

        for ch in self.channels_to_download:
            path = ch.get("path", "").strip()
            if not path:
                continue
            chan_id = self.getChannelIDFromPath(path)
            if chan_id <= 0:
                msg = f"Nie znaleziono kanału o ścieżce: {path}"
                print(msg)
                self._send_to_request_target(msg)
                continue
            self._channel_paths[chan_id] = path
            self._download_queue.append(
                {"id": chan_id, "path": path, "password": ch.get("password", "")}
            )

    def _prepare_auto_queue(self) -> None:
        self._download_queue = []
        channels = self.getServerChannels()
        for ch in channels:
            chan_id = ch.nChannelID
            if chan_id <= 0:
                continue
            path = self.getChannelPath(chan_id) or ch.szName or f"channel_{chan_id}"
            self._channel_paths[chan_id] = path
            requires_password = bool(getattr(ch, "bPassword", False))
            self._download_queue.append(
                {"id": chan_id, "path": path, "requires_password": requires_password}
            )

    def _safe_channel_path(self, chan_id: int) -> str:
        try:
            path = self.getChannelPath(chan_id)
        except Exception:
            path = ""
        return path or f"channel_{chan_id}"

    def _start_next_channel_download(self) -> None:
        if not self._download_queue:
            if self._downloads_started:
                self._send_to_request_target(
                    f"Skończyłem pobieranie ze wszystkich kanałów. "
                    f"Łącznie pobrano {self._total_completed_files} plików."
                )
            self._downloads_started = False
            self._current_channel_id = None
            self._awaiting_password_channel_id = None
            return

        task = self._download_queue.pop(0)
        chan_id = int(task["id"])
        path = str(task.get("path", f"channel_{chan_id}"))
        self._current_channel_id = chan_id

        if self.channel_mode == "auto_all" and task.get("requires_password") and not task.get("password"):
            self._send_to_request_target(
                f"Kanał '{path}' prawdopodobnie ma hasło. "
                "Odpowiedz w wiadomości podając samo hasło "
                "albo 'pomiń', aby pominąć ten kanał."
            )
            self._awaiting_password_channel_id = chan_id
            return

        self._send_to_request_target(f"Pobieram pliki z kanału '{path}'.")
        self._start_downloads_for_channel(chan_id, path)

    def _start_downloads_for_channel(self, channel_id: int, channel_path: str) -> None:
        folder_name = sanitize_for_fs(channel_path or f"channel_{channel_id}")
        target_dir = os.path.join(self.output_dir, folder_name)
        os.makedirs(target_dir, exist_ok=True)

        files = self.getChannelFiles(channel_id)
        if not files:
            print(f"Kanał '{channel_path}' nie zawiera plików.")
            self._send_to_request_target(
                f"Kanał '{channel_path}' nie zawiera plików. Przechodzę do kolejnego."
            )
            self._channel_pending_counts[channel_id] = 0
            self._channel_completed_counts[channel_id] = 0
            self._start_next_channel_download()
            return

        print(f"Znaleziono {len(files)} plików w kanale '{channel_path}'.")
        started = 0
        for rf in files:
            filename = rf.szFileName or f"file_{rf.nFileID}"
            local_path = os.path.join(target_dir, filename)
            print(f"Rozpoczynam pobieranie: {filename} -> {local_path}")
            key = (rf.nChannelID, filename)
            self._expected_downloads[key] = local_path
            transfer_id = self.doRecvFile(rf.nChannelID, rf.nFileID, local_path)
            if transfer_id > 0:
                started += 1
            else:
                print(f"  Nie udało się rozpocząć pobierania pliku: {filename}")
                self._completed_downloads.add(key)

        self._channel_pending_counts[channel_id] = started
        self._channel_completed_counts[channel_id] = 0

        if started == 0:
            self._send_to_request_target(
                f"Nie udało się uruchomić transferów dla kanału '{channel_path}'. Przechodzę dalej."
            )
            self._start_next_channel_download()

    def _send_private_message(self, to_user_id: int, text: str) -> None:
        try:
            from_user_id = self.getMyUserID()
        except Exception:
            from_user_id = 0

        msgs = buildTextMessage(
            content=text,
            nMsgType=TextMsgType.MSGTYPE_USER,
            nToUserID=to_user_id,
            nChannelID=0,
            nFromUserID=from_user_id,
            szFromUsername=self.nickname,
        )
        for msg in msgs:
            self.doTextMessage(msg)

    def _send_channel_message(self, channel_id: int, text: str) -> None:
        try:
            from_user_id = self.getMyUserID()
        except Exception:
            from_user_id = 0

        msgs = buildTextMessage(
            content=text,
            nMsgType=TextMsgType.MSGTYPE_CHANNEL,
            nToUserID=0,
            nChannelID=channel_id,
            nFromUserID=from_user_id,
            szFromUsername=self.nickname,
        )
        for msg in msgs:
            self.doTextMessage(msg)

    def _send_to_request_target(self, text: str) -> None:
        if self._request_origin == "private" and self._request_user_id is not None:
            self._send_private_message(self._request_user_id, text)
        elif self._request_origin == "channel" and self._request_channel_id is not None:
            self._send_channel_message(self._request_channel_id, text)


# --- obsługa profili serwera i kanałów ---


def _ensure_dirs() -> None:
    os.makedirs(PROFILE_DIR, exist_ok=True)
    os.makedirs(CHANNEL_PROFILE_DIR, exist_ok=True)


def _profile_path(name: str) -> str:
    safe = sanitize_for_fs(name.replace(" ", "_"))
    return os.path.join(PROFILE_DIR, safe + ".json")


def _channel_profile_path(profile_name: str) -> str:
    safe = sanitize_for_fs(profile_name.replace(" ", "_"))
    return os.path.join(CHANNEL_PROFILE_DIR, safe + ".json")


def _list_server_profiles() -> List[str]:
    if not os.path.isdir(PROFILE_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(PROFILE_DIR)
        if f.lower().endswith(".json")
    )


def _load_server_profile(name: str) -> Optional[Dict[str, object]]:
    path = _profile_path(name)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        profile = json.load(f)
    profile.setdefault("name", name)
    return profile


def _save_server_profile(name: str, profile: Dict[str, object]) -> None:
    _ensure_dirs()
    path = _profile_path(name)
    profile = dict(profile)
    profile["name"] = name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)


def _delete_server_profile(name: str) -> None:
    path = _profile_path(name)
    if os.path.isfile(path):
        os.remove(path)


def _load_channel_profile(profile_name: str) -> List[Dict[str, str]]:
    path = _channel_profile_path(profile_name)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("channels", []))


def _save_channel_profile(profile_name: str, channels: List[Dict[str, str]]) -> None:
    _ensure_dirs()
    path = _channel_profile_path(profile_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"channels": channels}, f, indent=2, ensure_ascii=False)


def _delete_channel_profile(profile_name: str) -> None:
    path = _channel_profile_path(profile_name)
    if os.path.isfile(path):
        os.remove(path)


def _prompt_int(prompt: str, default: int) -> int:
    txt = input(f"{prompt} [{default}]: ").strip()
    if not txt:
        return default
    try:
        return int(txt)
    except ValueError:
        print("Nieprawidłowa liczba, używam wartości domyślnej.")
        return default


def _prompt_channels_interactive() -> List[Dict[str, str]]:
    channels: List[Dict[str, str]] = []
    while True:
        print("\n1. Dodaj kanał do pobrania")
        print("2. Przejdź dalej")
        choice = input("Wybierz opcję [1/2]: ").strip() or "2"
        if choice == "1":
            path = input("Ścieżka kanału (np. /Główny/Pliki): ").strip()
            if not path:
                print("Ścieżka kanału nie może być pusta.")
                continue
            password = getpass.getpass("Hasło kanału (ENTER jeśli brak): ")
            channels.append({"path": path, "password": password})
        elif choice == "2":
            break
        else:
            print("Nieprawidłowy wybór.")
    return channels


def create_server_profile_interactive() -> Optional[Dict[str, object]]:
    _ensure_dirs()
    print("=== Tworzenie profilu serwera ===")
    name = input("Nazwa profilu serwera: ").strip()
    if not name:
        print("Nazwa profilu jest wymagana.")
        return None

    host = input("Adres serwera (np. 127.0.0.1): ").strip()
    if not host:
        print("Adres serwera jest wymagany.")
        return None

    tcp_port = _prompt_int("Port TCP", 10333)
    udp_port = _prompt_int("Port UDP", 10333)

    username = input("Nazwa użytkownika: ").strip()
    if not username:
        print("Nazwa użytkownika jest wymagana.")
        return None

    password = getpass.getpass("Hasło użytkownika (ENTER jeśli brak): ")

    nickname = input(f"Nick (domyślnie '{username}'): ").strip() or username

    base_channel_path = input("Ścieżka kanału startowego (np. /Główny): ").strip() or "/"
    base_channel_password = getpass.getpass("Hasło kanału startowego (ENTER jeśli brak): ")

    enc_answer = input("Czy użyć szyfrowania (SSL)? [t/N]: ").strip().lower()
    encrypted = enc_answer in ("t", "tak", "y", "yes", "1")

    output_dir = input("Folder, gdzie zapisywać pliki (domyślnie bieżący): ").strip() or "."

    profile: Dict[str, object] = {
        "name": name,
        "host": host,
        "tcp_port": tcp_port,
        "udp_port": udp_port,
        "username": username,
        "password": password,
        "nickname": nickname,
        "base_channel_path": base_channel_path,
        "base_channel_password": base_channel_password,
        "encrypted": encrypted,
        "output_dir": output_dir,
    }
    _save_server_profile(name, profile)
    print(f"Zapisano profil serwera: {name}")
    return profile


def choose_server_profile_interactive() -> Optional[Dict[str, object]]:
    profiles = _list_server_profiles()
    if not profiles:
        print("Brak zapisanych profili serwera.")
        return None

    print("=== Dostępne profile serwera ===")
    for idx, name in enumerate(profiles, start=1):
        print(f"{idx}. {name}")

    choice_txt = input("Wybierz profil numerem (ENTER aby anulować): ").strip()
    if not choice_txt:
        return None
    try:
        idx = int(choice_txt)
    except ValueError:
        print("Nieprawidłowy numer.")
        return None
    if not (1 <= idx <= len(profiles)):
        print("Nieprawidłowy numer.")
        return None

    name = profiles[idx - 1]
    profile = _load_server_profile(name)
    if not profile:
        print("Nie udało się wczytać profilu.")
        return None
    return profile


def delete_server_profile_interactive() -> None:
    profiles = _list_server_profiles()
    if not profiles:
        print("Brak zapisanych profili serwera.")
        return

    print("=== Usuwanie profilu serwera ===")
    for idx, name in enumerate(profiles, start=1):
        print(f"{idx}. {name}")
    choice_txt = input("Wybierz profil do usunięcia numerem (ENTER aby anulować): ").strip()
    if not choice_txt:
        return
    try:
        idx = int(choice_txt)
    except ValueError:
        print("Nieprawidłowy numer.")
        return
    if not (1 <= idx <= len(profiles)):
        print("Nieprawidłowy numer.")
        return
    name = profiles[idx - 1]
    _delete_server_profile(name)
    _delete_channel_profile(name)
    print(f"Profil '{name}' został usunięty.")


def run_with_profile(profile: Dict[str, object]) -> int:
    profile_name = str(profile.get("name", "default"))
    channels_for_run: List[Dict[str, str]] = []
    selected_mode: Optional[str] = None  # "manual_list", "auto_all", "single"

    while True:
        print("\n=== Konfiguracja kanałów dla profilu ===")
        print("1. Użyj/utwórz profil kanałów do pobierania")
        print("2. Wpisz kanały ręcznie (bez zapisywania)")
        print("3. Automatyczne pobieranie ze wszystkich kanałów (hasła w PW)")
        print("4. Usuń profil kanałów")
        print("5. Uruchom bota z aktualnym wyborem")
        print("6. Wróć do menu profili serwera")
        choice = input("Wybierz opcję [1-6]: ").strip()

        if choice == "1":
            existing = _load_channel_profile(profile_name)
            if existing:
                print("Znaleziono istniejący profil kanałów:")
                for ch in existing:
                    print(f" - {ch.get('path')} (hasło: {'tak' if ch.get('password') else 'nie'})")
                ans = input("Użyć istniejącego profilu? [T/n]: ").strip().lower()
                if ans in ("", "t", "tak", "y", "yes"):
                    channels_for_run = existing
                    selected_mode = "manual_list"
                    print("Używam istniejącego profilu kanałów.")
                    continue
            channels_for_run = _prompt_channels_interactive()
            _save_channel_profile(profile_name, channels_for_run)
            selected_mode = "manual_list"
        elif choice == "2":
            channels_for_run = _prompt_channels_interactive()
            selected_mode = "manual_list"
        elif choice == "3":
            selected_mode = "auto_all"
        elif choice == "4":
            _delete_channel_profile(profile_name)
            print("Profil kanałów został usunięty.")
            if selected_mode == "manual_list":
                channels_for_run = []
                selected_mode = None
        elif choice == "5":
            if selected_mode is None:
                selected_mode = "single"
            break
        elif choice == "6":
            return 0
        else:
            print("Nieprawidłowy wybór.")

    bot = TTDownloaderBot(
        host=str(profile["host"]),
        tcp_port=int(profile["tcp_port"]),
        udp_port=int(profile["udp_port"]),
        username=str(profile["username"]),
        password=str(profile.get("password", "")),
        nickname=str(profile.get("nickname", profile["username"])),
        channel_path=str(profile.get("base_channel_path", "/")),
        channel_password=str(profile.get("base_channel_password", "")),
        encrypted=bool(profile.get("encrypted", False)),
        output_dir=str(profile.get("output_dir", ".")),
        channel_mode=selected_mode,
        channels_to_download=channels_for_run,
    )
    return bot.start()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bot TeamTalk 5 pobierający pliki z kanału do folderu o nazwie kanału.",
    )
    parser.add_argument("--host", required=True, help="Adres serwera TeamTalk.")
    parser.add_argument("--tcp-port", type=int, default=10333, help="Port TCP serwera (domyślnie 10333).")
    parser.add_argument("--udp-port", type=int, default=10333, help="Port UDP serwera (domyślnie 10333).")
    parser.add_argument("--username", required=True, help="Nazwa użytkownika do logowania.")
    parser.add_argument("--password", default="", help="Hasło użytkownika.")
    parser.add_argument(
        "--nickname",
        default="TT Downloader Bot",
        help="Nick widoczny na serwerze (domyślnie 'TT Downloader Bot').",
    )
    parser.add_argument(
        "--channel-path",
        required=True,
        help="Ścieżka kanału startowego, np. '/Główny/Pliki'.",
    )
    parser.add_argument(
        "--channel-password",
        default="",
        help="Hasło kanału startowego (jeśli jest wymagane).",
    )
    parser.add_argument(
        "--encrypted",
        action="store_true",
        help="Użyj połączenia szyfrowanego (SSL), jeśli serwer to wspiera.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Katalog bazowy, w którym zostanie utworzony folder z nazwą kanału.",
    )

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    bot = TTDownloaderBot(
        host=args.host,
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        username=args.username,
        password=args.password,
        nickname=args.nickname,
        channel_path=args.channel_path,
        channel_password=args.channel_password,
        encrypted=args.encrypted,
        output_dir=args.output_dir,
        channel_mode="single",
    )
    return bot.start()


def interactive_setup() -> int:
    _ensure_dirs()
    while True:
        print("\n=== TT Downloader Bot ===")
        print("1. Utwórz profil serwera")
        print("2. Wczytaj profil serwera")
        print("3. Usuń profil serwera")
        print("4. Wyjdź")
        choice = input("Wybierz opcję [1-4]: ").strip()

        if choice == "1":
            profile = create_server_profile_interactive()
            if profile:
                return run_with_profile(profile)
        elif choice == "2":
            profile = choose_server_profile_interactive()
            if profile:
                return run_with_profile(profile)
        elif choice == "3":
            delete_server_profile_interactive()
        elif choice == "4" or choice == "":
            return 0
        else:
            print("Nieprawidłowy wybór.")

