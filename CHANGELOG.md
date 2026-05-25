# Changelog

Records functional and protocol changes for webui / pipeline / scripts, ordered by commit descending.

---

## PayPal Protocol Payment Completion Plus Subscription Path

Previously `--paypal` was entirely on the Team path; Plus is already supported in modern path, but abcard + WebUI export + CLI all had gaps. This PR completes it.

- **`pipeline.py`** adds `--plan {team,plus}`: Before all branches (single / pay-only / batch / daemon / self-dealer), perform one config override first—generate temp config to align `fresh_checkout.plan.plan_name` / `entry_point` / `promo_campaign_id` to target plan. Plus mode additionally strips `workspace_name` / `seat_quantity`, doesn't touch user's original file, cleanup on atexit after run
- **`card.py::_build_abcard_checkout_payload`**: No longer force-inject `workspace_name` / `seat_quantity` when detecting `plus` plan_name. Previously Plus + access_token / abcard path would send team fields together, causing ChatGPT backend 400
- **`card.py::_provision_openai_auth_via_local_bundle`**: Strip team fields from ab_cfg.team_plan example residue for Plus, avoid plan_name / seat field mismatch during CTF-reg account creation phase
- **`webui/backend/config_writer.py`**: When exporting PayPal / Plus config, actively strip team-only fields from both `fresh_checkout.plan` and `team_plan` segments. Previously `_deep_merge` would retain example skeleton defaults like `seat_quantity=5` / `workspace_name=MyWorkspace`, polluting Plus export
- **`config.paypal.example.json`**: Add `_comment` to plan section, hint which fields to change for Plus switch or use `--plan plus` directly
- Test coverage:
  - `webui/tests/test_pipeline_plan_override.py` covers `_apply_plan_override` Plus / Team behavior + `_build_abcard_checkout_payload` Plus payload
  - `webui/tests/test_config_writer.py` adds `test_export_strips_team_only_fields_when_plan_is_plus` / `test_export_keeps_team_fields_when_plan_is_team`

---

## GoPay Payment 429 Risk Control Bypass

`CTF-pay/gopay.py::_midtrans_init_linking` adds risk control bypass path:

- **Trigger condition**: Midtrans `POST /snap/v3/accounts/{snap}/linking` returns 429, or body contains keywords like `technical error` / `too many` / `rate limit` (certain IPs / high-frequency scenarios always fail)
- **Bypass approach**: Resend to same endpoint with same body, but **strip `Authorization: Basic …` header**. Requests without Auth bypass Midtrans SDK's risk control branch, directly returning `201 + activation_link_url`. Downstream `validate-reference / user-consent / OTP / PIN` flows unchanged
- **Failure fallback**: When bypass also fails, throw `GoPayError("midtrans linking bypass failed …")`, allowing daemon layer to leverage retry / IP rotation logic
- Test coverage: `test_linking_429_bypass_drops_authorization` / `test_linking_200_with_technical_error_body_triggers_bypass` / `test_linking_429_bypass_also_fails_raises`

---

## [0074642] webui Account Panel Major Upgrade + CPA / Registration Path Multiple Fixes

> Commit message missed **runtime data JSONL → SQLite major migration**. Full scope details here. See `docs/architecture.md` line 191+ for SQLite storage explanation.

### Runtime Data Migration (omitted from previous message)
- Account / payment / OAuth state migrated from scattered `output/*.jsonl` files to single SQLite (`output/webui.db`):
  - `output/registered_accounts.jsonl` → table `registered_accounts`
  - `output/results.jsonl` → tables `pipeline_results` + `card_results`
  - `output/secrets.json` / `daemon_state.json` / `webui_wizard_state.json` / `email_domain_state.json` / `wa_state.json` → table `runtime_meta` (key/value JSON)
  - New table `oauth_status` separately tracks OAuth flow state
- `_purge_legacy_runtime_files` auto-cleans old jsonl on startup, avoiding data drift from dual-write
- Pipeline call site synchronized: `_append_result` / reading results.jsonl all switched to db interface

### New Features (already in commit message)
- webui account inventory: batch verify + batch delete + plan inference (free/plus/team) + CPA push status display and "push→CPA" button
- Account validity verification three-layer liveness: rt → at → cookie, judge invalid on 401/invalid_grant, judge unknown on CF block/timeout
- CPA preflight switched to `GET /v0/management/auth-files` + Bearer
- Codex OAuth `client_id` backend hardcoded fallback `app_EMoamEEZ73f0CkXaXp7hrann`, frontend no longer lets user manually enter
- webshare preflight adds `mode=direct` query parameter
- `config_writer` webshare mode auto-injects `socks5://127.0.0.1:18898`, avoiding `USER:PASS` placeholder passthrough from example template
- vite `WEBUI_BASE` fix + `server.py` simultaneously serves `/` and `/webui/`, both direct and reverse-proxy work
- New favicon (`webicon.png`) + GitHub link bottom-right
- `batch` / `register_only` / `pay_only` three flags decoupled, `batch + register-only` = batch register N without payment
- worker OTP extraction excludes `#XXXXXX` hex colors + `color: / bgcolor=` context (OpenAI email `#353740` false positive root cause)
- `browser_register` detects OpenAI "Incorrect code" red text and fails immediately, avoiding `max_check_attempts` risk control trigger

---

## [bf0cca2] WhatsApp Relay Engine Free Switching Support
(earlier omitted)