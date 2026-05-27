#!/usr/bin/env python3
"""兼容 shim：旧调用 `python pipeline.py …` 透传到 `pipeline/` 包。

新写法是 `python -m pipeline …`。实际入口与全部代码在 `pipeline/_monolith.py`,
后续 Wave 会再增量拆 modes/spawn/infra/oauth/util 子模块。
"""

from pipeline.__main__ import main

if __name__ == "__main__":
    main()
