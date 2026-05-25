# Pull Request

> ⚠️ **Maintainers cannot manually reproduce your PR**. So the "proof of running" required in this template is not a formality—it's a hard requirement for PR merging. Missing evidence, PR gets closed immediately, no exceptions.
>
> Please read [`CONTRIBUTING.md`](../CONTRIBUTING.md) and [`NOTICE`](../NOTICE) before submitting a PR.

---

## 1. Change Type (Required)

Please check which category this PR belongs to (**only one**, different types require different evidence):

- [ ] 🐛 **Bug fix** — Fixed a behavior that breaks existing user configurations
- [ ] ✨ **New feature** — Added new functionality without breaking existing APIs
- [ ] 💥 **Breaking change** — Changes will break existing configurations (must describe migration path in final section)
- [ ] 🧠 **hCaptcha solver** — Added new challenge type / improved existing solver
- [ ] 🔄 **Protocol adaptation** — Patch for breaking changes from Stripe / PayPal / OpenAI
- [ ] 🛡️ **Daemon self-healing loop** — Added self-healing branch for new failure modes
- [ ] 🔬 **Research content** — Added new data / conclusions to `docs/anti-fraud-research.md`
- [ ] 📚 **Documentation** — README / docs/* / inline comments
- [ ] 🌍 **Translation** — Chinese-English translation, no behavior changes
- [ ] 🔧 **Toolchain / CI** — No runtime behavior changes

---

## 2. Change Summary (Required)

<!-- Describe in one or two sentences what was done -->



**Closes issue (if applicable)**: Closes #

---

## 3. Detailed Explanation + Design Decisions (Required, at least 100 words)

<!--
Answer these three things:
1. Why is the current implementation insufficient? What specific problem does this PR solve?
2. Why this solution over others? What trade-offs were considered?
3. What behavior does this PR change? What remains unchanged? How wide is the impact?

If protocol adaptation: Include specific behavior changes from Stripe / PayPal / OpenAI, e.g., endpoint changes, field name changes, new steps.
If solver challenge type: Include prompt text, frequency, why existing solver doesn't handle it.
-->



---

## 4. Proof of Running (Required—different types need different evidence)

> Maintainers can only judge PR validity based on this section. **PRs with insufficient evidence will not be merged.**

### A. Universal Requirements (All PRs)

- [ ] **Ran this PR's code locally** (not just code review of text changes)
- [ ] **No new real credentials / IPs / domains / PII introduced** — self-reviewed via `git diff main...HEAD`

### B. Additional Evidence by Type (Check corresponding branch based on §1)

#### 🐛 Bug fix / ✨ New feature / 💥 Breaking change

- [ ] **Reproduction command** (reviewer can copy and run directly):
  ```bash
  # How to trigger the fixed / implemented functionality?
  ```
- [ ] **Failure log before fix** (30–50 lines after redaction):
  <details><summary>Failure Log</summary>

  ```
  paste here
  ```
  </details>
- [ ] **Success log after fix** (same redaction):
  <details><summary>Success Log</summary>

  ```
  paste here
  ```
  </details>
- [ ] **Describe edge cases**: Which scenarios were tested? Which not covered?

#### 🧠 hCaptcha solver

- [ ] **Challenge prompt text** (complete string): `______`
- [ ] **Typical challenge screenshot** 1 image (after redaction, drag-upload in PR comment)
- [ ] **`/tmp/hcaptcha_auto_solver*/round_XX.json`** contents (successful round, key fields only):
  <details><summary>round JSON</summary>

  ```json
  paste here
  ```
  </details>
- [ ] **`checkcaptcha_pass_*.json`** at least 1 file (proof of actual pass)
- [ ] **Sample size**: How many runs? Pass rate?
  - Ran ___ times, passed ___ times, pass rate ___%
- [ ] **Both VLM path + heuristic path tested** (if new heuristic solver added)

#### 🔄 Protocol Adaptation (Stripe / PayPal / OpenAI changed)

- [ ] **Packet capture comparison** (mitmproxy / Burp):
  - Old response: attach 1 excerpt
  - New response: attach 1 excerpt
- [ ] **Service provider change notice / announcement link** (if any): ______
- [ ] **Local complete pipeline run final log** (proof of running through to `state=succeeded` or similar terminal state):
  <details><summary>Pipeline Log</summary>

  ```
  paste here
  ```
  </details>

#### 🛡️ Daemon Self-Healing Loop

- [ ] **Trigger scenario description**: When triggered? How frequently?
- [ ] **Log at trigger moment** (30 lines):
  <details><summary>Trigger Log</summary>

  ```
  paste here
  ```
  </details>
- [ ] **Next run log after successful self-healing** (proof of restored effectiveness):
  <details><summary>Recovery Log</summary>

  ```
  paste here
  ```
  </details>
- [ ] **State machine changes**: Field diffs in `output/daemon_state.json` before/after recovery

#### 🔬 Research Content

- [ ] **Data collection time window**: ______
- [ ] **Sample size + experiment setup**: Clearly explain control group, variables
- [ ] **Redaction check** (per [`CONTRIBUTING.md`](../CONTRIBUTING.md#redaction-checklist-for-research-contributions)):
  - [ ] IPs all use RFC 5737 ranges (`203.0.113.x` / `198.51.100.x` / `192.0.2.x`)
  - [ ] Domains all use `*.example` / `*.example.com`
  - [ ] ASN / ISP names use `AS-XX` / `ISP-A`
  - [ ] No real accounts / tokens / cookies
- [ ] **Does conclusion override / correct existing README conclusions?** List them

#### 📚 Documentation / 🌍 Translation / 🔧 Toolchain

- [ ] **Renders correctly after changes** (at least previewed on GitHub web)
- [ ] **Existing anchor links not broken** (internal cross-references in README and docs/ still work)
- [ ] **If CI changes**: Verified locally; attach GitHub Actions URL or local output

---

## 5. How Reviewer Validates Locally (Required)

<!--
Write the shortest reproducible steps for reviewer after checking out this branch.

Don't write "just follow the README"—write specific copy-paste commands.

Example:
1. Checkout this branch
2. cp CTF-pay/config.paypal.example.json CTF-pay/config.paypal.json
3. Fill in PayPal credentials + proxy URL
4. Set env var SKIP_HERMES_FAST_PATH=0
5. Run: xvfb-run -a python pipeline.py --pay-only --paypal --debug
6. Expect to see in log: "[B6 hermes] hit fast path: status=200"
-->



---

## 6. What Was Tested / Not Tested (Required)

### Tested Scenarios

- [ ] ______
- [ ] ______

### Known Uncovered Edge Cases

<!-- Be honest about this; "all tested" claims are less credible -->

- ______
- ______

### Breaking Change Migration Path (if checked "💥 Breaking change" in §1)

<!--
- Which config field name changed?
- How do existing users migrate?
- Needs highlight in next release changelog?
-->



---

## 7. Redaction Check (Required—mandatory checkboxes)

- [ ] I searched all PR changes for real IPs and replaced with `203.0.113.x` / `198.51.100.x` / `192.0.2.x`
- [ ] I searched all changes for real domains and replaced with `*.example`
- [ ] I searched all changes for real emails, tokens, cookies, card numbers and redacted/removed them
- [ ] I ran `git diff --cached` to verify no real credentials staged
- [ ] All logs / screenshots pasted in PR description are redacted

---

## 8. Authorization & License Confirmation (Required—mandatory checkboxes)

- [ ] The contributed content is written by me or from MIT-compatible licensed sources
- [ ] I agree my contribution is released under this project's [MIT License](../LICENSE)
- [ ] I have read and accept all terms in [`NOTICE`](../NOTICE)
- [ ] I have read [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)
- [ ] This PR is **not** assisting unauthorized use against any target
- [ ] All research / data / experiments in this PR were conducted on targets I own or have explicit authorization for

---

## 9. Other (Optional)

<!-- Any points for reviewer attention: future plans, related PR links, referenced papers, etc. -->



---

> Final check before submission: All **Required** sections filled? All **mandatory checkboxes** checked? Complete evidence provided for your PR type? Incomplete PRs get labeled `needs-info` and auto-closed after 14 days if not supplemented.