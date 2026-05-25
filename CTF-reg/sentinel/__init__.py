"""OpenAI sentinel-chat-requirements token generation: 3 variants share get_sentinel_token entry point.

- pure_python: current main entry (relies on quickjs fallback)
- quickjs: runs OpenAI's openai_sentinel_quickjs.js (sandboxed JS, true user-agent calculation)
- v1_legacy: legacy pure Python implementation, deprecated but retained as fallback"""

from sentinel.pure_python import get_sentinel_token  # noqa: F401
