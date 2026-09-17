"""Allows running the converter with: python -m tja2osz <file.tja | folder> [options]"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
