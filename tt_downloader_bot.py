import argparse
import getpass
import json
import os
import sys
from typing import Dict, List, Optional, Set, Tuple

# Ensure we can import the local TeamTalkPy wrapper.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

PROFILE_DIR = os.path.join(SCRIPT_DIR, "profiles")
CHANNEL_PROFILE_DIR = os.path.join(SCRIPT_DIR, "channel_profiles")

try:
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
except Exception as exc:
    raise SystemExit(
        "Failed to import the TeamTalk Python wrapper.\n"
        "Make sure you have the TeamTalk 5 SDK available locally.\n"
        "Expected local files:\n"
        "  - TeamTalkPy/TeamTalk5.py\n"
        "  - TeamTalk_DLL/TeamTalk5.dll\n"
        "See README.md.\n"
        f"Original error: {exc}"
    ) from exc


def sanitize_for_fs(name: str) -> str:
    """Basic filename sanitization for Windows."""
    invalid = '<>:"/\\|?*'
    cleaned = "".join(c for c in name if c not in invalid)
    cleaned = cleaned.strip().rstrip(". ")
    return cleaned or "channel"


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
        channel_mode: str = "single",  # single, manual_list, auto_all
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
        # The "base" channel where the bot stays and listens for the command.
        self.channel_id: Optional[int] = None
        self.join_cmd_id: Optional[int] = None

        # (channel_id, remote_filename) -> local_path
        self._expected_downloads: Dict[Tuple[int, str], str] = {}
        self._completed_downloads: Set[Tuple[int, str]] = set()
        self._downloads_started = False

        # Queue of channels to process.
        self._download_queue: List[Dict[str, object]] = []
        self._current_channel_id: Optional[int] = None
        self._channel_paths: Dict[int, str] = {}
        self._channel_pending_counts: Dict[int, int] = {}
        self._channel_completed_counts: Dict[int, int] = {}
        self._total_completed_files: int = 0
        self._notify_every: int = 10

        # Who requested the download (so we can reply in the same place).
        self._request_user_id: Optional[int] = None
        self._request_channel_id: Optional[int] = None
        self._request_origin: Optional[str] = None  # "private" or "channel"

        # In auto_all mode we may need to ask for a channel password.
        self._awaiting_password_channel_id: Optional[int] = None

    # --- main loop ---

    def start(self) -> int:
        print(f"Connecting to {self.host}:{self.tcp_port} (encrypted={self.encrypted})...")
        ok = self.connect(
            self.host,
            self.tcp_port,
            self.udp_port,
            0,
            0,
            self.encrypted,
        )
        if not ok:
            print("Failed to initialize connection (connect() returned False).")
            return 1

        try:
            while self.running:
                self.runEventLoop(500)
        except KeyboardInterrupt:
            print("\nInterrupted by user (Ctrl+C).")
        finally:
            self.disconnect()
        return 0

    # --- TeamTalk callbacks ---

    def onConnectSuccess(self) -> None:
        print("Connected, logging in...")
        cmd_id = self.doLogin(
            self.nickname,
            self.username,
            self.password,
            "TTDownloaderBot",
        )
        if cmd_id <= 0:
            print("Failed to initialize login (doLogin returned <= 0).")
            self.running = False

    def onConnectFailed(self) -> None:
        print("Connection failed.")
        self.running = False

    def onConnectionLost(self) -> None:
        print("Connection lost.")
        self.running = False

    def onCmdError(self, cmdId: int, errmsg) -> None:
        # errmsg to ClientErrorMsg
        try:
            msg = errmsg.szErrorMsg
        except Exception:
            msg = ""
        print(f"Command error (id={cmdId}): {msg}")
        if cmdId == self.join_cmd_id:
            print("Failed to join the base channel.")
            self.running = False

    def onCmdMyselfLoggedIn(self, userid: int, useraccount) -> None:
        self.logged_in = True
        print("Logged in. Joining base channel...")
        self._join_target_channel()

    def onCmdMyselfLoggedOut(self) -> None:
        print("Logged out.")
        self.running = False

    def onCmdSuccess(self, cmdId: int) -> None:
        if cmdId == self.join_cmd_id:
            print("Joined base channel. Waiting for the message 'download files' (private or channel).")

    def onCmdUserTextMessage(self, textmessage: TextMessage) -> None:
        print(
            f"Received text message: type={textmessage.nMsgType}, "
            f"from={textmessage.nFromUserID}, to={textmessage.nToUserID}, "
            f"channel={textmessage.nChannelID}, content='{textmessage.szMessage}'"
        )

        content_raw = textmessage.szMessage or ""
        content = content_raw.strip().lower()
        from_user_id = textmessage.nFromUserID

        try:
            my_user_id = self.getMyUserID()
        except Exception:
            my_user_id = 0

        # Ignore our own messages.
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

        # Waiting for a channel password (auto_all).
        if self._awaiting_password_channel_id is not None:
            chan_id = self._awaiting_password_channel_id
            chan_path = self._channel_paths.get(chan_id, f"channel_{chan_id}")

            # Password must come from the same place as the original request.
            if self._request_origin == "private":
                if not origin_private or from_user_id != self._request_user_id:
                    return
            elif self._request_origin == "channel":
                if not origin_channel or textmessage.nChannelID != self._request_channel_id:
                    return

            if content in ("skip", "next", ""):
                self._send_to_request_target(f"Skipping channel '{chan_path}' (no password provided).")
                self._awaiting_password_channel_id = None
                self._start_next_channel_download()
                return

            # Store the password in the queue entry and continue.
            for task in self._download_queue:
                if int(task.get("id", -1)) == chan_id:
                    task["password"] = content_raw.strip()
            self._send_to_request_target(f"Password saved for channel '{chan_path}'.")
            self._awaiting_password_channel_id = None
            self._start_downloads_for_channel(chan_id, chan_path)
            return

        # Start command.
        if content != "download files":
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
            # Ignore other message types.
            return

        if self._downloads_started or self._download_queue:
            self._send_to_request_target("Downloads are already running.")
            return

        if self.channel_id is None:
            self._send_to_request_target("I'm not in a channel yet. Please try again in a moment.")
            return

        # Build the download queue.
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
            self._send_to_request_target("No channels configured for download.")
            return

        self._downloads_started = True
        first = self._download_queue[0]
        self._send_to_request_target(
            f"Starting downloads from {len(self._download_queue)} channel(s). "
            f"First up: '{first.get('path')}'."
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
                f"Downloading {filename}: {ft.nTransferred}/{ft.nFileSize} bytes",
                end="\r",
                flush=True,
            )
            return
        # Final statuses.
        if key in self._completed_downloads:
            return

        if status == FileTransferStatus.FILETRANSFER_FINISHED:
            print(f"\nDownload finished: {filename} -> {local_path}")
        elif status == FileTransferStatus.FILETRANSFER_ERROR:
            print(f"\nError while downloading file: {filename}")
        elif status == FileTransferStatus.FILETRANSFER_CLOSED:
            print(f"\nTransfer closed: {filename}")

        self._completed_downloads.add(key)
        self._total_completed_files += 1
        self._channel_completed_counts[chan_id] = self._channel_completed_counts.get(chan_id, 0) + 1

        # Notify every N files.
        if self._total_completed_files % self._notify_every == 0:
            self._send_to_request_target(
                f"Downloaded a total of {self._total_completed_files} files..."
            )

        pending = self._channel_pending_counts.get(chan_id)
        completed = self._channel_completed_counts.get(chan_id)
        if pending is not None and completed is not None and completed >= pending:
            chan_path = self._channel_paths.get(chan_id, f"channel_{chan_id}")
            self._send_to_request_target(
                f"Finished downloading files from channel '{chan_path}'."
            )
            if self._current_channel_id == chan_id:
                self._start_next_channel_download()

    # --- logika pomocnicza ---

    def _join_target_channel(self) -> None:
        if not self.channel_path:
            print("No base channel path provided.")
            self.running = False
            return

        chan_id = self.getChannelIDFromPath(self.channel_path)
        if chan_id <= 0:
            print(f"Channel not found for path: {self.channel_path}")
            self.running = False
            return

        self.channel_id = chan_id
        self.join_cmd_id = self.doJoinChannelByID(self.channel_id, self.channel_password)
        if self.join_cmd_id <= 0:
            print("Failed to start joining the base channel.")
            self.running = False

    def _prepare_manual_queue(self) -> None:
        """Build a channel queue for single/manual_list modes."""
        self._download_queue = []

        if self.channel_mode == "single":
            # Base channel only.
            chan_id = self.channel_id or self.getChannelIDFromPath(self.channel_path)
            if not chan_id or chan_id <= 0:
                return
            path = self.channel_path or self._safe_channel_path(chan_id)
            self._channel_paths[chan_id] = path
            self._download_queue.append({"id": chan_id, "path": path, "password": self.channel_password})
            return

        # manual_list: channels configured in the profile.
        for ch in self.channels_to_download:
            path = ch.get("path", "").strip()
            if not path:
                continue
            chan_id = self.getChannelIDFromPath(path)
            if chan_id <= 0:
                msg = f"Channel not found for path: {path}"
                print(msg)
                self._send_to_request_target(msg)
                continue
            self._channel_paths[chan_id] = path
            self._download_queue.append(
                {"id": chan_id, "path": path, "password": ch.get("password", "")}
            )

    def _prepare_auto_queue(self) -> None:
        """Build a channel queue for auto_all mode (all server channels)."""
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
                    f"Finished downloading from all channels. "
                    f"Total files downloaded: {self._total_completed_files}."
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
                f"Channel '{path}' likely requires a password. "
                "Reply with the password only, "
                "or type 'skip' to skip this channel."
            )
            self._awaiting_password_channel_id = chan_id
            return

        self._send_to_request_target(f"Downloading files from channel '{path}'.")
        self._start_downloads_for_channel(chan_id, path)

    def _start_downloads_for_channel(self, channel_id: int, channel_path: str) -> None:
        folder_name = sanitize_for_fs(channel_path or f"channel_{channel_id}")
        target_dir = os.path.join(self.output_dir, folder_name)
        os.makedirs(target_dir, exist_ok=True)

        files = self.getChannelFiles(channel_id)
        if not files:
            print(f"Channel '{channel_path}' contains no files.")
            self._send_to_request_target(
                f"Channel '{channel_path}' contains no files. Moving on."
            )
            self._channel_pending_counts[channel_id] = 0
            self._channel_completed_counts[channel_id] = 0
            self._start_next_channel_download()
            return

        print(f"Found {len(files)} file(s) in channel '{channel_path}'.")
        started = 0
        for rf in files:
            filename = rf.szFileName or f"file_{rf.nFileID}"
            local_path = os.path.join(target_dir, filename)
            print(f"Starting download: {filename} -> {local_path}")
            key = (rf.nChannelID, filename)
            self._expected_downloads[key] = local_path
            transfer_id = self.doRecvFile(rf.nChannelID, rf.nFileID, local_path)
            if transfer_id > 0:
                started += 1
            else:
                print(f"  Failed to start download for file: {filename}")
                # Treat as completed with error.
                self._completed_downloads.add(key)

        self._channel_pending_counts[channel_id] = started
        self._channel_completed_counts[channel_id] = 0

        if started == 0:
            self._send_to_request_target(
                f"Failed to start transfers for channel '{channel_path}'. Moving on."
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


# --- server/channel profile helpers ---


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
        print("Invalid number, using the default value.")
        return default


def _prompt_channels_interactive() -> List[Dict[str, str]]:
    channels: List[Dict[str, str]] = []
    while True:
        print("\n1. Add a channel to download")
        print("2. Continue")
        choice = input("Choose an option [1/2]: ").strip() or "2"
        if choice == "1":
            path = input("Channel path (e.g. /Root/Files): ").strip()
            if not path:
                print("Channel path cannot be empty.")
                continue
            password = getpass.getpass("Channel password (press ENTER if none): ")
            channels.append({"path": path, "password": password})
        elif choice == "2":
            break
        else:
            print("Invalid choice.")
    return channels


def create_server_profile_interactive() -> Optional[Dict[str, object]]:
    _ensure_dirs()
    print("=== Create server profile ===")
    name = input("Server profile name: ").strip()
    if not name:
        print("Profile name is required.")
        return None

    host = input("Server address (e.g. 127.0.0.1): ").strip()
    if not host:
        print("Server address is required.")
        return None

    tcp_port = _prompt_int("TCP port", 10333)
    udp_port = _prompt_int("UDP port", 10333)

    username = input("Username: ").strip()
    if not username:
        print("Username is required.")
        return None

    password = getpass.getpass("User password (press ENTER if none): ")

    nickname = input(f"Nickname (default '{username}'): ").strip() or username

    base_channel_path = input("Base channel path (e.g. /Root): ").strip() or "/"
    base_channel_password = getpass.getpass("Base channel password (press ENTER if none): ")

    enc_answer = input("Use encryption (SSL)? [y/N]: ").strip().lower()
    encrypted = enc_answer in ("y", "yes", "1", "true")

    output_dir = input("Output folder (default: current directory): ").strip() or "."

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
    print(f"Saved server profile: {name}")
    return profile


def choose_server_profile_interactive() -> Optional[Dict[str, object]]:
    profiles = _list_server_profiles()
    if not profiles:
        print("No saved server profiles.")
        return None

    print("=== Available server profiles ===")
    for idx, name in enumerate(profiles, start=1):
        print(f"{idx}. {name}")

    choice_txt = input("Select profile by number (press ENTER to cancel): ").strip()
    if not choice_txt:
        return None
    try:
        idx = int(choice_txt)
    except ValueError:
        print("Invalid number.")
        return None
    if not (1 <= idx <= len(profiles)):
        print("Invalid number.")
        return None

    name = profiles[idx - 1]
    profile = _load_server_profile(name)
    if not profile:
        print("Failed to load profile.")
        return None
    return profile


def delete_server_profile_interactive() -> None:
    profiles = _list_server_profiles()
    if not profiles:
        print("No saved server profiles.")
        return

    print("=== Delete server profile ===")
    for idx, name in enumerate(profiles, start=1):
        print(f"{idx}. {name}")
    choice_txt = input("Select profile to delete by number (press ENTER to cancel): ").strip()
    if not choice_txt:
        return
    try:
        idx = int(choice_txt)
    except ValueError:
        print("Invalid number.")
        return
    if not (1 <= idx <= len(profiles)):
        print("Invalid number.")
        return
    name = profiles[idx - 1]
    _delete_server_profile(name)
    _delete_channel_profile(name)
    print(f"Profile '{name}' has been deleted.")


def run_with_profile(profile: Dict[str, object]) -> int:
    profile_name = str(profile.get("name", "default"))
    channels_for_run: List[Dict[str, str]] = []
    selected_mode: Optional[str] = None  # "manual_list", "auto_all", "single"

    while True:
        print("\n=== Channel selection for this profile ===")
        print("1. Use/create a saved channel profile")
        print("2. Enter channels manually (do not save)")
        print("3. Auto-download from all channels (password prompts via chat)")
        print("4. Delete channel profile")
        print("5. Start bot with current selection")
        print("6. Back to server profile menu")
        choice = input("Choose an option [1-6]: ").strip()

        if choice == "1":
            existing = _load_channel_profile(profile_name)
            if existing:
                print("Found an existing channel profile:")
                for ch in existing:
                    print(f" - {ch.get('path')} (password: {'yes' if ch.get('password') else 'no'})")
                ans = input("Use existing profile? [Y/n]: ").strip().lower()
                if ans in ("", "y", "yes"):
                    channels_for_run = existing
                    selected_mode = "manual_list"
                    print("Using existing channel profile.")
                    continue
            # Create a new profile.
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
            print("Channel profile deleted.")
            if selected_mode == "manual_list":
                channels_for_run = []
                selected_mode = None
        elif choice == "5":
            if selected_mode is None:
                # Default to base channel only.
                selected_mode = "single"
            break
        elif choice == "6":
            return 0
        else:
            print("Invalid choice.")

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
        description="TeamTalk 5 bot that downloads channel files into a folder named after the channel.",
    )
    parser.add_argument("--host", required=True, help="TeamTalk server address.")
    parser.add_argument("--tcp-port", type=int, default=10333, help="Server TCP port (default: 10333).")
    parser.add_argument("--udp-port", type=int, default=10333, help="Server UDP port (default: 10333).")
    parser.add_argument("--username", required=True, help="Login username.")
    parser.add_argument("--password", default="", help="Login password.")
    parser.add_argument(
        "--nickname",
        default="TT Downloader Bot",
        help="Nickname visible on the server (default: 'TT Downloader Bot').",
    )
    parser.add_argument(
        "--channel-path",
        required=True,
        help="Base channel path, e.g. '/Root/Files'.",
    )
    parser.add_argument(
        "--channel-password",
        default="",
        help="Base channel password (if required).",
    )
    parser.add_argument(
        "--encrypted",
        action="store_true",
        help="Use encrypted connection (SSL), if the server supports it.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Base output directory where per-channel folders will be created.",
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
        print("1. Create server profile")
        print("2. Load server profile")
        print("3. Delete server profile")
        print("4. Exit")
        choice = input("Choose an option [1-4]: ").strip()

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
            print("Invalid choice.")


if __name__ == "__main__":
    # Without arguments: interactive mode.
    if len(sys.argv) == 1:
        raise SystemExit(interactive_setup())

    # With arguments: CLI mode.
    raise SystemExit(main(sys.argv[1:]))
