#!/usr/bin/env python3
"""兼容 shim: 旧调用 `python CTF-pay/qris.py …` 透传到 qris/ 包。

新写法是 `cd CTF-pay && python -m qris …`。实际代码在 qris/_monolith.py。
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # CTF-pay/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from qris.__main__ import main

if __name__ == "__main__":
    main()
