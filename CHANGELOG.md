# Changelog

Record of features and protocol changes for webui / pipeline / scripts, in reverse chronological order.

---

## [0074642] WebUI Account Panel Upgrade + Multiple Fixes for CPA / Registration Flows

> The **Runtime Data JSONL → SQLite Migration** was missing from the commit message; logging the full scope here. See `docs/architecture.md` (line 191 onwards) for SQLite storage details.

### Runtime Data Migration (Missed in previous message)
- Migrated account / payment / OAuth status from scattered `output/*.jsonl` files to a single SQLite database (`output/webui.db`):
  - `output/registered_accounts.jsonl` → Table `registered_accounts`
  - `output/results.jsonl` → Table `pipeline_results` + `card_results`
  - `output/secrets.json` / `daemon_state.json` / `webui_wizard_state.json` / `email_domain_state.json` / `wa_state.json` → Table `runtime_meta` (key/value JSON)
  - New table `oauth_status` for tracking separate OAuth flow states.
- Startup handles `_purge_legacy_runtime_files` to automatically clear old jsonl files, preventing data drift from double writes.
- Pipeline synchronization: `_append_result` and results.jsonl reading now all use the DB interface.

### New Features (Already in commit message)
- WebUI Account Inventory: Batch validation + batch deletion + plan inference (free/plus/team) + CPA push status display and "Push to CPA" button.
- Three-layer account validity verification: RT → AT → cookie. 401/invalid_grant judged as invalid, CF blocking/timeout judged as unknown.
- CPA preflight updated to use `GET /v0/management/auth-files` + Bearer.
- Codex OAuth `client_id` now has a backend fallback `app_EMoamEEZ73f0CkXaXp7hrann`; frontend no longer requires manual entry.
- Added `mode=direct` query parameter for webshare preflight.
- `config_writer` automatically injects `socks5://127.0.0.1:18898` for webshare mode, avoiding placeholder `USER:PASS` transmission from example templates.
- Fixed Vite `WEBUI_BASE` + `server.py` now mounts both `/` and `/webui/` for compatibility with direct and proxy connections.
- New favicon (`webicon.png`) + GitHub link in bottom right.
- Decoupled `batch`, `register_only`, and `pay_only` flags. `batch + register-only` = batch register N accounts without payment.
- Worker OTP extraction now excludes `#XXXXXX` hex colors + `color: / bgcolor=` contexts (fixes OpenAI email `#353740` false positive).
- `browser_register` detects OpenAI "Incorrect code" red text and fails immediately to avoid triggering `max_check_attempts` risk control.

---

## [bf0cca2] WhatsApp relay supports free engine switching
(Omitted)
