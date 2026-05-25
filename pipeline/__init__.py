"""pipeline package: registration → payment → team workflow orchestrator extracted from original pipeline.py.

Wave C-1: Current implementation still single file (`pipeline._monolith`), package-level re-export consistent with original `pipeline.py` top-level, allowing `from pipeline import X` / legacy `python pipeline.py` to still work.

Subsequent Waves will extract modes / spawn / infra / oauth / util from inside _monolith into independent modules, __init__.py re-export interface here maintains compatibility."""

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
    # Wave C re-export miss: webui/backend/routes/proxy.py directly `from pipeline import WebshareClient` etc
    WebshareClient,
    WebshareQuotaExhausted,
    ProxyPool,
    _rotate_webshare_ip,
    _swap_gost_relay,
    _probe_gost_upstream,
    _ensure_gost_alive,
    # team / domain pool (other webui routes occasionally import)
    CloudflareDomainProvisioner,
    MultiZoneDomainProvisioner,
    DomainPool,
    TeamSystemClient,
    # cpa push (webui/backend/routes/inventory.py calls pipeline._cpa_import_after_team)
    _cpa_import_after_team,
)

# retail panel (cpa_autofill) push — independent targets separate from _cpa_import_after_team:
# former pushes cli-proxy-api admin pool, latter pushes retail account selling panel
from pipeline.cpa_autofill import upload_accounts as _cpa_autofill_upload  # noqa: F401

