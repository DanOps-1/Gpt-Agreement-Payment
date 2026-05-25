#!/usr/bin/env python3
"""Compatibility shim: old calls `python CTF-pay/card.py …` pass through to card/ package.

New usage is `cd CTF-pay && python -m card …`. Actual 9050 lines implementation in card/_monolith.py."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # CTF-pay/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from card.__main__ import main

if __name__ == "__main__":
    main()
