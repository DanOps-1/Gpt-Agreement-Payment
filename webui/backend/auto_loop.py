"""Auto-loop runner: keeps spawning register+gopay-pay iterations until target
success count is reached or max consecutive failures.

Stop conditions:
  - cumulative success_count >= target_success
  - consecutive_fail >= max_consec_fail
  - user calls stop()
  - Webshare quota exhausted (we tried to rotate IP and got
    WebshareQuotaExhausted)

Per-iteration error classification + remediation (scanned from tail of pipeline
log after each iteration):

  | kind                 | remediation                                          |
  |----------------------|------------------------------------------------------|
  | success              | success_count += 1, reset consecutive_fail           |
  | cf_429               | _rotate_webshare_ip → continue (counts as fail)      |
  | otp_timeout          | continue                                             |
  | linked_exhausted     | runner._drain already auto-marks linked → continue   |
  | wallet_insufficient  | continue (manual top-up needed; out of our scope)    |
  | coupon_ineligible    | scrap (delete from inventory) the email → continue   |
  | register_failed      | continue                                             |
  | unknown              | continue (counts as fail)                            |

Every iteration runs runner.start(mode='single', gopay=True, ...) — i.e. one
register + one gopay charge. Pre-flight checks (link_state, coupon eligibility)
already in place from earlier patches.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

from . import link_state, runner
from .db import get_db

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop_flag = False
_state = {
    "running": False,
    "started_at": None,
    "ended_at": None,
    "target_success": 0,
    "max_consec_fail": 0,
    "iteration": 0,
    "success_count": 0,
    "fail_count": 0,
    "consecutive_fail": 0,
    "last_kind": "",
    "last_action": "",
    "last_email": "",
    "stop_reason": "",
    "ip_rotations": 0,
    "scrap_marked": [],   # list of {email, kind, ts}
    "mode_args": {},
    # ── 多 zone 域名轮换（参考 daemon 实现） ──
    "zone_list": [],            # 来自 cardw mail.catch_all_domains
    "zone_idx": 0,
    "current_zone": "",
    "zone_reg_fail_streak": 0,
    "zone_ip_rotations": 0,
    "total_zone_rotations": 0,
    "zone_rotate_on_reg_fails": 3,    # 注册连挂 N 次切 zone（默认 3）
    "zone_rotate_after_ip_rotations": 2,  # 当前 zone 内 IP 轮换 N 次也切 zone（默认 2）
}


_KIND_PATTERNS = [
    ("proxy_dead",          re.compile(
        r"curl: \((7|28|52|56|97)\)|"
        r"Proxy Authentication Required|"
        r"\b407\b.*Proxy|"
        r"cannot complete SOCKS5|"
        r"SOCKS5.*Network unreachable",
        re.I,
    )),
    ("cf_429",              re.compile(r"midtrans linking unexpected status=429")),
    ("already_paid",        re.compile(r'User is already paid', re.I)),
    ("otp_validate_400",    re.compile(r"_gopay_validate_otp.*\n.*HTTPError|HTTP Error 400.*\n.*_gopay_validate_otp", re.S)),
    ("otp_timeout",         re.compile(r"OTPCancelled|OTP timeout after")),
    ("linked_exhausted",    re.compile(r"midtrans linking exhausted retries")),
    ("wallet_insufficient", re.compile(r'"code"\s*:\s*"201"|INSUFFICIENT_BALANCE|createAuth call to payment-switch failed')),
    ("coupon_ineligible",   re.compile(r"promo coupon.*state=not_eligible|coupon.*not_eligible|state=not_eligible.*promo")),
    ("register_failed",     re.compile(r"RegistrationError")),
]

_ACCOUNT_LOG_RES = [
    re.compile(r"\[pay-only\]\s+复用最近未支付注册账号:\s*([\w.+-]+@[\w.-]+\.[\w]+)"),
    re.compile(r"\[reg\][^\n]*邮箱已创建:\s*([\w.+-]+@[\w.-]+\.[\w]+)"),
    re.compile(r"\[fresh\]\s+当前账号:\s*([\w.+-]+@[\w.-]+\.[\w]+)"),
]


def _classify(tail_lines: list[str]) -> str:
    text = "\n".join(tail_lines)
    for kind, rgx in _KIND_PATTERNS:
        if rgx.search(text):
            return kind
    return "unknown"


def _extract_email(tail_lines: list[str]) -> str:
    """取 tail 里最后（最新）一条匹配的 email。

    auto-loop 跨 iter 保留 log buffer 时，buffer 头部可能还有上一轮甚至更早的
    email；用 `re.search` 拿到的会是最早一条，跟当前 iter 不符。改用
    `findall` 取末尾匹配。"""
    text = "\n".join(tail_lines)
    for rgx in _ACCOUNT_LOG_RES:
        matches = rgx.findall(text)
        if matches:
            last = matches[-1]
            # findall returns the group string when there's exactly one capture
            return last if isinstance(last, str) else last[0] if last else ""
    return ""


def _scrap_account(email: str) -> bool:
    """Delete the most recent registered_accounts row matching `email`."""
    if not email:
        return False
    db = get_db()
    try:
        with db._conn() as c:
            row = c.execute(
                "SELECT id FROM registered_accounts WHERE email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (email,),
            ).fetchone()
        if not row:
            return False
        n = db.delete_registered_accounts([int(row["id"])])
        return n > 0
    except Exception as e:
        logger.warning(f"scrap account {email} failed: {e}")
        return False


def _rotate_ip() -> tuple[bool, str]:
    """Trigger Webshare IP rotation. Returns (ok, message)."""
    import json
    import sys
    from pathlib import Path
    from . import settings as s

    sys.path.insert(0, str(s.ROOT))
    try:
        from pipeline import _rotate_webshare_ip, WebshareQuotaExhausted  # type: ignore
    except Exception as e:
        return False, f"pipeline import 失败: {e}"
    finally:
        try:
            sys.path.remove(str(s.ROOT))
        except ValueError:
            pass

    try:
        cfg = json.loads(Path(s.PAY_CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"读 pay config 失败: {e}"
    if not (cfg.get("webshare") or {}).get("enabled"):
        return False, "webshare 未启用"

    try:
        new_px = _rotate_webshare_ip(cfg, team_client=None, prev_ip="")
        return True, f"new ip {new_px.get('proxy_address')} ({new_px.get('country_code')})"
    except WebshareQuotaExhausted as e:
        return False, f"quota exhausted: {e}"
    except Exception as e:
        return False, f"rotate failed: {e}"


def _loop_body(*, target_success: int, max_consec_fail: int, mode_args: dict):
    global _stop_flag

    poll_interval = 2.0
    inter_iter_sleep = 5.0

    while not _stop_flag:
        with _lock:
            if _state["success_count"] >= target_success:
                _state["stop_reason"] = f"target {target_success} reached"
                break
            if _state["consecutive_fail"] >= max_consec_fail:
                _state["stop_reason"] = f"consecutive fail ≥ {max_consec_fail}"
                break
            _state["iteration"] += 1
            iter_no = _state["iteration"]

        msg_start = (
            f"[auto-loop] ===== iter {iter_no} starting "
            f"(累计 success={_state['success_count']}/{target_success} "
            f"fail={_state['fail_count']} 连续fail={_state['consecutive_fail']}/{max_consec_fail}) ====="
        )
        logger.info(msg_start)
        runner.append_log(msg_start)

        # 清掉 wa_state.latest 防止上一轮残留 OTP（手动模态框留下的）被
        # 这一轮的 gopay polling 误吃 → GoPay validate-otp 400。
        try:
            get_db().delete_runtime_key("wa_state")
            runner.append_log("[auto-loop] cleared wa_state.latest (避免 stale OTP)")
        except Exception as e:
            runner.append_log(f"[auto-loop] 清 wa_state 失败: {e}")

        # 自动 unlink 上一轮成功后被 mark linked 的 phone：runner.start 的预检
        # 看到 linked 状态会 409 拒绝，连续多 iter 自我锁死。auto-loop 全权管理
        # 自己的状态，每轮开始前都先把当前配置 phone 翻为 unlinked。
        try:
            from . import settings as _s
            import json as _json
            _cfg = _json.loads(open(_s.PAY_CONFIG_PATH, encoding="utf-8").read())
            _phone = link_state.phone_from_gopay_config(_cfg)
            if _phone and link_state.is_linked(_phone):
                link_state.mark_unlinked(_phone, source="auto_loop_pre_iter")
                runner.append_log(f"[auto-loop] auto-unlinked {_phone} (跨 iter 解锁)")
        except Exception as e:
            runner.append_log(f"[auto-loop] auto-unlink 失败: {e}")

        # 注入跳过【注册阶段】废物逻辑的环境变量：
        # - 注册阶段 Codex RT 交换 / token brute-force exchange (12 mode 401 全失败)
        # 支付阶段的 [RT] 流程**保留**：支付成功后必须走 RT 拿 refresh_token，
        # 否则 CPA 只能用 access_token 裸导（access_token 一过期号就废了）。
        iter_env = {
            "OAUTH_CODEX_RT_EXCHANGE": "0",
            "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
            "SKIP_SIGNUP_CODEX_RT": "1",
            "SKIP_OAUTH_TOKEN_EXCHANGE": "1",
        }

        # 多 zone 域名轮换：当前 zone 写到 WEBUI_FORCE_ZONE，pipeline DomainPool
        # 读到后会过滤池只留这一个 zone 的域。
        with _lock:
            cur_zone = _state.get("current_zone", "")
        if cur_zone:
            iter_env["WEBUI_FORCE_ZONE"] = cur_zone
            runner.append_log(f"[auto-loop] zone={cur_zone}")

        # Spawn one pipeline iteration. preserve_log keeps prior auto-loop
        # markers visible across iterations.
        try:
            runner.preserve_log_on_next_start()
            runner.start(**mode_args, env_overrides=iter_env)
        except RuntimeError as e:
            with _lock:
                _state["fail_count"] += 1
                _state["consecutive_fail"] += 1
                _state["last_kind"] = "spawn_failed"
                _state["last_action"] = f"spawn failed: {str(e)[:160]}"
            time.sleep(inter_iter_sleep)
            continue

        # Wait for pipeline to finish
        while True:
            if _stop_flag:
                try:
                    runner.stop()
                except Exception:
                    pass
                break
            st = runner.status()
            if not st.get("running"):
                break
            time.sleep(poll_interval)

        if _stop_flag:
            break

        # Inspect tail
        st = runner.status()
        exit_code = st.get("exit_code")
        tail_entries = runner.get_tail(300)
        tail_lines = [e.get("line", "") for e in tail_entries]
        email = _extract_email(tail_lines)
        kind = _classify(tail_lines) if exit_code != 0 else "success"

        with _lock:
            _state["last_email"] = email
            _state["last_kind"] = kind

        if kind == "success":
            with _lock:
                _state["success_count"] += 1
                _state["consecutive_fail"] = 0
                _state["zone_reg_fail_streak"] = 0
                _state["zone_ip_rotations"] = 0
                _state["last_action"] = f"success ({email or '?'})"
        elif kind == "already_paid":
            # 命中已付费账号 — 不算失败（不增 fail / consecutive_fail），
            # card.py 的预记录已把这个 email 写进 card_results，下一轮
            # _paid_or_consumed_emails() 会过滤掉它。
            with _lock:
                _state["last_action"] = f"已付费账号被选中 ({email or '?'})，已标记跳过下次"
            time.sleep(inter_iter_sleep)
            continue
        else:
            with _lock:
                _state["fail_count"] += 1
                _state["consecutive_fail"] += 1
                if kind == "register_failed":
                    _state["zone_reg_fail_streak"] += 1

            action = ""
            if kind in ("cf_429", "proxy_dead"):
                ok, msg = _rotate_ip()
                with _lock:
                    if ok:
                        _state["ip_rotations"] += 1
                        _state["zone_ip_rotations"] += 1
                action = f"{kind} → rotate {'OK' if ok else 'FAIL'}: {msg}"
                if not ok and "quota" in msg.lower():
                    with _lock:
                        _state["stop_reason"] = "webshare quota exhausted"
                    _stop_flag = True
            elif kind == "coupon_ineligible":
                if email and _scrap_account(email):
                    action = f"scrapped {email} (promo not_eligible)"
                    with _lock:
                        _state["scrap_marked"].append({
                            "email": email, "kind": kind, "ts": time.time(),
                        })
                else:
                    action = f"could not scrap (email={email!r})"
            elif kind == "linked_exhausted":
                action = "phone auto-marked linked by runner._drain (next iter pre-check 409 → 跳过该号)"
            elif kind == "wallet_insufficient":
                action = "GoPay wallet 余额不足，跳过该号"
            elif kind == "otp_validate_400":
                action = "GoPay 收到错误 OTP（可能是 wa_state 旧码污染或手动输错），跳过"
            elif kind == "otp_timeout":
                action = "OTP 超时，跳过"
            elif kind == "register_failed":
                action = "注册失败，跳过"
            else:
                action = f"unknown error: {tail_lines[-1] if tail_lines else ''}"[:200]

            with _lock:
                _state["last_action"] = action
            logger.info(f"[auto-loop] iter {iter_no} {kind} → {action}")

        # 多 zone 轮换检查：reg_fail_streak 或 zone_ip_rotations 达阈值就切下一个 zone
        with _lock:
            zlist = list(_state.get("zone_list") or [])
            if len(zlist) > 1:
                rfs = _state.get("zone_reg_fail_streak", 0)
                zir = _state.get("zone_ip_rotations", 0)
                rfs_th = _state.get("zone_rotate_on_reg_fails", 3)
                zir_th = _state.get("zone_rotate_after_ip_rotations", 2)
                if rfs >= rfs_th or zir >= zir_th:
                    cur_idx = _state.get("zone_idx", 0)
                    next_idx = (cur_idx + 1) % len(zlist)
                    old_zone = _state.get("current_zone", "")
                    new_zone = zlist[next_idx]
                    _state["zone_idx"] = next_idx
                    _state["current_zone"] = new_zone
                    _state["zone_reg_fail_streak"] = 0
                    _state["zone_ip_rotations"] = 0
                    _state["total_zone_rotations"] = _state.get("total_zone_rotations", 0) + 1
                    reason = (
                        f"reg_fail_streak={rfs}≥{rfs_th}"
                        if rfs >= rfs_th
                        else f"zone_ip_rotations={zir}≥{zir_th}"
                    )
                    runner.append_log(
                        f"[auto-loop] 🔀 zone {old_zone} → {new_zone} ({reason}; "
                        f"累计 zone 轮换={_state['total_zone_rotations']})"
                    )

        msg_end = f"[auto-loop] ===== iter {iter_no} ended kind={kind} action={_state.get('last_action','')[:200]} ====="
        runner.append_log(msg_end)
        time.sleep(inter_iter_sleep)

    with _lock:
        _state["running"] = False
        _state["ended_at"] = time.time()
        if not _state.get("stop_reason"):
            _state["stop_reason"] = "stopped"
    logger.info(f"[auto-loop] ended: {_state.get('stop_reason')}")


def _load_zone_list_from_cardw() -> list:
    """从 CTF-reg/config.paypal-proxy.json 读 mail.catch_all_domains。"""
    import json
    from pathlib import Path
    from . import settings as s

    cardw_path = Path(s.ROOT) / "CTF-reg" / "config.paypal-proxy.json"
    try:
        data = json.loads(cardw_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    mail_cfg = data.get("mail") or {}
    lst = mail_cfg.get("catch_all_domains") or []
    if not isinstance(lst, list):
        return []
    out = []
    for d in lst:
        if isinstance(d, str) and d.strip():
            out.append(d.strip())
    return out


def start(
    *,
    target_success: int,
    max_consec_fail: int = 5,
    paypal: bool = False,
    gopay: bool = True,
    pay_only: bool = False,
    register_only: bool = False,
    register_mode: str = "browser",
    zone_rotate_on_reg_fails: int = 3,
    zone_rotate_after_ip_rotations: int = 2,
) -> dict:
    global _thread, _stop_flag

    if target_success < 1:
        raise ValueError("target_success must be >= 1")
    if max_consec_fail < 1:
        raise ValueError("max_consec_fail must be >= 1")

    with _lock:
        if _state["running"]:
            raise RuntimeError("auto-loop 已在运行")

    if runner.status().get("running"):
        raise RuntimeError("普通 pipeline 正在运行，先停掉再启 auto-loop")

    mode_args = {
        "mode": "single",
        "paypal": paypal,
        "gopay": gopay,
        "pay_only": pay_only,
        "register_only": register_only,
        "batch": 0,
        "workers": 3,
        "self_dealer": 0,
        "count": 0,
        "register_mode": register_mode,
    }

    zone_list = _load_zone_list_from_cardw()

    with _lock:
        _stop_flag = False
        _state.update({
            "running": True,
            "started_at": time.time(),
            "ended_at": None,
            "target_success": target_success,
            "max_consec_fail": max_consec_fail,
            "iteration": 0,
            "success_count": 0,
            "fail_count": 0,
            "consecutive_fail": 0,
            "last_kind": "",
            "last_action": "",
            "last_email": "",
            "stop_reason": "",
            "ip_rotations": 0,
            "scrap_marked": [],
            "mode_args": mode_args,
            "zone_list": zone_list,
            "zone_idx": 0,
            "current_zone": zone_list[0] if zone_list else "",
            "zone_reg_fail_streak": 0,
            "zone_ip_rotations": 0,
            "total_zone_rotations": 0,
            "zone_rotate_on_reg_fails": zone_rotate_on_reg_fails,
            "zone_rotate_after_ip_rotations": zone_rotate_after_ip_rotations,
        })

    _thread = threading.Thread(
        target=_loop_body,
        kwargs={
            "target_success": target_success,
            "max_consec_fail": max_consec_fail,
            "mode_args": mode_args,
        },
        daemon=True,
    )
    _thread.start()
    return status()


def stop() -> dict:
    global _stop_flag
    _stop_flag = True
    return status()


def status() -> dict:
    with _lock:
        return {**_state}
