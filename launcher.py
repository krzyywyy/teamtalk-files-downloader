import sys

from tt_downloader_bot import interactive_setup, main


if __name__ == "__main__":
    # If started without arguments, run the interactive setup.
    if len(sys.argv) == 1:
        raise SystemExit(interactive_setup())

    # If arguments were provided, pass them to the CLI version.
    raise SystemExit(main(sys.argv[1:]))
