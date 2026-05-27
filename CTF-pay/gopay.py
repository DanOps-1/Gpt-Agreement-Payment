#!/usr/bin/env python3
"""兼容 shim: 旧调用 `python CTF-pay/gopay.py …` 透传到 gopay/ 包。

新写法是 `cd CTF-pay && python -m gopay …`。
实际代码在 gopay/_monolith.py + gopay/sign/* + gopay/protocol/*。
"""

import sys
from pathlib import Path

# 让 `import gopay` 解析到旁边的 gopay/ 包 (而不是这个文件本身)
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from gopay.__main__ import main

if __name__ == "__main__":
    main()
