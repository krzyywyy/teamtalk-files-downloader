import sys

from tt_downloader_bot import interactive_setup, main


if __name__ == "__main__":
    # Jeśli uruchomiono bez parametrów, pytamy o wszystko krok po kroku.
    if len(sys.argv) == 1:
        raise SystemExit(interactive_setup())

    # Jeśli jednak podano parametry, przekazujemy je do wersji CLI.
    raise SystemExit(main(sys.argv[1:]))

