"""GoPay 自动化 module — adb UI 驱动 emulator 上的 GoPay app 完成 deeplink + PIN 支付。

用途：替代 qris.py 里"等用户扫 QR + 输 PIN"的人工卡点。
前提：emulator 已 root + frida-friendly 不需要（GoPay 自己跑业务，我们只是用户）；
GoPay 主号已登录 + KYC + 余额 ≥ 1 IDR；adb 可达（本地或 ssh tunnel）。

设计假设（基于 2026-05-10 实测，PD2203 mumu 12 emulator，900x1600）：
  - GoPay v2.8.0 是 Flutter app；UI 用 content-desc 不用 resource-id
  - PIN 用自定义数字键盘（不接受 input text；必须 input tap 每个数字）
  - 数字键盘坐标固定（Flutter render 在 900x1600 是 deterministic）
  - deeplink 入口：am start -n com.gojek.gopay/.MainActivity -a VIEW -d <url>
  - "Bayar X Rp" 按钮在 Review pembayaran 页底部
  - PIN 页标题 "Masukkin PIN kamu"

不同分辨率会失败，需要按比例 rescale。先 hardcode 900x1600 上线，后续按需改。
"""
from __future__ import annotations
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional


# ─── 数字键盘坐标（900x1600 实测） ────────────────────
# 列 X：左/中/右
PIN_X = {0: 148, 1: 449, 2: 746}
# 行 Y：keypad 4 行
PIN_Y = [1067, 1218, 1369, 1521]
# 数字 → (col, row)
PIN_DIGIT_POS: dict[str, tuple[int, int]] = {
    "1": (PIN_X[0], PIN_Y[0]),
    "2": (PIN_X[1], PIN_Y[0]),
    "3": (PIN_X[2], PIN_Y[0]),
    "4": (PIN_X[0], PIN_Y[1]),
    "5": (PIN_X[1], PIN_Y[1]),
    "6": (PIN_X[2], PIN_Y[1]),
    "7": (PIN_X[0], PIN_Y[2]),
    "8": (PIN_X[1], PIN_Y[2]),
    "9": (PIN_X[2], PIN_Y[2]),
    "0": (PIN_X[1], PIN_Y[3]),
}
PIN_BACKSPACE = (PIN_X[2], PIN_Y[3])


class GoPayAutoError(Exception):
    pass


class GoPayAuto:
    """adb 驱动 GoPay 自动化操作。

    Args:
      serial: adb device serial (None=用 ANDROID_SERIAL env)
      adb_port: adb-server port (None=用 ANDROID_ADB_SERVER_PORT env)
      log: print-like callback
    """

    PKG = "com.gojek.gopay"
    MAIN_ACT = f"{PKG}/.MainActivity"

    def __init__(self, serial: Optional[str] = None, adb_port: Optional[int] = None, log=print):
        self.serial = serial or os.environ.get("ANDROID_SERIAL", "")
        self.adb_port = adb_port or int(os.environ.get("ANDROID_ADB_SERVER_PORT", 0) or 5037)
        self.log = log

    # ─── adb wrappers ──────────────────────────────────────
    def _adb(self, *args, timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = ["adb"]
        if self.adb_port and self.adb_port != 5037:
            cmd += ["-P", str(self.adb_port)]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def shell(self, cmd: str, timeout: int = 30) -> str:
        r = self._adb("shell", cmd, timeout=timeout)
        return r.stdout

    def tap(self, x: int, y: int, jitter: int = 0) -> None:
        """Tap at (x, y). 加可选 jitter (±N px) 让 tap 不那么机械避反作弊。"""
        if jitter > 0:
            import random as _r
            x += _r.randint(-jitter, jitter)
            y += _r.randint(-jitter, jitter)
        self._adb("shell", "input", "tap", str(x), str(y))

    def tap_humanlike(self, x: int, y: int) -> None:
        """坐标 ±10px 抖动 + 30-120ms 随机延迟。模拟真人触摸。"""
        import random as _r
        self.tap(x, y, jitter=10)
        time.sleep(_r.uniform(0.03, 0.12))

    def keyevent(self, code: str) -> None:
        self._adb("shell", "input", "keyevent", code)

    def screencap(self, save_to: str | Path) -> str:
        """Grab screenshot to local file. Return local path."""
        save_to = str(save_to)
        self._adb("shell", "screencap", "-p", "/sdcard/_gpa.png")
        self._adb("pull", "/sdcard/_gpa.png", save_to)
        return save_to

    def ui_dump(self) -> str:
        """uiautomator dump → return XML string."""
        self._adb("shell", "uiautomator", "dump", "/sdcard/_gpa.xml")
        r = self._adb("shell", "cat", "/sdcard/_gpa.xml")
        return r.stdout or ""

    # ─── high-level helpers ────────────────────────────────
    def current_focus(self) -> str:
        out = self.shell("dumpsys window | grep mCurrentFocus")
        return out.strip()

    def is_gopay_focused(self) -> bool:
        return self.PKG in self.current_focus()

    def open_deeplink(self, deeplink: str) -> None:
        """强制 GoPay 处理 deeplink（不让系统路由到浏览器）。"""
        self.log(f"[gopay-adb] open_deeplink: {deeplink[:120]}")
        # -W 等到 activity 启动完成。注意 deeplink 含 & 要 shell quote。
        # 用 -d 'url' 而不是 input；通过 adb shell 直传 url 时，& 需转义。
        # subprocess args 形式直接传，adb 不走 shell 解析。
        self._adb(
            "shell", "am", "start", "-W",
            "-n", self.MAIN_ACT,
            "-a", "android.intent.action.VIEW",
            "-d", deeplink,
            timeout=30,
        )

    def wait_for_text(
        self, patterns: list[str], timeout_s: float = 30.0, poll_s: float = 1.0
    ) -> Optional[tuple[str, tuple[int, int, int, int]]]:
        """轮询 ui dump，匹配 content-desc 含任一 pattern 的节点。返回 (matched_pattern, bounds)。"""
        result = self.wait_for_groups({"_": patterns}, timeout_s=timeout_s, poll_s=poll_s)
        if not result:
            return None
        _, pat, bounds = result
        return pat, bounds

    def wait_for_groups(
        self,
        groups: dict[str, list[str]],
        timeout_s: float = 30.0,
        poll_s: float = 1.0,
        class_filter: Optional[str] = None,
        exact: bool = False,
    ) -> Optional[tuple[str, str, tuple[int, int, int, int]]]:
        """同时等多组模式，返回第一个匹配的 (group, pattern, bounds)。

        Args:
          class_filter: 节点 class 过滤（如 "android.widget.Button"）。None=不过滤。
          exact: True 时 content-desc 必须精确等于 pattern；False 时允许子串匹配。
        """
        deadline = time.time() + timeout_s
        compiled: list[tuple[str, str, re.Pattern]] = []
        for tag, pats in groups.items():
            for p in pats:
                if exact:
                    rx = re.compile(r'^' + re.escape(p) + r'$', re.IGNORECASE)
                else:
                    rx = re.compile(re.escape(p), re.IGNORECASE)
                compiled.append((tag, p, rx))
        bounds_re = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
        cd_re = re.compile(r'content-desc="([^"]*)"')
        cls_re = re.compile(r'class="([^"]+)"')
        while time.time() < deadline:
            xml = self.ui_dump()
            for m in re.finditer(r'<node[^>]+>', xml):
                node = m.group(0)
                if class_filter:
                    cls_m = cls_re.search(node)
                    if not cls_m or class_filter not in cls_m.group(1):
                        continue
                cd_m = cd_re.search(node)
                if not cd_m:
                    continue
                cd = cd_m.group(1)
                for tag, pat, rgx in compiled:
                    if rgx.search(cd):
                        b_m = bounds_re.search(node)
                        if b_m:
                            x1, y1, x2, y2 = (int(b_m.group(i)) for i in range(1, 5))
                            return tag, pat, (x1, y1, x2, y2)
            time.sleep(poll_s)
        return None

    def tap_bounds_center(self, bounds: tuple[int, int, int, int]) -> None:
        x1, y1, x2, y2 = bounds
        self.tap((x1 + x2) // 2, (y1 + y2) // 2)

    # ─── PIN 输入 ──────────────────────────────────────────
    def enter_pin(self, pin: str, humanize: bool = True) -> None:
        """点击数字键盘输 6 位 PIN。GoPay 自定义键盘不接受 input text。

        humanize=True：每位间随机 250-650ms + ±10px 坐标抖动 + 偶尔 1-2s 停顿
        （模拟真人犹豫）。GoPay 反作弊会检测机械等距 tap，必须人化。
        """
        if not pin or not pin.isdigit() or len(pin) != 6:
            raise GoPayAutoError(f"PIN 必须 6 位数字，给的是 {pin!r}")
        self.log("[gopay-adb] enter_pin: ******")
        import random as _r
        for i, d in enumerate(pin):
            x, y = PIN_DIGIT_POS[d]
            if humanize:
                self.tap(x, y, jitter=10)
                # 第 3-4 位之间偶尔长停（模拟想一下）
                if i in (2, 3) and _r.random() < 0.4:
                    time.sleep(_r.uniform(1.0, 2.0))
                else:
                    time.sleep(_r.uniform(0.25, 0.65))
            else:
                self.tap(x, y)
                time.sleep(0.08)

    def pin_backspace(self, n: int = 1) -> None:
        for _ in range(n):
            self.tap(*PIN_BACKSPACE)
            time.sleep(0.05)

    # ─── 完整支付流程 ───────────────────────────────────────
    def pay_with_deeplink(
        self,
        deeplink: str,
        pin: str,
        max_review_wait_s: float = 20.0,
        max_pin_wait_s: float = 15.0,
        max_result_wait_s: float = 60.0,
        screenshot_dir: Optional[str | Path] = None,
    ) -> dict:
        """全流程：deeplink → Review → Bayar → PIN → 等结果。

        Returns:
            {state: "success"|"failed"|"timeout"|"expired"|"insufficient",
             screenshots: [...], message: ...}
        """
        result: dict = {"state": "unknown", "screenshots": [], "message": ""}
        sd = Path(screenshot_dir) if screenshot_dir else None

        def snap(tag: str) -> None:
            if sd:
                sd.mkdir(parents=True, exist_ok=True)
                p = sd / f"gpa_{int(time.time())}_{tag}.png"
                self.screencap(p)
                result["screenshots"].append(str(p))

        # 1) 打开 deeplink
        self.open_deeplink(deeplink)
        time.sleep(2)
        snap("01_after_deeplink")

        # 2) 等 Review 页：用 class_filter+exact 找真正的 "Bayar" Button（避免标题
        # "Review pembayaran" 子串匹配）；同时并行检测过期 / 余额不足 dialog
        ev = self.wait_for_groups(
            {"ok": ["Bayar", "Lanjut", "Pay"]},
            timeout_s=max_review_wait_s,
            class_filter="android.widget.Button",
            exact=True,
        )
        if not ev:
            # 检查过期 / 错误对话框
            err = self.wait_for_groups(
                {"err": ["Waktu pembayaran habis", "expired", "Saldo tidak cukup",
                         "tidak tersedia", "kadaluarsa"]},
                timeout_s=2,
            )
            if err:
                tag, msg, bounds = err
                state = "expired" if "habis" in msg.lower() or "expired" in msg.lower() else "failed"
                result["state"] = state
                result["message"] = msg
                snap("err_review")
                return result
            result["state"] = "timeout"
            result["message"] = "review 页 Bayar Button 未出现"
            snap("err_no_bayar")
            return result

        _, bayar_text, bayar_bounds = ev
        self.log(f"[gopay-adb] 找到付款按钮: {bayar_text!r} bounds={bayar_bounds}")
        # 反作弊：用户读 review 页通常 2-5s（看金额、商户、确认），不会立刻按
        import random as _r
        time.sleep(_r.uniform(2.0, 5.0))
        x1, y1, x2, y2 = bayar_bounds
        self.tap((x1 + x2) // 2, (y1 + y2) // 2, jitter=15)
        time.sleep(_r.uniform(2.0, 3.0))
        snap("02_after_bayar")

        # 3) 等 PIN 页 / 错误 dialog（同步轮询，第一个匹配的决定路径）
        ev = self.wait_for_groups(
            {
                "pin": ["Masukkin PIN kamu", "Masukkan PIN", "Enter your PIN"],
                "expired": ["Waktu pembayaran habis", "expired", "kadaluarsa"],
                "insufficient": ["Saldo tidak cukup", "saldo kurang", "saldo gak cukup"],
                "blocked": ["transaksi diblokir", "Tidak bisa diproses", "tidak tersedia"],
            },
            timeout_s=max_pin_wait_s,
        )
        if not ev:
            result["state"] = "timeout"
            result["message"] = "PIN 输入页 / 错误 dialog 都没出现"
            snap("err_no_pin_or_dialog")
            return result
        tag, msg, bounds = ev
        if tag != "pin":
            result["state"] = tag
            result["message"] = msg
            snap(f"err_{tag}")
            # 友善关闭 dialog（点 Oke / Kembali）
            ok = self.wait_for_text(["Oke", "Kembali", "Mengerti"], timeout_s=1)
            if ok:
                self.tap_bounds_center(ok[1])
            return result

        # 4) 输 PIN
        self.enter_pin(pin)
        time.sleep(2)
        snap("03_after_pin")

        # 5) 等结果（可能：success / 错 PIN / 网络错 / verify 中）
        deadline = time.time() + max_result_wait_s
        last_snap_t = 0.0
        while time.time() < deadline:
            ok = self.wait_for_text(
                ["Pembayaran berhasil", "berhasil", "Success", "Successful", "Plus active"],
                timeout_s=2,
            )
            if ok:
                result["state"] = "success"
                result["message"] = ok[0]
                snap("04_success")
                return result
            err = self.wait_for_text(
                ["PIN salah", "PIN keliru", "Wrong PIN", "Incorrect", "Coba lagi", "Tidak bisa diproses"],
                timeout_s=2,
            )
            if err:
                result["state"] = "failed"
                result["message"] = err[0]
                snap("err_pin_or_other")
                return result
            # 周期性截图供 debug
            now = time.time()
            if now - last_snap_t > 8:
                snap(f"polling_{int(now - deadline + max_result_wait_s)}")
                last_snap_t = now
            time.sleep(1.5)

        result["state"] = "timeout"
        result["message"] = "等结果超时，可能 GoPay 后端慢或 verify 中"
        snap("err_result_timeout")
        return result


# ─── CLI ───────────────────────────────────────────────────
def _cli():
    import argparse, json
    p = argparse.ArgumentParser(description="GoPay adb 自动化")
    p.add_argument("--deeplink", required=True, help="GoPay App Link")
    p.add_argument("--pin", required=True, help="6-digit PIN")
    p.add_argument("--screenshot-dir", default="/tmp/gopay_adb_shots")
    p.add_argument("--serial", default="")
    p.add_argument("--adb-port", type=int, default=0)
    args = p.parse_args()

    g = GoPayAuto(
        serial=args.serial or None,
        adb_port=args.adb_port or None,
    )
    if not g.is_gopay_focused():
        # GoPay 不在前台无所谓，open_deeplink 会拉起
        pass
    res = g.pay_with_deeplink(
        deeplink=args.deeplink,
        pin=args.pin,
        screenshot_dir=args.screenshot_dir,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
