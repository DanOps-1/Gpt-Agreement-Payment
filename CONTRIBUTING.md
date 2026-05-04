# Contribution Guidelines

Thank you for being willing to contribute. This is a research project, where being "useful" is more important than being "perfectly compliant with rules," but following these points will make your PR easier to merge.

> ⚠️ **Important: Maintainers cannot manually reproduce your PR locally.**
>
> This means whether a PR can be merged **depends entirely on whether the "proof of success" you provide is sufficient**. This is not formalism:
>
> - **PRs for solver types**: Must attach prompt text, `round_XX.json`, `checkcaptcha_pass_*.json`, and pass rate statistics.
> - **PRs for protocol adaptation**: Must attach packet capture comparisons (before / after) + complete pipeline logs to the final state.
> - **PRs for daemon self-healing**: Must attach logs at the moment of triggering + logs of the next successful round after self-healing + `SQLite runtime_meta[daemon_state]` field differences.
> - **Bug fixes / new features**: Must attach reproduction commands + failure logs before fix + success logs after fix.
>
> For detailed requirements, see the [PR Template](.github/PULL_REQUEST_TEMPLATE.md). All "required" items must be filled, and all "mandatory checkboxes" must be checked. **If evidence is missing, the PR will be labeled `needs-info` and automatically closed after 14 days without explanation.**

## Desired Contributions

Sorted roughly by impact:

1. **New hCaptcha question type solvers.** If you see a question type not covered by the solver, add it. Integration methods are in [`README.md`](README.md#adding-new-question-types) — three small functions + one dispatcher registration. If you have public prompt -> label datasets, even better, please mention them in the PR.
2. **Protocol stage adaptation.** Stripe / PayPal / OpenAI have breaking changes every few weeks. After applying a patch and running it successfully a few times, submit a PR and add a line to the `pipeline.py` changelog comments.
3. **Daemon self-healing loop.** Failure modes you have **actually** encountered, attached with failure logs (not just hypothetical). Add detection + recovery + state flag cleanup.
4. **Reverse engineering notes.** If you reverse engineer new endpoints (new invitation mechanism, new management API surface, etc.) and want to add documentation to the README research section, you're welcome. Follow existing anonymization: RFC 5737 IPs, `*.example` domains.
5. **Translation / Documentation polishing.** Plenty of inline comments are still in Chinese; PRs to translate them to English are accepted, provided no behavior is changed.
6. **`flows/` test fixtures.** Real packet captures cannot be shipped (contains cookies / PII), but anonymized fixtures or tools to generate fixtures are useful.

## Not Wanted

- "I ran it against $TARGET and got banned". Read the [README Empirical Evidence Section](README.md#-anti-fraud-empirical-evidence) — being banned is expected; that's exactly what we are studying.
- Requests for help running the toolset against unauthorized targets. Closed immediately, potential ban. See [`SECURITY.md`](SECURITY.md).
- PRs that only refactor without adding functionality. `card.py` is intentionally kept as an 8000-line single file; after splitting, diffs are harder to track and incidental complexity doesn't decrease.
- Introducing new ML model dependencies without a strong reason. The ML venv is already 4 GB.

## Workflow

1. **Open an issue first for larger changes.** 5 lines of discussion can save a 500-line PR going in the wrong direction.
2. **Branch from `main`**, naming it `feat/<thing>` or `fix/<thing>`.
3. **Write human-readable commit messages.** Imperative mood, lowercase first letter, first line ≤72 characters, write details in the body if needed. Follow the style in `git log --oneline | head`.
4. **Test if you can.** The project heavily depends on online services; coverage is not mandatory, but if it can be simulated with offline-mock or local-mock (`config.local-mock.json`), do it.
5. **Anonymize diffs before submitting.** `.gitignore` already excludes `output/` / `flows/` / `paypal_cf_persist/` / runtime configs, but check `git diff --cached` to ensure no real cookies / tokens / IPs / emails leaked into the stage.
6. **PR title** follows the same format as commit. Description answers three things: what was changed, why, and how it was tested.

## Coding Style

- Python: Basic PEP-8, 4-space indentation, no strict line length, no mandatory auto-formatter (existing code has its own rhythm; just match locally).
- Comments: Either Chinese or English. English is preferred for new code expected to be read by non-Chinese speakers.
- Logs: Keep enough context to spot problems from `tail`. Current pattern: `[STAGE] something something detail=...`
- Configuration: Prefer adding flags to existing JSON sections rather than creating new top-level sections. Document user-visible flags in `README.md`.

## Anonymization Checklist for Empirical Data

To add content to the [Empirical Evidence Section](README.md#-anti-fraud-empirical-evidence):

- IPs: Use `203.0.113.x` (TEST-NET-3), `198.51.100.x` (TEST-NET-2), `192.0.2.x` (TEST-NET-1). **Never** post real IPs, even if used briefly.
- Domains: Use `*.example`, `*.example.com`, `*.test`, `*.invalid`. **Never** post real domains.
- Emails: `you@example.com`, `tester@example.com`
- Account numbers and times: **Keep them real**, as they are the research itself.
- Internal ASNs / Organization names: Replace with `AS-XX`, `ISP-A`.
- ChatGPT Account IDs / tokens / cookies: **Never** post them, not even truncated.

## Questions

Open a discussion or issue. No chat, no Discord, no mailing list.
