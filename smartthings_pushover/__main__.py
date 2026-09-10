"""`python -m smartthings_pushover` entry point; see `cli.main`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
