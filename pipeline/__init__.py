"""pipeline 包：原 pipeline.py 拆出来的注册 → 支付 → team workflow orchestrator。

Wave C-1: 当前实现仍单文件 (`pipeline._monolith`), 包级 re-export 跟原 `pipeline.py`
顶层一致, 让 `from pipeline import X` / 老 `python pipeline.py` 仍能跑。

后续 Wave 会把 _monolith 内部的 modes / spawn / infra / oauth / util 分别拆出独立模块,
__init__.py 这里 re-export 出口保持兼容。
"""

from pipeline._monolith import (  # noqa: F401  (re-export)
    main,
    pipeline,
    batch,
    daemon,
    pay_only,
    register,
    pay,
    self_dealer,
    free_register_loop,
    free_backfill_rt_loop,
    promo_link_loop,
    ROOT,
    CARDW_DIR,
    CARD_DIR,
    CARD_PY,
    GOPAY_PY,
    QRIS_PY,
    RUNTIME_PAY,
    RUNTIME_REG,
    OUTPUT_DIR,
    DOMAIN_STATE_KEY,
    DAEMON_STATE_KEY,
    SECRETS_KEY,
    RUNTIME_DB_FILE,
    # Wave C re-export 漏: webui/backend/routes/proxy.py 直接 `from pipeline import WebshareClient` 等
    WebshareClient,
    WebshareQuotaExhausted,
    ProxyPool,
    _rotate_webshare_ip,
    _swap_gost_relay,
    _probe_gost_upstream,
    _ensure_gost_alive,
    # team / domain pool (其它 webui 路由偶发会 import)
    CloudflareDomainProvisioner,
    MultiZoneDomainProvisioner,
    DomainPool,
    TeamSystemClient,
    # cpa push (webui/backend/routes/inventory.py 调 pipeline._cpa_import_after_team)
    _cpa_import_after_team,
)

# 散户面板 (cpa_autofill) 推送 — 跟 _cpa_import_after_team 是两个独立目标:
# 前者推 cli-proxy-api admin 池, 后者推散户卖号面板
from pipeline.cpa_autofill import upload_accounts as _cpa_autofill_upload  # noqa: F401

