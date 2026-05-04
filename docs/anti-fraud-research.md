# Empirical Anti-Fraud Research

[← Back to README](../README.md)

> All data in this document comes from real production runs. **IPs are replaced with RFC 5737 documentation blocks (`203.0.113.0/24`, `198.51.100.0/24`, `192.0.2.0/24`), domains are replaced with `*.example`**, account counts and timestamps are unmodified — that is the core of the research itself.

---

## Abstract

ChatGPT Team's anti-fraud mechanism has two independent layers with very different behaviors:

1. **Probe Layer** (instant): Account-level / domain-level signals, evaluated at request time, tagged by **exact IP string** and **domain string**
2. **Ban Layer** (delayed batch): Batch correlation signals, run by backend scheduled tasks (typical time windows `00:00 UTC` and `12:00 UTC`), reviewed as `(time_window, payment_account, egress_ip, fingerprint)` tuples

**Single PayPal + single IP + dense registration yields ~2% 24-hour survival rate.** This is not a technical problem — it's a hard limit of batch correlation itself. To significantly improve survival rate, batch correlation must be reduced: multiple PayPal accounts, multiple ISP/city IPs, spread out timing, diversified fingerprints.

---

## Experiment 1: 45-Account Batch, 24-Hour Survival Rate

### Experiment Setup

- Single-day window (~14 hour span)
- **Same PayPal account** (PayPal-A)
- **Same egress IP** (`203.0.113.10`, labeled IP-A)
- **Seven adjacent subdomains** (5 `*.zone-a.example` + `zone-b.example` + `zone-c.example`)
- Total created: **45**

### Probe-Time Statistics

| Result | Count |
|---|---|
| `probe=ok` | 32 |
| `probe=no_permission` | 1 (`zone-c.example`, known persistently tagged domain) |
| Other errors | 12 |

From the probe-time perspective, it appears "32 accounts work, 1 domain has issues".

### 24-Hour Survival Rate

Direct query of the `gpt_accounts` table:

| Domain | Created | BAN | Survived | Avg `user_count` |
|---|---|---:|---:|---:|
| `subA1.zone-a.example` | 15 | 15 | 0 | 2.3 |
| `subA2.zone-a.example` | 8 | 8 | 0 | 4.0 |
| `subA3.zone-a.example` | 6 | 5 | **1** | 4.0 |
| `subA4.zone-a.example` | 5 | 5 | 0 | 4.6 |
| `subA5.zone-a.example` | 5 | 5 | 0 | 3.6 |
| `zone-b.example` | 5 | 5 | 0 | 3.6 |
| `zone-c.example` | 1 | 1 | 0 | 1.0 |
| **Total** | **45** | **44** | **1** | — |

**24-hour survival rate: 1/45 ≈ 2.2%.**

### Seat Fill Rate Distribution (Banned Accounts)

Accounts that actually had downstream users filled in were still banned:

| user_count | Banned Count |
|---|---|
| 5 / 5 | 17 |
| 4 / 5 | 13 |
| 3 / 5 | 4 |
| 2 / 5 | 1 |
| 1 / 5 | 8 |
| Full + 1 inviting | 1 |

**Seat fill rate is unrelated to survival.** Bans do not look at whether downstream users are using the account — they look at correlation at creation time.

### Batch BAN Timestamps

`updated_at` field aggregation (by banned status change time):

| Time Window | Concentrated Bans |
|---|---|
| 2026-04-19 12:xx UTC | 29 |
| 2026-04-19 07:xx UTC | 4 |
| Other scattered | 11 |

**Review runs on a fixed cron** (presumably near `00:00 UTC` and `12:00 UTC`). The `07:xx` batch may be another staggered review window.

### Survivor Comparison

Only one account survived (`subA3.zone-a.example` created 4/18 19:03, 5 seats full). **This is most likely a review miss, not genuinely stable.** An account created 5 days earlier (`subA4.zone-a.example` 4/14 23:19, 5+1 seats) is still alive, suggesting that early batches that pass a certain observation period become stable.

---

## Experiment 2: IP Dimension Comparison (Dual Proxy)

### Setup

- Same batch of `*.zone-a.example` subdomain candidates (10)
- Two proxy egress comparisons:
  - Proxy X: `203.0.113.10` (NY, ISP-A, AS-XX) — **same IP-A as Experiment 1**
  - Proxy Y: `203.0.113.20` (VA, ISP-B, AS-YY) — clean IP
- Same PayPal-A, same Camoufox fingerprint

### Results

| Proxy | Egress IP | Probe `ok` Count | Completed | `ok` Rate |
|---|---|---|---:|---:|---:|
| X (NY) | `203.0.113.10` | 1 | 7 | **14%** |
| Y (VA) | `203.0.113.20` | 3 | 3 | **100%** |

**Same subdomain `subA3.zone-a.example`: from X it returns `no_perm`, from Y it returns `ok`.**

### Conclusion

**The domain is not the main factor — the IP is.** Probe-layer tagging targets IPs.

---

## Experiment 3: Same ASN, Different IP

### Setup

Switch to proxy Z: `203.0.113.30`, **same ISP as IP-A (ISP-A), same city (NY), same ASN (AS-XX)**, but different IP string.

Immediately retest the same batch of subdomains.

### Results

| Proxy | Egress IP | Probe `ok` | Completed | `ok` Rate | First `no_perm` Trigger |
|---|---|---|---:|---:|---:|---|
| X (old NY, tagged) | `203.0.113.10` | 1 | 7 | 14% | Rejected on 2nd attempt |
| **Z (new NY, clean)** | `203.0.113.30` | **4** | **5** | **80%** | **Rejected only on 5th attempt** |

### Conclusion

**Switch to another IP within the same ISP / same city / same ASN, and "clean state" is immediately restored.**

→ ChatGPT's tagging granularity is **exact IP string**, not ASN / city / ISP.

### IP Lifetime

Each IP can sustain approximately **4–5** registrations before flipping to `no_perm` status (probe starts rejecting on the 5th attempt). Recovery methods:

- **Change IP**: immediate recovery (same ASN works, as long as the IP string is different)
- **Wait a few hours**: natural recovery (based on observation of `subA1.zone-a.example` briefly recovering after 2h)

---

## Refuted Early Hypotheses

| Early Hypothesis | Data | Refuted |
|---|---|---|
| Anti-fraud granularity is "registration email domain" | Five clean subdomains wiped on the same day | ❌ It's batch correlation, not per-domain |
| All five `*.zone-a.example` subdomains are stable | All five batches wiped on the same day | ❌ The accounts across these five domains were cleared in the same batch; before being banned they all looked "ok" |
| Same IP + same PayPal + concentrated registration doesn't trigger anti-fraud (12 consecutive ok) | 24h survival ≈ 2% | ❌ probe=ok is only the instant state |
| 30+ in a single day doesn't trigger new burn | All 45 accounts banned in a batch | ❌ It does, just with a 12-hour delay |
| `probe=no_perm` is domain-level tagging | Same domain gives opposite results under different IPs | ❌ The dominant signal is IP |

---

## Revised Anti-Fraud Model

### Two Independent Mechanisms

```
┌─────────────────────────────────────────────────────┐
│              Probe Layer (Instant)                  │
│                                                     │
│  signal = (egress_ip_string, email_domain_string?)  │
│  evaluation = at request time                       │
│  IP lifetime = ~4-5 registrations                   │
│  domain tagging = minority domains permanently down │
└─────────────────────────────────────────────────────┘
                       ↓
                 Registration may be ok
                       ↓
┌─────────────────────────────────────────────────────┐
│              Ban Layer (Delayed Batch)              │
│                                                     │
│  signal = (time_window, payment_account,            │
│            egress_ip, fingerprint, ...)             │
│  evaluation = scheduled cron @ ~00:00 / 12:00 UTC  │
│  whole batch correlated → whole batch banned        │
└─────────────────────────────────────────────────────┘
                       ↓
                95% dead by next morning
```

### Key Insights

1. **Probe=ok does not mean stable.** It only means the cohort hasn't been reviewed yet
2. **Domain-level tagging is rare** (e.g. `zone-c.example`), less prevalent than expected
3. **IP string is the dominant signal in the probe layer**
4. **Batch correlation is the dominant signal in the ban layer** — reducing dimension sharing within cohorts is essential

---

## Engineering Impact

### What Existing Tools Handle

- ✅ DomainPool + permanent_burned handles probe-layer "persistently tagged domains"
- ✅ Webshare API + gost relay handles IP lifetime
- ✅ Multi-zone domain pool handles CF quota + zone-level risk

### What Existing Tools Cannot Handle

- ❌ **Batch-correlation-driven mass extinction by next day**: daemon mode with target=20 inventory after a batch BAN may drop to 0–1, essentially restarting from scratch each night
- ❌ Domain rotation / on-demand subdomain provisioning is ineffective against "next-day mass extinction"

### Methods to Improve 24-Hour Survival Rate

To significantly reduce batch correlation (ordered by effectiveness):

1. **Multiple PayPal account pool**: rotate after 1–3 orders per account, spread out timing. This is the strongest signal
2. **Multiple proxy egress IPs, cross-ISP / cross-city**: not multiple slots from the same provider, but genuinely different ISPs
3. **Time dispersion**: 2–3 per day, spaced hours apart, not 30+ in one hour
4. **Camoufox fingerprint diversification**: `humanize` + different OS/screen profiles
5. **Reduce single PayPal reuse rate**: multiple Team subscriptions from the same PayPal in a short time is itself a strong fraud signal

Currently the pipeline only does "60–180s jitter spacing" and "multi-domain rotation" — **all other dimensions share the same parameters**. This is the root cause of the 2% survival rate.

---

## Directions for Future Investigation

The following lack sufficient experimental data and are left for future work:

1. **Real ROI of Camoufox fingerprint diversification**: Do different OS/screen profiles actually increase cohort separation?
2. **PayPal trust decay**: Survival rate curve as a PayPal account runs through N orders
3. **Optimal time dispersion interval**: How much spacing between cohorts is enough to appear "independent"?
4. **Fingerprint reuse vs IP reuse — which has higher weight?**: Controlled experiment with fixed other dimensions
5. **Is there a "warm-up" path?**: Can performing "normal user" actions before a ban reduces the probability of getting banned?

---

## Reproduction Guide

To reproduce these experiments:

1. Prepare at least 2 proxy egress points from different ASNs
2. Configure daemon mode and run for at least a week
3. Query the `gpt_accounts` table's `is_banned` status changes daily with sqlite3
4. Key query:

```sql
-- Batch characteristics of accounts banned within 24h
SELECT
    DATE(created_at) AS create_day,
    HOUR(updated_at) AS ban_hour,
    proxy_ip,
    payment_account,
    COUNT(*) AS cnt
FROM gpt_accounts
WHERE is_banned = 1
  AND updated_at > created_at + INTERVAL '12 hours'
GROUP BY 1, 2, 3, 4
ORDER BY 1 DESC, 2 DESC;
```

5. Group control groups by dimension differences (same PayPal vs different PayPal, same IP vs different IP, etc.)
6. Data anonymization method per this document (RFC 5737 IPs, `*.example` domains)

If you produce new data, feel free to submit a PR to add it here following [`CONTRIBUTING.md`](../CONTRIBUTING.md#research-contribution-anonymization-checklist).

---

## Citation

If your research / paper / blog cites data from this document, please cite:

```
Gpt-Agreement-Payment — Empirical Anti-Fraud Research. (2026).
https://github.com/DanOps-1/Gpt-Agreement-Payment/blob/main/docs/anti-fraud-research.md
```

Or BibTeX:

```bibtex
@misc{Gpt-Agreement-Payment-antifraud,
  title  = {Empirical Anti-Fraud Research on ChatGPT Team Subscription},
  author = {Gpt-Agreement-Payment contributors},
  year   = {2026},
  howpublished = {\url{https://github.com/DanOps-1/Gpt-Agreement-Payment}},
  note   = {Licensed under MIT, IP addresses use RFC 5737 placeholders}
}
```