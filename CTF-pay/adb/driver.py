"""GoPay automation module — adb UI driver to complete deeplink + PIN payment on emulator's GoPay app.

Purpose: Replace the manual checkpoint "wait for user to scan QR + enter PIN" in qris.py.
Prerequisites: emulator already rooted + frida-friendly not needed (GoPay runs its own business, we're just the user); GoPay main account logged in + KYC + balance ≥ 1 IDR; adb reachable (local or via ssh tunnel).

Design assumptions (based on 2026-05-10 live testing, PD2203 mumu 12 emulator, 900x1600):
  - GoPay v2.8.0 is a Flutter app; UI uses content-desc not resource-id
  - PIN uses custom digit keyboard (doesn't accept input text; must input tap each digit)
  - Digit keyboard coordinates are fixed (Flutter render on 900x1600 is deterministic)
  - deeplink entry: am start -n com.gojek.gopay/.MainActivity -a VIEW -d <url>
  - "Bayar X Rp" button at bottom of Review pembayaran page
  - PIN page title "Masukkin PIN kamu"

Different resolutions will fail, need to rescale proportionally. Hardcode 900x1600 for now, adjust later as needed."""
from __future__ import annotations
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional


# ─── Digit keyboard coordinates (900x1600 live tested) ────────────────────
# Column X: left/center/right
PIN_X = {0: 148, 1: 449, 2: 746}
# Row Y: keypad 4 rows
PIN_Y = [1067, 1218, 1369, 1521]
# Digit → (col, row)
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
    """adb driver for GoPay automation.

    Args:
      serial: adb device serial (None=use ANDROID_SERIAL env)
      adb_port: adb-server port (None=use ANDROID_ADB_SERVER_PORT env)
      log: print-like callback"""

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
        """Tap at (x, y). Optional jitter (±N px) to make tap less mechanical and avoid anti-cheat."""
        if jitter > 0:
            import random as _r
            x += _r.randint(-jitter, jitter)
            y += _r.randint(-jitter, jitter)
        self._adb("shell", "input", "tap", str(x), str(y))

    def tap_humanlike(self, x: int, y: int) -> None:
        """Coordinates ±10px jitter + 30-120ms random delay. Simulate human touch."""
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
        """Force GoPay to handle deeplink (don't let system route to browser)."""
        self.log(f"[gopay-adb] open_deeplink: {deeplink[:120]}")
        # -W wait until activity starts. Note deeplink contains & needs shell quoting.
        # Use -d 'url' instead of input; when passing url via adb shell, & needs escaping.
        # subprocess args form passed directly, adb doesn't parse through shell.
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
        """Poll ui dump, match nodes whose content-desc contains any pattern. Return (matched_pattern, bounds)."""
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
        """Wait for multiple pattern groups, return first match (group, pattern, bounds).

        Args:
          class_filter: node class filter (e.g. "android.widget.Button"). None=no filter.
          exact: True means content-desc must exactly equal pattern; False allows substring match."""
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

    def _read_pin_keypad_coords(self) -> dict[str, tuple[int, int]]:
        """Dynamically read PIN keyboard button center coordinates from UI dump. content-desc is digit string.
        cloudphone#1 (900x1600) and #2 (1080x1920) have different resolutions, hardcoded coordinates will error."""
        xml = self.ui_dump()
        import re
        coords: dict[str, tuple[int, int]] = {}
        node_re = re.compile(r'<node[^>]+content-desc="([^"]*)"[^>]+bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
        for m in node_re.finditer(xml):
            cd, x1, y1, x2, y2 = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            if cd in "0123456789" and len(cd) == 1:
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                coords[cd] = (cx, cy)
        return coords

    # ─── PIN input ──────────────────────────────────────────
    def enter_pin(self, pin: str, humanize: bool = True) -> None:
        """Tap digit keyboard to enter 6-digit PIN. GoPay custom keyboard doesn't accept input text.

        humanize=True: random 250-650ms between each digit + ±10px coordinate jitter + occasional 1-2s pause
        (simulate human hesitation). GoPay anti-cheat detects mechanical equidistant taps, must humanize."""
        if not pin or not pin.isdigit() or len(pin) != 6:
            raise GoPayAutoError(f"PIN 必须 6 位数字，给的是 {pin!r}")
        # Dynamically read keypad coordinates — hardcoded wrong for 1080x1920
        try:
            dynamic = self._read_pin_keypad_coords()
        except Exception:
            dynamic = {}
        # Must have all 10 digits, otherwise don't start (prevent partial input errors locking account)
        if len(dynamic) != 10 or not all(d in dynamic for d in "0123456789"):
            raise GoPayAutoError(
                f"PIN keypad UI dump 不完整 ({len(dynamic)}/10 digit 找到), 拒绝盲点击防锁号"
            )
        self.log(f"[gopay-adb] enter_pin: ****** (dynamic coords {len(dynamic)} digits)")
        import random as _r
        for i, d in enumerate(pin):
            x, y = dynamic[d]
            if humanize:
                self.tap(x, y, jitter=10)
                # Occasional long pause between digits 3-4 (simulate human hesitation)
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

    # ─── Complete payment flow ───────────────────────────────────────
    def pay_with_deeplink(
        self,
        deeplink: str,
        pin: str,
        max_review_wait_s: float = 20.0,
        max_pin_wait_s: float = 15.0,
        max_result_wait_s: float = 60.0,
        screenshot_dir: Optional[str | Path] = None,
    ) -> dict:
        """Full flow: deeplink → Review → Bayar → PIN → wait result.

        Returns:
            {state: "success"|"failed"|"timeout"|"expired"|"insufficient",
             screenshots: [...], message: ...}"""
        result: dict = {"state": "unknown", "screenshots": [], "message": ""}
        sd = Path(screenshot_dir) if screenshot_dir else None

        def snap(tag: str) -> None:
            if sd:
                sd.mkdir(parents=True, exist_ok=True)
                p = sd / f"gpa_{int(time.time())}_{tag}.png"
                self.screencap(p)
                result["screenshots"].append(str(p))

        # 1) Open deeplink
        self.open_deeplink(deeplink)
        time.sleep(2)
        snap("01_after_deeplink")

        # 2) Wait Review page: use class_filter+exact to find actual "Bayar" Button (avoid title
        # "Review pembayaran" substring match); parallel detect expiry/insufficient balance dialog
        ev = self.wait_for_groups(
            {"ok": ["Bayar", "Lanjut", "Pay"]},
            timeout_s=max_review_wait_s,
            class_filter="android.widget.Button",
            exact=True,
        )
        if not ev:
            # Check expiry/error dialogs
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
        # Anti-cheat: users typically read review page 2-5s (check amount, merchant, confirm), won't tap immediately
        import random as _r
        time.sleep(_r.uniform(2.0, 5.0))
        x1, y1, x2, y2 = bayar_bounds
        self.tap((x1 + x2) // 2, (y1 + y2) // 2, jitter=15)
        time.sleep(_r.uniform(2.0, 3.0))
        snap("02_after_bayar")

        # 3) Wait PIN page/error dialog (sync poll, first match decides path)
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
            # Gracefully close dialog (tap Oke/Kembali)
            ok = self.wait_for_text(["Oke", "Kembali", "Mengerti"], timeout_s=1)
            if ok:
                self.tap_bounds_center(ok[1])
            return result

        # 4) Enter PIN
        self.enter_pin(pin)
        time.sleep(2)
        snap("03_after_pin")

        # 5) Wait result (possible: success/wrong PIN/network error/verify in progress)
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
            # Periodic screenshots for debug
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
        # GoPay not in foreground is fine, open_deeplink will bring it up
        pass
    res = g.pay_with_deeplink(
        deeplink=args.deeplink,
        pin=args.pin,
        screenshot_dir=args.screenshot_dir,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
