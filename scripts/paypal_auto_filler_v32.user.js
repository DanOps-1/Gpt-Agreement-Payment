// ==UserScript==
// @name         PayPal Auto Filler
// @namespace    http://tampermonkey.net/
// @version      32.0
// @description  Auto-fill PayPal/OpenAI checkout pages
// @match        https://www.paypal.com/*
// @match        https://pay.openai.com/*
// @match        https://checkout.stripe.com/*
// @grant        GM_xmlhttpRequest
// @connect      meiguodizhi.com
// @connect      a.62-us.com
// @run-at       document-idle
// ==/UserScript==

// ========== 配置 ==========
var CONFIG = {
    phone: 'PHONE_REDACTED',
    cardNumber: 'CARD_REDACTED',
    cardExpiry: '03 / 30',
    cardCvv: '996',
    smsKey: 'SMS_KEY_REDACTED'
};
// ========================

(function() {
    'use strict';
    var log = function(s) { console.log('[PP] ' + s); };

    var st = document.createElement('style');
    st.textContent = '#captcha-standalone,.captcha-overlay,.captcha-container,.AddressAutocomplete-results,[class*="AddressAutocomplete-results"]{display:none!important;height:0!important;overflow:hidden!important}';
    document.head.appendChild(st);

    var randEmail = function() {
        var c = 'abcdefghijklmnopqrstuvwxyz0123456789', e = '';
        for (var i = 0; i < 16; i++) e += c[Math.floor(Math.random() * c.length)];
        return e + '@gmail.com';
    };

    var randPass = function() {
        var L = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
        var D = '0123456789', S = '!@#$%^', A = L + D + S;
        var p = L[Math.floor(Math.random()*26)] + L[26+Math.floor(Math.random()*26)] + D[Math.floor(Math.random()*10)] + S[Math.floor(Math.random()*6)];
        for (var i = 4; i < 14; i++) p += A[Math.floor(Math.random()*A.length)];
        return p.split('').sort(function(){ return Math.random()-0.5; }).join('');
    };

    var fill = function(id, val) {
        var el = document.getElementById(id);
        if (!el) { log('NOT FOUND: ' + id); return; }
        var ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        ns.call(el, val);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        log(id + ' = ' + el.value);
    };

    var fillSel = function(sel, val) {
        var el = document.querySelector(sel);
        if (!el) { log('NOT FOUND: ' + sel); return; }
        var ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        ns.call(el, val);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        log(sel + ' = ' + el.value);
    };

    var fillSelect = function(id, text) {
        var el = document.getElementById(id);
        if (!el) { log('NOT FOUND: ' + id); return; }
        for (var i = 0; i < el.options.length; i++) {
            if (el.options[i].text.toLowerCase().includes(text.toLowerCase()) || el.options[i].value.toLowerCase().includes(text.toLowerCase())) {
                el.value = el.options[i].value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                log(id + ' = ' + el.options[i].text);
                return;
            }
        }
    };

    var getAddr = function(cb) {
        log('Fetching address from meiguodizhi.com...');
        GM_xmlhttpRequest({
            method: 'POST',
            url: 'https://www.meiguodizhi.com/api/v1/dz',
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ path: '/', method: 'address' }),
            onload: function(r) {
                try {
                    var d = JSON.parse(r.responseText);
                    var a = d.address || d;
                    var addr = {
                        street: a.Address || a.street || '123 Main St',
                        city: a.City || a.city || 'New York',
                        state: a.State_Full || a.State || a.state || 'New York',
                        zip: (a.Zip_Code || a.zip || '10001').substring(0, 5)
                    };
                    log('Address: ' + JSON.stringify(addr));
                    cb(addr);
                } catch(e) {
                    log('Parse error: ' + e.message);
                    cb({ street:'123 Main St', city:'New York', state:'New York', zip:'10001' });
                }
            },
            onerror: function(e) {
                log('Addr req fail: ' + (e.statusText || 'network error'));
                cb({ street:'123 Main St', city:'New York', state:'New York', zip:'10001' });
            }
        });
    };

    var getOTP = function(cb, attempts) {
        attempts = attempts || 0;
        log('Fetching OTP (attempt ' + (attempts+1) + ')');
        GM_xmlhttpRequest({
            method: 'GET',
            url: 'https://a.62-us.com/api/get_sms?key=' + CONFIG.smsKey,
            onload: function(r) {
                var t = (r.responseText || '').trim();
                log('SMS: ' + t.slice(0, 160));
                if (t.indexOf('yes|') === 0) {
                    var m = t.match(/PayPal[^0-9]*(\d{6})/i) || t.match(/\b(\d{6})\b/);
                    if (m) { log('OTP code: ' + m[1]); cb(m[1]); return; }
                }
                if (attempts < 40) setTimeout(function(){ getOTP(cb, attempts+1); }, 3000);
                else log('OTP timeout');
            },
            onerror: function(e) {
                log('SMS req fail: ' + (e && e.statusText));
                if (attempts < 40) setTimeout(function(){ getOTP(cb, attempts+1); }, 3000);
            }
        });
    };

    var findOTPGridIn = function(root) {
        try {
            // 1) 标准 selector：单 input
            var single = root.querySelector('input[autocomplete="one-time-code"]')
                      || root.querySelector('input[name="otp"]')
                      || root.querySelector('input[name="code"]')
                      || root.querySelector('input[type="tel"][maxlength="6"]');
            if (single) return { type: 'single', inputs: [single] };

            // 2) 标准 selector：maxlength=1 的 6 格
            var ml1 = root.querySelectorAll('input[maxlength="1"]');
            if (ml1.length >= 6) return { type: 'grid-ml1', inputs: Array.from(ml1).slice(0, 6) };

            // 3) 文本 "Enter your code" 定位 modal 容器，找 modal 内的 input
            var allEls = root.querySelectorAll('h1, h2, h3, h4, div, p, span, label');
            for (var i = 0; i < allEls.length; i++) {
                var t = (allEls[i].textContent || '').replace(/\s+/g, ' ').trim();
                if (/^enter your code$/i.test(t) || /enter.{1,15}code|6.?digit code|security code|验证码/i.test(t)) {
                    var p = allEls[i];
                    for (var d = 0; d < 10 && p; d++) {
                        var ins = p.querySelectorAll('input');
                        if (ins.length >= 6) {
                            var visible = Array.from(ins).filter(function(inp){ return inp.offsetParent !== null; });
                            // 优先取尺寸小的（OTP 格子通常是 30-80px 宽）
                            var small = visible.filter(function(inp){
                                var r = inp.getBoundingClientRect();
                                return r.width >= 10 && r.width <= 100 && r.height >= 10 && r.height <= 100;
                            });
                            if (small.length >= 6) return { type: 'modal-small', inputs: small.slice(0, 6) };
                            if (visible.length >= 6) return { type: 'modal-any', inputs: visible.slice(0, 6) };
                        }
                        p = p.parentElement;
                    }
                }
            }
        } catch(e) {}
        return null;
    };

    var fillOTPIn = function(root, code) {
        var found = findOTPGridIn(root);
        if (!found) return null;
        var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        if (found.type === 'single') {
            var s = found.inputs[0];
            s.focus();
            setter.call(s, code);
            s.dispatchEvent(new Event('input', { bubbles: true }));
            s.dispatchEvent(new Event('change', { bubbles: true }));
            return found.type;
        }
        for (var i = 0; i < 6; i++) {
            var el = found.inputs[i];
            el.focus();
            setter.call(el, code[i]);
            el.dispatchEvent(new KeyboardEvent('keydown', { key: code[i], bubbles: true }));
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: code[i], inputType: 'insertText' }));
            el.dispatchEvent(new KeyboardEvent('keyup', { key: code[i], bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        found.inputs[5].blur();
        return found.type;
    };

    var fillOTP = function(code) {
        var res = fillOTPIn(document, code);
        if (res) { log('OTP filled in main doc (' + res + '): ' + code); return true; }
        var ifr = document.querySelectorAll('iframe');
        for (var i = 0; i < ifr.length; i++) {
            try {
                var fdoc = ifr[i].contentDocument;
                if (fdoc) {
                    var r = fillOTPIn(fdoc, code);
                    if (r) { log('OTP filled in iframe[' + i + '] (' + r + '): ' + code); return true; }
                }
            } catch(e) {}
        }
        log('OTP input not found anywhere');
        return false;
    };

    var hasOTPIn = function(root) {
        return !!findOTPGridIn(root);
    };

    var watchOTP = function(retries) {
        retries = retries || 0;
        var found = hasOTPIn(document);
        var ifr = Array.from(document.querySelectorAll('iframe'));
        if (!found) {
            for (var i = 0; i < ifr.length; i++) {
                try { if (ifr[i].contentDocument && hasOTPIn(ifr[i].contentDocument)) { found = true; break; } } catch(e) {}
            }
        }
        if ((retries % 4) === 0) {
            log('OTP watch #' + retries + ': mainGrid=' + document.querySelectorAll('input[maxlength="1"]').length + ' ifr=' + ifr.length + ' found=' + found);
            for (var f = 0; f < ifr.length; f++) {
                var fr = ifr[f];
                var info = (fr.id||'_') + '/name=' + (fr.name||'_') + '/src=' + (fr.src||'_').slice(-70);
                try {
                    var fd = fr.contentDocument;
                    if (fd) info += ' SAME inputs=' + fd.querySelectorAll('input').length + ' ml1=' + fd.querySelectorAll('input[maxlength="1"]').length;
                    else info += ' XORIGIN';
                } catch(e) { info += ' XORIGIN'; }
                log('iframe[' + f + ']: ' + info);
            }
            var visIn = Array.from(document.querySelectorAll('input')).filter(function(inp){ return inp.offsetParent !== null; });
            log('main visible inputs(' + visIn.length + '): ' + visIn.map(function(inp){
                return (inp.name||'_')+'|id='+(inp.id||'_')+'|t='+inp.type+'|ml='+inp.maxLength+'|ac='+(inp.autocomplete||'_')+'|im='+(inp.inputMode||'_');
            }).join(' || '));
        }
        if (found) {
            log('OTP modal detected');
            getOTP(function(code) {
                fillOTP(code);
                setTimeout(function(){ clickBtn(); }, 1500);
            });
            return;
        }
        if (retries < 80) setTimeout(function(){ watchOTP(retries+1); }, 1500);
        else log('OTP watcher timeout');
    };

    var clickBtn = function(retries) {
        retries = retries || 0;
        var btn = document.querySelector('button[data-testid="submit-button"]') ||
                  document.querySelector('button[data-testid="hosted-payment-submit-button"]') ||
                  document.querySelector('button[data-atomic-wait-intent="Submit_Email"]') ||
                  document.querySelector('button.SubmitButton--complete');
        if (!btn) {
            var all = document.querySelectorAll('button');
            for (var i = 0; i < all.length; i++) {
                var t = all[i].textContent.trim();
                if (t === '下一页' || t === 'Next' || t === 'Subscribe' || t === 'Pay' || t === 'Continue' || t === 'Agree' || t === 'Verify' || t === 'Confirm') {
                    btn = all[i]; break;
                }
            }
        }
        if (btn) {
            if (btn.disabled) {
                log('Button disabled, waiting... (' + retries + ')');
                if (retries < 15) setTimeout(function() { clickBtn(retries + 1); }, 1000);
                return;
            }
            var rect = btn.getBoundingClientRect();
            log('Button: ' + btn.textContent.trim() + ' visible: ' + (rect.height > 0));
            if (rect.height === 0) {
                if (retries < 15) setTimeout(function() { clickBtn(retries + 1); }, 1000);
                return;
            }
            log('Clicking: ' + btn.textContent.trim());
            btn.click();
        } else {
            if (retries < 15) setTimeout(function() { clickBtn(retries + 1); }, 1000);
        }
    };

    var host = window.location.host;
    var path = window.location.pathname;
    log('Host: ' + host + ' Path: ' + path);

    if (host.includes('pay.openai.com') || host.includes('checkout.stripe.com')) {
        log('=== OpenAI/Stripe Page ===');
        setTimeout(function() {
            var ppBtn = document.querySelector('[data-testid="paypal-accordion-item-button"]') || document.querySelector('.paypal-accordion-item button');
            log('PayPal button: ' + !!ppBtn);
            if (ppBtn) {
                ppBtn.click(); log('Clicked PayPal');
                setTimeout(function(){ ppBtn.click(); log('Clicked PayPal again'); }, 500);
            }
            setTimeout(function() {
                var cs = document.querySelector('#billingCountry')
                      || document.querySelector('select[name="country"]')
                      || document.querySelector('select[autocomplete="country"]');
                if (cs && cs.tagName === 'SELECT') {
                    if (cs.value !== 'US') {
                        var setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
                        setter.call(cs, 'US');
                        cs.dispatchEvent(new Event('change', { bubbles: true }));
                        cs.dispatchEvent(new Event('blur', { bubbles: true }));
                        log('Country -> US');
                    } else log('Country already US');
                } else {
                    log('Country select not found; selects: ' + Array.from(document.querySelectorAll('select')).map(function(s){ return s.id+'/'+s.name; }).join(' | '));
                }
                setTimeout(function() {
                    getAddr(function(addr) {
                        fillSel('#billingAddressLine1', addr.street);
                        setTimeout(function() {
                            var manual = null;
                            var nodes = document.querySelectorAll('button, a, [role="button"], span');
                            for (var i = 0; i < nodes.length; i++) {
                                var n = nodes[i];
                                var t = (n.textContent || '').replace(/\s+/g,' ').trim();
                                if (/^enter address manually$/i.test(t) && n.offsetParent !== null) {
                                    manual = n; break;
                                }
                            }
                            if (manual) {
                                manual.click();
                                log('Clicked "Enter address manually"');
                            } else {
                                log('Manual link not found, sending Escape');
                                var l1 = document.querySelector('#billingAddressLine1');
                                if (l1) {
                                    l1.dispatchEvent(new KeyboardEvent('keydown', { key:'Escape', code:'Escape', keyCode:27, which:27, bubbles:true }));
                                    l1.blur();
                                }
                            }
                            setTimeout(function() {
                                fillSel('#billingAddressLine1', addr.street);
                                fillSel('#billingLocality', addr.city);
                                fillSel('#billingPostalCode', addr.zip);
                                fillSelect('billingAdministrativeArea', addr.state);
                                var cb = document.getElementById('termsOfServiceConsentCheckbox');
                                if (cb && !cb.checked) { cb.click(); log('Checkbox checked'); }
                                setTimeout(clickBtn, 1000);
                                setTimeout(clickBtn, 4000);
                            }, 800);
                        }, 1000);
                    });
                }, 2500);
            }, 2500);
        }, 2000);
        return;
    }

    if (host.includes('paypal.com') && path === '/pay') {
        log('=== PayPal Login Page ===');
        setTimeout(function() {
            var email = randEmail();
            log('Email: ' + email);
            fill('email', email);
            setTimeout(function() {
                var ca = Array.from(document.querySelectorAll('button, a, [role="button"]')).find(function(b) {
                    var t = (b.textContent || '').replace(/\s+/g, ' ').trim();
                    return /^create an account$|创建.*[账帐][户号]|注册/i.test(t) && b.offsetParent !== null;
                });
                if (ca) {
                    log('Clicking: ' + ca.textContent.trim());
                    ca.click();
                } else {
                    log('Create Account not found, buttons: ' + Array.from(document.querySelectorAll('button,a')).map(function(b){ return (b.textContent||'').trim().slice(0,40); }).join(' | '));
                }
            }, 1500);
        }, 2000);
        return;
    }

    if (host.includes('paypal.com') && path.includes('/checkoutweb/')) {
        log('=== PayPal Checkout Page ===');
        var doFill = function() {
            getAddr(function(addr) {
                var email = randEmail();
                var password = randPass();
                log('Email: ' + email + ' Pass: ' + password);
                fill('email', email);
                fill('phone', CONFIG.phone);
                fill('cardNumber', CONFIG.cardNumber);
                fill('cardExpiry', CONFIG.cardExpiry);
                fill('cardCvv', CONFIG.cardCvv);
                fill('password', password);
                fill('firstName', 'James');
                fill('lastName', 'Smith');
                fill('billingLine1', addr.street);
                fill('billingCity', addr.city);
                fill('billingPostalCode', addr.zip);
                fillSelect('billingState', addr.state);
                setTimeout(clickBtn, 500);
                setTimeout(watchOTP, 5000);
            });
        };
        setTimeout(function() {
            var country = document.getElementById('country');
            if (country && country.value !== 'US') {
                country.value = 'US';
                country.dispatchEvent(new Event('change', { bubbles: true }));
                log('Country -> US, waiting...');
                setTimeout(doFill, 3000);
            } else {
                doFill();
            }
        }, 2000);
        return;
    }

    // PayPal 其他页面（如 "Set up once. Pay faster next time" 等）→ 自动点 Agree and Continue
    if (host.includes('paypal.com')) {
        log('=== PayPal Generic Page ===');
        var watchAgree = function(retries) {
            retries = retries || 0;
            var btn = null;
            var all = document.querySelectorAll('button, a, [role="button"]');
            for (var i = 0; i < all.length; i++) {
                var t = (all[i].textContent || '').replace(/\s+/g, ' ').trim();
                if (/^(agree[\s&]+continue|agree and continue|continue|agree|confirm|next)$/i.test(t) && all[i].offsetParent !== null) {
                    var r = all[i].getBoundingClientRect();
                    if (r.height > 0 && !all[i].disabled) {
                        btn = all[i]; break;
                    }
                }
            }
            if (btn) {
                log('Auto-click: ' + btn.textContent.trim());
                btn.click();
                return;
            }
            if ((retries % 4) === 0) log('Agree watch #' + retries + ': no button yet');
            if (retries < 40) setTimeout(function(){ watchAgree(retries+1); }, 1500);
            else log('Agree watcher timeout');
        };
        setTimeout(watchAgree, 2000);
        return;
    }

    log('Page not matched');
})();
