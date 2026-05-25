# Contribution Guide

Thanks for being willing to contribute. This is a research project where "useful" matters more than "perfectly compliant," but the following things will make your PR easier to merge.

> ⚠️ **IMPORTANT: Maintainers cannot manually reproduce your PR locally.**
>
> This means whether a PR gets merged depends **entirely on the quality of "working proof" you provide**. This isn't bureaucracy:
>
> - **Solver problem type PR**: must include prompt text, `round_XX.json`, `checkcaptcha_pass_*.json`, pass rate statistics
> - **Protocol adaptation PR**: must include packet capture comparison (before / after) + complete end-to-end pipeline logs
> - **Daemon self-healing PR**: must include logs at the moment of trigger + logs of successful next round after recovery + `SQLite runtime_meta[daemon_state]` field diff
> - **Bug fix / new feature**: must include reproduction command + failure logs before fix + success logs after fix
>
> See detailed requirements in [PR template](.github/PULL_REQUEST_TEMPLATE.md). Fill all "required" fields and check all "mandatory" checkboxes. **Missing evidence means the PR gets tagged `needs-info` and auto-closes after 14 days with no explanation.**

## Wanted Contributions

Roughly ordered by impact:

1. **New hCaptcha problem type solvers.** See a solver type not covered? Add it. Integration method in [`README.md`](README.md#加新题型) — three small functions + one dispatcher registration. If you have public prompt → label datasets, mention that in the PR
2. **Protocol stage adaptation.** Stripe / PayPal / OpenAI have breaking changes roughly every few weeks. After patching and running stable a few times, submit a PR and add a line in `pipeline.py` changelog comment
3. **Daemon self-healing loops.** Failure modes you've **actually** encountered, with failure logs attached—don't just speculate. Add detection + recovery + state flag cleanup
4. **Reverse engineering notes.** Figured out new endpoints (new invitation mechanisms, new management API surfaces, etc.), want to add documentation to README research section? Welcome. Follow current style with obfuscation: RFC 5737 IPs, `*.example` domains
5. **Translations / documentation polish.** Lots of inline comments still in Chinese; PRs translating to English are accepted, as long as behavior doesn't change
6. **`flows/` test fixtures.** Real packet captures can't ship (contain cookies / PII), but obfuscated fixtures or tools for generating them are useful

## Unwanted

- "I ran this against $TARGET and got banned." Read [README empirical section](README.md#-反欺诈实证) — bans are expected, that's what we research
- Requests to help run the toolset against unauthorized targets. Direct close, possible ban. See [`SECURITY.md`](SECURITY.md)
- Refactor-only PRs with no new functionality. `card.py` is deliberately one 8000-line file; splitting it makes diffs harder to follow and doesn't reduce incidental complexity
- Introducing new ML model dependencies without solid justification. The ML venv is already 4 GB

## Workflow

1. **Open an issue for bigger changes first.** 5 lines of discussion saves 500 lines of PR going the wrong way
2. **Branch from `main`**, named `feat/<thing>` or `fix/<thing>`
3. **Write human-readable commit messages.** Imperative mood, lowercase first letter, first line ≤72 characters, explanation in body if needed. Look at `git log --oneline | head` and follow the style
4. **Test if you can.** The project heavily depends on online services; coverage isn't required, but simulate with offline-mock or local-mock (`config.local-mock.json`) where possible
5. **Sanitize diffs before committing.** `.gitignore` already excludes `output/` / `flows/` / `paypal_cf_persist/` / runtime configs, but do `git diff --cached` once to ensure no real cookies / tokens / IPs / emails sneak into staging
6. **PR title** follows commit format. Description answers three things: what changed, why, how you tested

## Code Style

- Python: basically PEP-8, 4-space indent, no hard line length, no mandatory auto-formatter (existing code has its own rhythm; match locally)
- Comments: Chinese or English both fine. Use English for new code expected to be read by non-Chinese speakers
- Logging: provide enough context to see the problem from `tail`. Current pattern: `[STAGE] something something detail=...`
- Config: prefer adding flags to existing JSON sections over creating new top-level ones. Document user-visible flags in `README.md`

## Sanitization Checklist for Research Content Contributions

Adding to [empirical section](README.md#-反欺诈实证):

- IPs: use `203.0.113.x` (TEST-NET-3), `198.51.100.x` (TEST-NET-2), `192.0.2.x` (TEST-NET-1). **Never** post real IPs, even if only temporarily used
- Domains: use `*.example`, `*.example.com`, `*.test`, `*.invalid`. **Never** post real domains
- Emails: `you@example.com`, `tester@example.com`
- Account counts and times: **keep accurate**, that's the research itself
- Internal ASNs / org names: replace with `AS-XX`, `ISP-A`
- ChatGPT account IDs / tokens / cookies: **never** post, truncation doesn't help

## Questions

Open a discussion or issue. No chat, Discord, or mailing list.