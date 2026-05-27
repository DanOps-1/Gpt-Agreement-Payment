#!/usr/bin/env python3
"""重复尝试家宽链路，优先命中“直接终态拒卡”。

用途：
- 目标不是做 challenge，而是优先刷出：
  3DS2/authenticate -> setup_intent requires_payment_method -> card_declined/generic_decline
- 一旦检测到进入 Stripe challenge，本脚本会立刻终止本轮并重试下一轮

示例：
    python retry_house_decline.py "https://chatgpt.com/checkout/openai_llc/cs_live_xxx"
    python retry_house_decline.py "cs_live_xxx" --attempts 5
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CARD_PY = ROOT / "card.py"
DEFAULT_CONFIG = ROOT / "config.auto.json"


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _prepare_attempt_config(config: Path, attempt: int) -> Path:
    """按 attempt 生成临时配置，避免每轮 time_on_page 都固定在同一个值。"""
    data = json.loads(config.read_text(encoding="utf-8"))
    behavior = data.setdefault("behavior", {})
    base_min = int(behavior.get("min_time_on_page_ms", 0) or 0)
    if base_min > 0:
        behavior["min_time_on_page_ms"] = base_min * attempt
    fd, tmp_path = tempfile.mkstemp(prefix=f"house-retry-{attempt}-", suffix=".json")
    os.close(fd)
    Path(tmp_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return Path(tmp_path)


def run_attempt(session: str, config: Path, attempt: int) -> tuple[str, list[str], int | None]:
    effective_config = _prepare_attempt_config(config, attempt)
    cmd = [
        sys.executable,
        "-u",
        str(CARD_PY),
        "--config",
        str(effective_config),
        session,
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    print(f"\n{'=' * 72}")
    print(f"[house-retry] attempt {attempt} start")
    print(f"[house-retry] config: {effective_config}")
    print(f"[house-retry] cmd: {' '.join(cmd)}")
    print(f"{'=' * 72}")

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert proc.stdout is not None
    lines: list[str] = []
    result = "unknown"

    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            print(line)

            if "支付已落到终态失败" in line:
                result = "terminal_decline"
                continue

            if "setup_intent 已落到终态失败:" in line:
                result = "terminal_decline"
                continue

            if "检测到 setup_intent confirmation challenge" in line:
                result = "challenge"
                _stop_process(proc)
                break

            if "本地桥接页:" in line:
                result = "challenge"
                _stop_process(proc)
                break

            if "[ERROR]" in line:
                result = "error"
            if "checkout_not_active_session" in line or "This Checkout Session is no longer active." in line:
                result = "session_inactive"

        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _stop_process(proc)
        rc = proc.returncode
        if result == "unknown":
            result = "timeout"
    finally:
        if proc.poll() is None:
            _stop_process(proc)
        try:
            effective_config.unlink(missing_ok=True)
        except Exception:
            pass

    if result == "unknown":
        if rc == 0:
            result = "done"
        else:
            result = "error"

    print(f"[house-retry] attempt {attempt} result={result} rc={rc}")
    return result, lines, rc


def main() -> int:
    parser = argparse.ArgumentParser(description="重复尝试家宽链路，优先命中直接拒卡")
    parser.add_argument("session", help="Checkout Session URL 或 cs_live_xxx")
    parser.add_argument("--attempts", type=int, default=5, help="最多尝试次数，默认 5")
    parser.add_argument("--delay", type=float, default=2.0, help="两轮之间等待秒数，默认 2")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"card.py 配置路径，默认 {DEFAULT_CONFIG}",
    )
    args = parser.parse_args()

    config = Path(args.config).resolve()
    if not config.exists():
        print(f"[house-retry] config 不存在: {config}", file=sys.stderr)
        return 2

    summary: list[tuple[int, str, int | None]] = []
    for attempt in range(1, args.attempts + 1):
        result, _, rc = run_attempt(args.session, config, attempt)
        summary.append((attempt, result, rc))
        if result == "terminal_decline":
            print(f"\n[house-retry] 命中目标：第 {attempt} 轮落到终态拒卡。")
            break
        if result == "session_inactive":
            print(f"\n[house-retry] 当前 Checkout Session 已失活，停止重试。")
            break
        if attempt < args.attempts:
            print(f"[house-retry] {args.delay:.1f}s 后继续下一轮 ...")
            time.sleep(args.delay)

    print("\n[house-retry] summary")
    for attempt, result, rc in summary:
        print(f"  - attempt {attempt}: {result} (rc={rc})")

    return 0 if any(result == "terminal_decline" for _, result, _ in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
