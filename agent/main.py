import sys

from forgepilot.cli import main as cli_main


def main(argv: list[str] | tuple[str, ...] = ()) -> int:
    return cli_main(list(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
