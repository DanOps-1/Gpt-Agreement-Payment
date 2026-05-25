# Anti-Fraud Empirical Research

[← Back to README](../README.md)

> All data in this document comes from actual production runs. **IPs are masked using RFC 5737 documentation ranges (`203.0.113.0/24`, `198.51.100.0/24`, `192.0.2.0/24`), domains are masked with `*.example`**, account counts and timestamps are unchanged — that is the core of the research itself.

---

## Abstract

ChatGPT Team's anti-fraud mechanism operates on two independent layers with very different behavioral characteristics:

1. **Probe layer** (real-time): Account-level / domain-level signals, evaluated in real-time at request time, labeled by **exact IP string** and **domain string**
2. **Ban layer** (delayed batch): Batch association signals, executed by backend cron jobs (typical windows `00:00 UTC` and `12:00 UTC`), reviewed by packaging `(time_window, payment_account, exit_ip, fingerprint)` tuples

**Single PayPal + single IP + dense registration has ~2% survival rate over 24 hours.** This is not a technical issue, but a hard limit of batch association itself. To significantly improve survival rate, you must reduce batch association: multiple PayPals, multiple ISP/city IPs, time distribution, and fingerprint diversity.

---

## Experiment 1: 45-Account Batch, 24-Hour Survival Rate

### Experiment Setup

- Single day window (~14 hour span)
- **Single PayPal account** (PayPal-A)
- **Single exit IP** (`203.0.113.10`, labeled IP-A)
- **Seven adjacent subdomains** (5 `*.zone-a.example` + `zone-b.example` + `zone-c.example`)
- Total creation: **45 accounts**

### Statistics at Probe Time

| Result | Count |
|---|---|
| `probe=ok` | 32 |
| `probe=no_permission` | 1 (domain `zone-c.example`, known persistent label) |
| Other errors | 12 |

From the probe perspective, it appeared "32 accounts active, 1 domain has issues."

### 24-Hour Survival Rate

Direct query on `gpt_accounts` table:

| Domain | Created | Banned | Survived | Avg `user_count` |
|---|---:|---:|---:|---:|
| `subA1.zone-a.example` | 15 | 15 | 0 | 2.3 |
| `subA2.zone-a.example` | 8 | 8 | 0 | 4.0 |
| `subA3.zone-a.example` | 6 | 5 | **1** | 4.0 |
| `subA4.zone-a.example` | 5 | 5 | 0 | 4.6 |
| `subA5.zone-a.example` | 5 | 5 | 0 | 3.6 |
| `zone-b.example` | 5 | 5 | 0 | 3.6 |
| `zone-c.example` | 1 | 1 | 0 | 1.0 |
| **Total** | **45** | **44** | **1** | — |

**24-hour survival: 1/45 ≈ 2.2%.**

### Seat Occupancy Distribution (Banned Accounts)

Downstream users filling seats also got banned:

| user_count | Banned accounts |
|---|---:|
| 5 / 5 | 17 |
| 4 / 5 | 13 |
| 3 / 5 | 4 |
| 2 / 5 | 1 |
| 1 / 5 | 8 |
| Full + 1 invite pending | 1 |

**Seat occupancy is irrelevant to survival.** Bans don't check downstream usage; they check creation-time association.

### Bulk BAN Timestamps

Aggregation of `updated_at` field (by ban status change time):

| Time Window | Concentrated Bans |
|---|---:|
| 2026-04-19 12:xx UTC | 29 accounts |
| 2026-04-19 07:xx UTC | 4 accounts |
| Other scattered | 11 accounts |

**Review runs on fixed cron** (presumed `00:00 UTC` and `12:00 UTC` vicinity). The `07:xx` wave may be another batch review window.

### Survivor Analysis

Only one account survived (`subA3.zone-a.example`, created 4/18 19:03, 5 seats full). **This is very likely review evasion, not genuinely stable.** One account created 5 days earlier (`subA4.zone-a.example`, created 4/14 23:19, 5+1 seats) remains active, suggesting early batches become stable after passing a certain observation period.

---

## Experiment 2: IP Dimension Control (Dual Proxy)

### Setup

- Same batch of `*.zone-a.example` subdomain candidates (10 total)
- Two proxy exit controls:
  - Proxy X: `203.0.113.10` (NY, ISP-A, AS-XX) — **same IP-A as Experiment 1**
  - Proxy Y: `203.0.113.20` (VA, ISP-B, AS-YY) — clean IP
- Same PayPal-A, same Camoufox fingerprint

### Results

| Proxy | Exit IP | Probe `ok` Count | Completed | `ok` Rate |
|---|---|---:|---:|---:|
| X (NY) | `203.0.113.10` | 1 | 7 | **14%** |
| Y (VA) | `203.0.113.20` | 3 | 3 | **100%** |

**Same subdomain `subA3.zone-a.example`: returns `no_perm` from X, returns `ok` from Y.**

### Conclusion

**Domain is not the primary factor; IP is.** The probe layer labels by IP.

---

## Experiment 3: Same ASN, Different IP

### Setup

Switch to proxy Z: `203.0.113.30`, **same ISP (ISP-A), same city (NY), same ASN (AS-XX) as IP-A, but different IP string**.

Re-test the same batch of subdomains immediately.

### Results

| Proxy | Exit IP | Probe `ok` | Completed | `ok` Rate | When `no_perm` Triggered |
|---|---|---:|---:|---:|---|
| X (old NY, labeled) | `203.0.113.10` | 1 | 7 | 14% | Refused on 2nd attempt |
| **Z (new NY, clean)** | `203.0.113.30` | **4** | **5** | **80%** | **Refused only on 5th attempt** |

### Conclusion

**Switching to a different IP within the same ISP/city/ASN immediately restores "clean state."**

→ ChatGPT's labeling granularity is **exact IP string**, not ASN / city / ISP.

### IP Lifespan

Each IP can run approximately **4–5 registrations** before flipping to `no_perm` state (probe starts rejecting on 5th attempt). Recovery methods:

- **Change IP**: Immediate recovery (same ASN is fine, as long as IP string differs)
- **Wait a few hours**: Natural recovery (reference observation of `subA1.zone-a.example` temporary 2h recovery)

---

## Debunked Early Assumptions

| Early Assumption | Data | Debunked |
|---|---|---|
| Anti-fraud granularity is "registration email domain" | Five clean subdomains cleared on same day | ❌ It's batch association, not by-domain |
| `*.zone-a.example` five subdomains all stable | All five subdomain batches cleared same day | ❌ These five domains' accounts cleared together, all looked "ok" before banning |
| Same IP + same PayPal + concentrated registration won't trigger anti-fraud (12 consecutive ok) | 24h survival ≈ 2% | ❌ probe=ok is just real-time state |
| Single day 30+ won't trigger new burn | 45 accounts concentrated ban | ❌ Yes it does, just delayed 12 hours |
| `probe=no_perm` is domain-level labeling | Same domain returns opposite results under different IPs | ❌ The dominant signal is IP |

---

## Revised Anti-Fraud Model

### Two Independent Mechanisms```
┌─────────────────────────────────────────────────────┐
│                Probe Layer (Immediate)              │
│                                                     │
│  signal = (egress_ip_string, email_domain_string?)  │
│  evaluation = at request time                       │
│  IP lifetime = ~4-5 registrations                   │
│  domain-level tagging = few domains permanently     │
│                        unavailable                  │
└─────────────────────────────────────────────────────┘
                       ↓
                registration may succeed
                       ↓
┌─────────────────────────────────────────────────────┐
│                Ban Layer (Delayed Batch)            │
│                                                     │
│  signal = (time_window, payment_account,            │
│            egress_ip, fingerprint, ...)             │
│  evaluation = scheduled cron @ ~00:00 / 12:00 UTC  │
│  entire batch correlation → entire batch banned     │
└─────────────────────────────────────────────────────┘
                       ↓
                95% death rate next day at dawn
```# Key Insights

1. **Probe=ok does not mean stability.** It only means the cohort hasn't been reviewed yet
2. **Domain-level tagging is rare** (e.g., `zone-c.example`), with narrower applicability than expected
3. **IP string is the dominant signal at the probe layer**
4. **Batch association is the dominant signal at the ban layer**, which requires reducing dimension sharing within cohorts to improve

---

## Engineering Impact

### What existing tools can manage

- ✅ DomainPool + permanent_burned handle probe-layer "persistent-tagged domains"
- ✅ Webshare API + gost relay handle IP lifespan issues
- ✅ Multi-zone domain pool handle CF quota + zone-level risk

### What existing tools cannot manage

- ❌ **Next-day mass extinction caused by batch association**: daemon mode with target=20 inventory may drop to 0–1 after a single batch BAN, essentially restarting each night
- ❌ Domain rotation / on-demand subdomain provisioning are ineffective against "next-day mass extinction"

### Methods to improve 24-hour survival rate

To significantly reduce batch association (ordered by effectiveness):

1. **Multiple PayPal account pool**: 1–3 orders per account, then rotate and stagger timing. This is the strongest signal
2. **Multiple proxy exit IPs across ISPs / cities**: Different ISPs' actual slots, not just multiple slots from the same proxy vendor
3. **Time dispersion**: 2–3 per day, spread hours apart, not 30+ in one hour
4. **Camoufox fingerprint diversification**: `humanize` + different OS/screen profiles
5. **Lower single PayPal reuse rate**: Reusing the same PayPal for multiple Team subscriptions in short timeframes is itself a strong fraud signal

The current pipeline only does "60–180s jitter staggering" and "multi-domain rotation", **all other dimensions use identical parameters**. This is the root cause of the 2% survival rate.

---

## Directions to be verified

The following have insufficient experimental data; left for future researchers:

1. **Real ROI of Camoufox fingerprint diversification**: Do different OS/screen profiles truly increase cohort distance?
2. **PayPal trust decay curve**: Survival rate trajectory after a PayPal runs N orders
3. **Optimal time dispersion interval**: How much interval between different cohorts is sufficient to be "independent"
4. **Fingerprint reuse vs IP reuse weight**: Hold other dimensions constant, contrastive testing
5. **Existence of a "warm-up" path**: Does performing some "normal user" behavior before banning occur reduce closure probability

---

## Reproduction Guide

To reproduce these experiments:

1. Prepare at least 2 proxy exits with different ASNs
2. Configure daemon mode to run for a week or longer
3. Query daily the `is_banned` status changes in the `gpt_accounts` table via sqlite3
4. Key query:```sql
-- Batch characteristics of accounts banned within 24 hours
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
```5. Control groups are organized by dimensional differences (same PayPal vs different PayPal, same IP vs different IP, etc.)
6. Data anonymization methods refer to this article (RFC 5737 IP, `*.example` domain names)

If you generate new data, feel free to submit a PR to add it to this article following [`CONTRIBUTING.md`](../CONTRIBUTING.md#data-anonymization-checklist-for-research-contributions).

---

## Citing this article

If your research / paper / blog cites data from this article, please cite:```
Gpt-Agreement-Payment — Anti-Fraud Empirical Research. (2026).
https://github.com/DanOps-1/Gpt-Agreement-Payment/blob/main/docs/anti-fraud-research.md
```Or BibTeX:```bibtex
@misc{Gpt-Agreement-Payment-antifraud,
  title  = {Empirical Anti-Fraud Research on ChatGPT Team Subscription},
  author = {Gpt-Agreement-Payment contributors},
  year   = {2026},
  howpublished = {\url{https://github.com/DanOps-1/Gpt-Agreement-Payment}},
  note   = {Licensed under MIT, IP addresses use RFC 5737 placeholders}
}
```
