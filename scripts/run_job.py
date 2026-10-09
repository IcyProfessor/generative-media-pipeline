"""Compatibility launcher; install the project first with pip install -e ."""
from genmedia.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
