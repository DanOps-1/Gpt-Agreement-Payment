#!/usr/bin/env python3
"""Compatibility shim: Old calls `python CTF-pay/gopay.py …` pass through to the gopay/ package.

New usage is `cd CTF-pay && python -m gopay …`.
Actual code is in gopay/_monolith.py + gopay/sign/* + gopay/protocol/*."""

import sys
from pathlib import Path

# Make `import gopay` resolve to the adjacent gopay/ package (not this file itself)
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from gopay.__main__ import main

if __name__ == "__main__":
    main()
