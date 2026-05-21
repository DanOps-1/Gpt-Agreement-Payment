"""QRIS 支付包 (印尼央行统一二维码标准, 无 OTP/PIN/绑定)。

Wave E-1: 原 CTF-pay/qris.py (887 行) 整体进 _monolith.py, 后续 Wave 再拆
charger / card_bridge / mock / render / cli 子模块。
"""

from qris._monolith import QrisCharger, main  # noqa: F401
