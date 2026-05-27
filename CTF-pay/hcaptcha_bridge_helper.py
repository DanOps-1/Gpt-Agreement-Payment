#!/usr/bin/env python3
"""Headless helper for the local Stripe hCaptcha bridge page.

用途：
- 在无图形环境下打开 `card.py` 输出的本地 bridge URL
- 自动点击 checkbox
- 通过 stdin 接受简单命令，截图 / 点击 / 提交 challenge

示例：
    python hcaptcha_bridge_helper.py http://127.0.0.1:46005/index.html
    TEXT
    SHOT /tmp/bridge.png
    ECLICK 238 145
    VERIFY
    STATE
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

if len(sys.argv) < 2:
    print("usage: hcaptcha_bridge_helper.py <bridge_url>", file=sys.stderr)
    raise SystemExit(2)

BRIDGE_URL = sys.argv[1]


class BridgeHelper:
    def __init__(self, url: str):
        self.url = url
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 960})
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_timeout(3_000)

    def close(self):
        try:
            self.browser.close()
        finally:
            self.playwright.stop()

    def checkbox_frame(self):
        return next((f for f in self.page.frames if "frame=checkbox" in f.url), None)

    def challenge_frame(self):
        return next((f for f in self.page.frames if "frame=challenge" in f.url), None)

    def auto_click_checkbox(self, timeout_ms: int = 15_000) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            cb = self.checkbox_frame()
            if cb:
                for sel in ["#checkbox", '[role="checkbox"]', "div[aria-checked]"]:
                    try:
                        cb.locator(sel).first.click(timeout=1_000)
                        self.page.wait_for_timeout(1_500)
                        return True
                    except Exception:
                        pass
            self.page.wait_for_timeout(300)
        return False

    def _frame_info(self, ch):
        if not ch:
            return {"has_challenge": False}
        return ch.evaluate(
            """() => {
                const q = (sel) => document.querySelector(sel);
                const rect = (el) => {
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {x:r.x, y:r.y, width:r.width, height:r.height};
                };
                const buttons = Array.from(document.querySelectorAll('.button, .button-submit, [aria-label], button')).map((el, i) => ({
                    i,
                    tag: el.tagName,
                    text: (el.innerText || el.getAttribute('aria-label') || '').trim(),
                    cls: el.className || '',
                    rect: rect(el),
                })).filter(x => x.rect && x.rect.width > 0 && x.rect.height > 0);
                return {
                    has_challenge: true,
                    prompt: (q('#prompt-question')?.innerText || '').trim(),
                    error: (q('.error-text')?.innerText || '').trim(),
                    submit_text: (q('.button-submit .text')?.innerText || q('.button-submit')?.innerText || '').trim(),
                    canvas: rect(q('canvas')),
                    example: rect(q('.challenge-example')),
                    buttons,
                };
            }"""
        )

    def _save_canvas(self, ch, path: str):
        if not ch:
            raise RuntimeError('challenge frame not found')
        data = ch.evaluate(
            """() => {
                const canvas = document.querySelector('canvas');
                if (!canvas) return {ok:false, reason:'no canvas'};
                try {
                    return {ok:true, data_url: canvas.toDataURL('image/png')};
                } catch (e) {
                    return {ok:false, reason: String(e)};
                }
            }"""
        )
        if not data.get('ok'):
            raise RuntimeError(data.get('reason') or 'canvas export failed')
        raw = data['data_url'].split(',', 1)[1]
        Path(path).write_bytes(base64.b64decode(raw))

    def run(self):
        self.auto_click_checkbox()
        self.page.wait_for_timeout(2_500)
        print("READY", flush=True)

        for raw in sys.stdin:
            parts = raw.strip().split()
            if not parts:
                continue
            op = parts[0].upper()
            try:
                ch = self.challenge_frame()
                if op == "AUTO":
                    ok = self.auto_click_checkbox()
                    print("AUTO_OK" if ok else "AUTO_FAIL", flush=True)
                elif op == "TEXT":
                    if not ch:
                        print("NO_CHALLENGE", flush=True)
                    else:
                        print(ch.locator("body").inner_text()[:2000], flush=True)
                elif op == "TASKS":
                    if not ch:
                        print("[]", flush=True)
                    else:
                        data = ch.evaluate(
                            """() => Array.from(document.querySelectorAll('.task')).map((el, i) => {
                                const r = el.getBoundingClientRect();
                                return {
                                    i,
                                    aria: el.getAttribute('aria-label') || '',
                                    pressed: el.getAttribute('aria-pressed') || '',
                                    cls: el.className || '',
                                    rect: {x:r.x,y:r.y,width:r.width,height:r.height}
                                };
                            })"""
                        )
                        print(json.dumps(data, ensure_ascii=False), flush=True)
                elif op == "AT":
                    x = float(parts[1])
                    y = float(parts[2])
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    data = ch.evaluate(
                        """([x, y]) => {
                            const els = document.elementsFromPoint(x, y).slice(0, 8).map(el => {
                                const r = el.getBoundingClientRect();
                                return {
                                    tag: el.tagName,
                                    id: el.id || '',
                                    cls: el.className || '',
                                    text: (el.innerText || '').slice(0, 80),
                                    rect: {x:r.x, y:r.y, width:r.width, height:r.height}
                                };
                            });
                            return els;
                        }""",
                        [x, y],
                    )
                    print(json.dumps(data, ensure_ascii=False), flush=True)
                elif op == "ELS":
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    data = ch.evaluate(
                        """() => Array.from(document.querySelectorAll('*')).slice(0, 200).map(el => {
                            const r = el.getBoundingClientRect();
                            return {
                                tag: el.tagName,
                                id: el.id || '',
                                cls: el.className || '',
                                text: (el.innerText || '').slice(0, 40),
                                rect: {x:r.x, y:r.y, width:r.width, height:r.height}
                            };
                        })"""
                    )
                    print(json.dumps(data, ensure_ascii=False), flush=True)
                elif op == "CLICK":
                    idx = int(parts[1])
                    ch.locator(".task").nth(idx).click(timeout=2_000)
                    self.page.wait_for_timeout(1_200)
                    print("CLICK_OK", idx, flush=True)
                elif op == "ECLICK":
                    x = float(parts[1])
                    y = float(parts[2])
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    data = ch.evaluate(
                        """([x, y]) => {
                            const el = document.elementFromPoint(x, y);
                            if (!el) return {ok:false, reason:'no element'};
                            const names = [];
                            let cur = el;
                            while (cur) {
                                names.push((cur.tagName || '') + '#' + (cur.id || '') + '.' + (cur.className || ''));
                                cur = cur.parentElement;
                                if (names.length >= 6) break;
                            }
                            el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true, clientX:x, clientY:y}));
                            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:x, clientY:y}));
                            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:x, clientY:y}));
                            el.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:x, clientY:y}));
                            return {ok:true, chain:names};
                        }""",
                        [x, y],
                    )
                    self.page.wait_for_timeout(1_200)
                    print("ECLICK_OK", json.dumps(data, ensure_ascii=False), flush=True)
                elif op == "PCLICK":
                    x = float(parts[1])
                    y = float(parts[2])
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    frame_el = ch.frame_element()
                    box = frame_el.bounding_box()
                    if not box:
                        raise RuntimeError('challenge iframe not found')
                    sx = box['x'] + x
                    sy = box['y'] + y
                    self.page.mouse.move(sx, sy)
                    self.page.mouse.down()
                    self.page.mouse.up()
                    self.page.wait_for_timeout(1500)
                    print("PCLICK_OK", x, y, flush=True)
                elif op == "DRAG":
                    x1 = float(parts[1])
                    y1 = float(parts[2])
                    x2 = float(parts[3])
                    y2 = float(parts[4])
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    ch.evaluate(
                        """([x1, y1, x2, y2]) => {
                            const el1 = document.elementFromPoint(x1, y1);
                            const el2 = document.elementFromPoint(x2, y2);
                            const mk = (type, x, y) => new MouseEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                clientX: x,
                                clientY: y,
                                buttons: 1,
                            });
                            const target = el1 || document.body;
                            target.dispatchEvent(mk('mouseover', x1, y1));
                            target.dispatchEvent(mk('mousedown', x1, y1));
                            for (let i = 1; i <= 8; i++) {
                                const x = x1 + (x2 - x1) * i / 8;
                                const y = y1 + (y2 - y1) * i / 8;
                                (document.elementFromPoint(x, y) || document.body).dispatchEvent(mk('mousemove', x, y));
                            }
                            (el2 || document.body).dispatchEvent(mk('mouseup', x2, y2));
                            return {ok: true};
                        }""",
                        [x1, y1, x2, y2],
                    )
                    self.page.wait_for_timeout(1_500)
                    print("DRAG_OK", x1, y1, x2, y2, flush=True)
                elif op == "PDRAG":
                    x1 = float(parts[1])
                    y1 = float(parts[2])
                    x2 = float(parts[3])
                    y2 = float(parts[4])
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    frame_el = ch.frame_element()
                    box = frame_el.bounding_box()
                    if not box:
                        raise RuntimeError('challenge iframe not found')
                    sx = box['x'] + x1
                    sy = box['y'] + y1
                    tx = box['x'] + x2
                    ty = box['y'] + y2
                    self.page.mouse.move(sx, sy)
                    self.page.mouse.down()
                    self.page.mouse.move(tx, ty, steps=18)
                    self.page.mouse.up()
                    self.page.wait_for_timeout(1_500)
                    print("PDRAG_OK", x1, y1, x2, y2, flush=True)
                elif op == "PTRDRAG":
                    x1 = float(parts[1])
                    y1 = float(parts[2])
                    x2 = float(parts[3])
                    y2 = float(parts[4])
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    data = ch.evaluate(
                        """([x1, y1, x2, y2]) => {
                            const canvas = document.querySelector('canvas');
                            if (!canvas) return {ok:false, reason:'no canvas'};
                            const mk = (type, x, y, buttons=1) => new PointerEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                composed: true,
                                pointerId: 1,
                                pointerType: 'mouse',
                                isPrimary: true,
                                clientX: x,
                                clientY: y,
                                buttons,
                                pressure: buttons ? 0.5 : 0,
                            });
                            canvas.dispatchEvent(mk('pointerover', x1, y1, 0));
                            canvas.dispatchEvent(mk('pointerenter', x1, y1, 0));
                            canvas.dispatchEvent(mk('pointerdown', x1, y1, 1));
                            for (let i = 1; i <= 18; i++) {
                                const x = x1 + (x2 - x1) * i / 18;
                                const y = y1 + (y2 - y1) * i / 18;
                                canvas.dispatchEvent(mk('pointermove', x, y, 1));
                            }
                            canvas.dispatchEvent(mk('pointerup', x2, y2, 0));
                            return {ok:true};
                        }""",
                        [x1, y1, x2, y2],
                    )
                    self.page.wait_for_timeout(1_500)
                    print("PTRDRAG_OK", json.dumps(data, ensure_ascii=False), flush=True)
                elif op == "CDRAG":
                    x1 = float(parts[1])
                    y1 = float(parts[2])
                    x2 = float(parts[3])
                    y2 = float(parts[4])
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    canvas = ch.locator('canvas').first
                    box = canvas.bounding_box()
                    if not box:
                        raise RuntimeError('canvas not found')
                    canvas.drag_to(
                        canvas,
                        source_position={"x": x1 - box['x'], "y": y1 - box['y']},
                        target_position={"x": x2 - box['x'], "y": y2 - box['y']},
                        force=True,
                        timeout=5000,
                    )
                    self.page.wait_for_timeout(1500)
                    print("CDRAG_OK", x1, y1, x2, y2, flush=True)
                elif op == "REFRESH":
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    ok = False
                    for sel in ["div.refresh.button", "div.refresh-on", "div.refresh-off"]:
                        try:
                            ch.locator(sel).first.click(timeout=1500, force=True)
                            ok = True
                            break
                        except Exception:
                            pass
                    self.page.wait_for_timeout(2500)
                    print("REFRESH_OK" if ok else "REFRESH_FAIL", flush=True)
                elif op == "INFO":
                    print(json.dumps(self._frame_info(ch), ensure_ascii=False), flush=True)
                elif op == "BTNCLICK":
                    label = " ".join(parts[1:]).strip().lower()
                    if not ch:
                        raise RuntimeError("challenge frame not found")
                    data = self._frame_info(ch)
                    target = None
                    for item in data.get('buttons', []):
                        txt = (item.get('text') or '').strip().lower()
                        cls = (item.get('cls') or '').strip().lower()
                        if label and (label in txt or label in cls):
                            target = item
                            break
                    if not target:
                        raise RuntimeError(f'button not found: {label}')
                    r = target['rect']
                    x = r['x'] + r['width'] / 2
                    y = r['y'] + r['height'] / 2
                    ch.evaluate(
                        """([x, y]) => {
                            const el = document.elementFromPoint(x, y);
                            if (!el) return false;
                            el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true, clientX:x, clientY:y}));
                            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:x, clientY:y}));
                            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:x, clientY:y}));
                            el.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:x, clientY:y}));
                            return true;
                        }""",
                        [x, y],
                    )
                    self.page.wait_for_timeout(1500)
                    print("BTNCLICK_OK", json.dumps(target, ensure_ascii=False), flush=True)
                elif op == "VERIFY":
                    selectors = [
                        '[aria-label*="Verify"]',
                        '[aria-label*="Submit"]',
                        '[aria-label*="Next"]',
                        "text=Verify",
                        "text=Submit",
                        "text=Next",
                        ".button-submit",
                    ]
                    done = False
                    if ch:
                        for sel in selectors:
                            try:
                                ch.locator(sel).first.click(timeout=1_200, force=True)
                                done = True
                                break
                            except Exception:
                                pass
                    self.page.wait_for_timeout(2_500)
                    print("VERIFY_OK" if done else "VERIFY_FAIL", flush=True)
                elif op == "CANVAS":
                    path = parts[1]
                    self._save_canvas(ch, path)
                    print("CANVAS_OK", path, flush=True)
                elif op == "SHOT":
                    path = parts[1]
                    self.page.screenshot(path=path, full_page=True)
                    print("SHOT_OK", path, flush=True)
                elif op == "CHSHOT":
                    path = parts[1]
                    if not ch:
                        print("NO_CHALLENGE", flush=True)
                    else:
                        ch.locator("body").screenshot(path=path)
                        print("CHSHOT_OK", path, flush=True)
                elif op == "STATE":
                    result = self.page.evaluate("window.__stripeChallengeResult")
                    cancelled = self.page.evaluate("window.__stripeChallengeCancelled")
                    print(json.dumps({"result": result, "cancelled": cancelled}, ensure_ascii=False), flush=True)
                elif op == "EXIT":
                    break
                else:
                    print("UNKNOWN", op, flush=True)
            except Exception as e:
                print("ERR", type(e).__name__, str(e), flush=True)


def main():
    helper = BridgeHelper(BRIDGE_URL)
    try:
        helper.run()
    finally:
        helper.close()


if __name__ == "__main__":
    main()
