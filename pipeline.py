#!/usr/bin/env python3
"""Compatibility shim: old calls `python pipeline.py …` are passed through to the `pipeline/` package.

The new way is `python -m pipeline …`. The actual entry point and all code are in `pipeline/_monolith.py`,
later Wave will further incrementally split modes/spawn/infra/oauth/util submodules."""

from pipeline.__main__ import main

if __name__ == "__main__":
    main()
