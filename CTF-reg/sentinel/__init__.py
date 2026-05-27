"""OpenAI sentinel-chat-requirements token 生成: 3 个变体共享 get_sentinel_token 入口。

- pure_python: 当前 main entry (依赖 quickjs fallback)
- quickjs: 跑 OpenAI 的 openai_sentinel_quickjs.js (沙盒 JS, 真 user-agent 计算)
- v1_legacy: 旧版纯 Python 实现, 已 deprecate 但保留作 fallback
"""

from sentinel.pure_python import get_sentinel_token  # noqa: F401
