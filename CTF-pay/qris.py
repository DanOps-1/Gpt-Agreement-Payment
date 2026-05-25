#!/usr/bin/env python3
"""Compatibility shim: old calls `python CTF-pay/qris.py …` pass through to qris/ package.

New usage is `cd CTF-pay && python -m qris …`. Actual code is in qris/_monolith.py."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # CTF-pay/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from qris.__main__ import main

if __name__ == "__main__":
    main()
