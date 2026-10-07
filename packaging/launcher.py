"""Entry point of the packaged JARVIS: JARVIS.exe (the window, no console) and jarvis-cli.exe (the
command line) both run this, with the same arguments as the `jarvis` command."""

import multiprocessing
import sys

from jarvis.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
