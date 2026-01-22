# TT Downloader Bot

![CI](https://github.com/krzyywyy/teamtalk-files-downloader/actions/workflows/ci.yml/badge.svg)

A TeamTalk 5 bot that downloads all files from one or more server channels when
you send the command `download files`. Downloaded files are saved to disk in
folders named after the channels.

## Features

- Connects to a TeamTalk 5 server and joins a configured "base" channel
- Supports multiple server profiles (host, ports, credentials, SSL, output dir)
- Supports multiple channel-selection modes:
  - Single channel (base channel only)
  - Manual list of channels (saved as a channel profile)
  - Automatic crawl of all channels on the server (asks for passwords when needed)
- Saves each channel's files into its own folder
- Progress notifications every 10 files (configurable in code)

## Requirements

- Windows
- Python 3.8+
- No external Python dependencies (standard library only)
- TeamTalk 5 server credentials
- TeamTalk user permission to download files (`USERRIGHT_DOWNLOAD_FILES`)
- TeamTalk 5 SDK DLL (`TeamTalk5.dll`) available locally (not included)

## Repository Layout

```text
.
|-- TeamTalk_DLL/
|   `-- README.md                  # where to put TeamTalk5.dll (not committed)
|-- TeamTalkPy/                    # Python wrapper for TeamTalk (from TeamTalk SDK)
|-- tt_downloader_bot.py           # bot implementation + interactive setup
`-- launcher.py                    # entrypoint (recommended)
```

## Setup

1. Clone the repository.
2. Install Python (3.8+).
3. Verify Python is available:

   ```bash
   python --version
   ```

3. Obtain the TeamTalk 5 SDK from BearWare.dk and copy at least:
   - `TeamTalk5.dll` into `TeamTalk_DLL/`

## DLL Placement

On Windows, the TeamTalk Python wrapper loads `TeamTalk5.dll` from:

- the current working directory, and
- `TeamTalk_DLL/` (relative to this repository)

Make sure the DLL architecture matches your Python interpreter (x64 vs x86).

## Run (Interactive Mode - Recommended)

```bash
python launcher.py
```

If you start without arguments, the bot launches an interactive menu:

1. Create server profile
2. Load server profile
3. Delete server profile
4. Exit

Server profiles are saved to `profiles/*.json` (ignored by git).
Example JSON files are available in `examples/`.

After loading a server profile you can choose how channels are selected:

1. Use/create a saved channel profile
2. Enter channels manually (not saved)
3. Auto-download from all channels (password prompts via chat)
4. Delete channel profile
5. Start the bot with the current selection
6. Back

Channel profiles are saved to `channel_profiles/<server_profile>.json` (ignored by git).
Example JSON files are available in `examples/`.

## Chat Command

Once connected and joined to the base channel, send the command (case-insensitive):

```text
download files
```

You can send it either:

- as a private message to the bot (the bot replies privately), or
- in a channel (the bot replies in the channel).

In `auto_all` mode, if a channel likely requires a password, the bot will ask you
to reply with the password only, or `skip` to skip that channel.

## Run (CLI Mode - No Profiles)

```bash
python launcher.py ^
  --host 127.0.0.1 ^
  --tcp-port 10333 ^
  --udp-port 10333 ^
  --username your_login ^
  --password your_password ^
  --nickname "TT Downloader Bot" ^
  --channel-path "/Root/Files" ^
  --output-dir "./downloads"
```

In CLI mode the bot runs in `single` mode (base channel only).

## Security Notes

- Server/channel profiles saved by the interactive setup may contain passwords.
- These files are stored under `profiles/` and `channel_profiles/` and are ignored by git.
  Do not share them.

## Troubleshooting

- If you see "Failed to import the TeamTalk Python wrapper", make sure:
  - `TeamTalk_DLL/TeamTalk5.dll` exists, and
  - you're running the bot from the repository root folder, and
  - the DLL matches your Python architecture (x64 vs x86).

## Notes

- This bot downloads files from the server; it does not delete remote files.
- The TeamTalk SDK is proprietary. Make sure you comply with BearWare's license.
  See `THIRD_PARTY_NOTICES.md`.

## License

MIT (see `LICENSE`).
