"""Run the same CLI exposed by the installed ``bilibili-crawler`` command."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
