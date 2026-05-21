# GoPay 2.8.0 Android 协议逆向 知识库

**目标**: ChatGPT Plus 通过 GoPay 协议自动订阅 (`pipeline.py --gopay` / `--qris`)
**入口包**: `com.gojek.gopay` v2.8.0 build 2080
**架构**: Flutter Dart AOT + native libbatteryOpt.so SG anti-tamper
**Test device**: samsung SM-G780F (LXC cloudphone 124.236.70.143:22899)

---

## 1. 核心 ALGORITHM — X-E1 header

### Header format
```
X-E1 = <sha_hex>:<cipher_hex>:D:<ts_ms>
```
- `sha_hex` = HMAC-SHA256(displayEncoderKey, real_msg) — 64 hex chars
- `cipher_hex` = 32-byte HMAC chain output — 64 hex chars
- `D` = displayEncoderId constant
- `ts_ms` = millisecond timestamp

### 1.1 sha portion 算法

**标准 HMAC-SHA256**:
```python
sha = hmac.new(displayEncoderKey, real_msg, hashlib.sha256).hexdigest()
```

`real_msg` = 1867B 拼接 blob 含 SSO + device fields + cipher_hex + tail (见 §4)

**注意**: 之前以为是自定义 envelope MAC, 后发现 SHA-256 input 前 64 字节就是 HMAC 的 K^ipad 块. 所以本质是标准 HMAC.

### 1.2 cipher portion 算法 (HMAC 链)

```python
# 协议常数 (deterministic, no secrets)
ZERO_KEY_64B = b'\x00' * 64
HKDF_DATA = b'\x01' * 64
EXPAND_TAG = b'\x01' * 32

KEY_C = HMAC-SHA256(ZERO_KEY_64B, HKDF_DATA)
      = 19f253a44301c9648c03f3570581ff2875b6aa52b1499bb8afffe346c52ecf6e
KEY_D = HMAC-SHA256(KEY_C, HKDF_DATA)
      = 122a04d927bb0af35c01fc39abe9d3df7bf9e5fcd18eef511e1d69dc3f2220bb

# Per-request (need 32B alphanumeric nonce)
K9 = HMAC-SHA256(KEY_C, KEY_D || EXPAND_TAG || KEY_C || nonce_32B)
T1 = HMAC-SHA256(K9, KEY_D || EXPAND_TAG)
cipher = HMAC-SHA256(K9, T1 || EXPAND_TAG)  # T2
T3 = HMAC-SHA256(K9, T2 || EXPAND_TAG)      # stack-adjacent value, used in real_msg tail
```

**Test vector** (来自 2026-05-14 trace):
- nonce = `7x4lQPoyuPdiqNmcOda0T2x2FUELObMf`
- K = `1V79g&FZMB#zQ9:[T+8*xr1FXYVJ#%J)LiKl?c?=JG8dc{cX?d?p-u&Ti)$<vJC`
- ts = 1778758474793
- Expected X-E1 = `1c163a34ccde16b8653a26f1b2c2b31e6f1dcaa9ddce6113d3761903d2904132:a1192922aafc7b9da815811296d60a5884dc42c4392256dbd6891f04ee9eb939:D:1778758474793`

参考 impl: `CTF-pay/gopay_sign_v2.py` (14 单元测试全 PASS).

---

## 2. X-E2 header

**Device-static constant** (per install, not per request):
```
X-E2 = "ED9A2B38749FBDE9ACA61D6A685B7"  # 29 ASCII chars
```

不同 device 不同. 我们的 test device 是上述值.

---

## 3. 关键 keys / 内存地址 速查表

| 物件 | 类型 | 位置 | 实例值 (this device) |
|---|---|---|---|
| **displayEncoderKey** (X-E1 K) | 63B ASCII | 堆 (rw-)，独立 std::string | `1V79g&FZMB#zQ9:[T+8*xr1FXYVJ#%J)LiKl?c?=JG8dc{cX?d?p-u&Ti)$<vJC` |
| **legacy displayEncoderKey** (V1/e6) | 63B ASCII | `*(libbatteryOpt+0x92948)[+0x00]` std::string | `4&G6DbV&j8QZs~{)(Ila_w_\|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG\`\`]d7P&L` |
| **X-E2 constant** | 29 ASCII | `*(libbatteryOpt+0x92948)[+0x18]` std::string | `ED9A2B38749FBDE9ACA61D6A685B7` |
| **displayAesKey** | 16B raw (b64) | `*(libbatteryOpt+0x92948)[+0x30]` std::string | b64: `ZQyB45aUcFG6hYTHfqdtSw==` → hex: `650c81e396947051ba8584c77ea76d4b` |
| **displayEncoderId** | 1 char | hardcoded | `D` |
| **deviceId** | 16 hex chars | adb shell | `b66aedfffc4c1068` |
| **authSecret** | 29 ASCII | FlutterSecureStorage `/tmp/gp_sso.xml` (decrypted) | `raOUumeMRBNifqvZRFjvsgTnjAlaA9` |
| **SSO JWE A128GCM** | ~1209-1211 ASCII | heap rw-, prefix `eyJhbGciOiJkaXIi` | Saved at `/tmp/sso_token.txt` |

### Native code offsets (libbatteryOpt.so v2.8.0)

| Offset | Symbol | 备注 |
|---|---|---|
| `+0x703d4` | AES_encrypt | 调 displayAesKey 加密 |
| `+0x76940` | SHA-256 alt (rare) | — |
| `+0x82394` | SHA-256 update | 主 SHA, 我们 hook 它 |
| `+0x6df54` | Base64 encoder | configurable alphabet flag at `+0x92780` |
| `+0x78470` | e0 dispatcher | dispatch table at `+0x8b818` |
| `+0x78918` | e1 | — |
| `+0x78c9c` | e2 | 75-BL big fn |
| `+0x795c8` | e5 | AES caller |
| `+0x79b04` | e6 | AES caller |
| `+0x7a2d8` | HMAC_inner_oa | — |
| `+0x8b818` | dispatch table | 8 entries × 32B `{id_hash:8, fn_ptr:8, 0:8, 1:8}` |
| `+0x92780` | B64 alphabet flag (BSS) | 1 = standard alphabet |
| `+0x92778` | B64 alphabet ptr (BSS) | runtime-init points to `ABCDEFGHIJ...0123456789+/` |
| `+0x928e8` | key storage offset (e2 only) | 不同于 e5/e6 用的 +0x948 |
| `+0x92948` | **key_struct pointer** | 3 个 std::string @ +0/+0x18/+0x30 |

### Native code offsets (libapp.so v2.8.0 — Flutter Dart)

| Offset | Symbol | 备注 |
|---|---|---|
| `+0xdc5658` | `Hab.cCJ` | Sign main entry trampoline (Dart closure) |
| `+0xdc4880` | `Hab.UqI` | FFI trampoline |
| `+0xdc670c` | `Hab.oCJ` | "oa" helper |
| `+0xdc2c68` | `_mab.IAq` | Dio Interceptor base |
| `+0xdc2ca8` | HXM_mab impl | bl target |
| `+0x1fea15` | "X-E1" const | Dart object pool |
| `+0x1fea14` | "X-E2" const | adj. position |

### Dio Interceptor chain map (10 IAq closures in libapp.so)

| Address | Class | Trigger | BL target |
|---|---|---|---|
| `+0xdc2c68` | HXM_mab | always | `+0xdc2ca8` |
| `+0xdc2d04` | LXM_qab | always | `+0xdc2d44` |
| `+0xdc2fa0` | MXM_rab | payment-only | `+0x2305afc` (large impl) |
| `+0xdc3080` | VMN | always | `+0x2305e48` |
| `+0xdc96fc` | LVN | payment-only | `+0x2309c14` |
| `+0xdc9cac` | DfP | always | `+0x230a74c` |
| `+0xdca12c` | TPQ | payment-only | `+0x230ac04` |
| `+0xdf94c4` | TyP_cUf | header injector | `+0xdf9504` |
| `+0xdfb2c4` | HCP_qag | payment-only | `+0xdfb304` |
| `+0xe03640` | EUR | post-cCJ | `+0xde43b8` |

Order observed during 1 HTTP request: `LXM_qab → HXM_mab → DfP → VMN`, async gap, `cCJ`, then `EUR`.

---

## 4. signed_msg (real_msg) 1867B 结构

```
[0:64]    K^ipad (HMAC 内层 pad — 由 K 派生, 不是独立字段)
[64:N]    SSO JWE A128GCM token  (~1209B 变长)
[N:M]     `:samsung,<model>:3:<id-rand>,4:<bufsize>,5:<cpu>,6:<mac>,7:<ssid>,8:<res>,10:1,11:<hash32>,15:<hash16>,16:<uuid>:<version>:<md5_empty>:<deviceId>:<method>:<os>:<ts_ms>::<url>:<package>:`
[M:M+64]  cipher_hex (T2 of HMAC chain, computed first per request)
[M+64:..] tail
          - 16 chars "0000000000000000" (8 字节 0)
          - 16 chars LE u64 stack ptr (variable per call)
          - 16 chars "c244dc56c7b6026a" (CONST)
          - 16 chars LE u64 stack ptr (variable per call)
          - 32 chars `T3_first_16_bytes.hex()` (T3 of HMAC chain)
```

### Field-by-field 解释

| 字段 | 内容 | 例 |
|---|---|---|
| samsung,<model> | device model | `samsung, SM-G780F` |
| 3:<id-rand> | timestamp-random session id | `1778725895447-8854231793310991186` |
| 4:<bufsize> | malloc size | `131072` |
| 5:<cpu> | CPU info | `universal990\|1800\|8` |
| 6:<mac> | MAC | `74:05:A5:09:9C:61` |
| 7:<ssid> | WiFi SSID | `TPLINK-VNE9CI` |
| 8:<res> | screen resolution | `1080x1920` |
| 10:1 | flag | constant 1 |
| 11:<hash32> | 32-char b64 fingerprint hash | `CmfRoRt6Gzl3aiWRsbaczQgJFq0vuhETrNMRljTT700=` |
| 15:<hash16> | 16-byte hex hash (MD5?) | `724629431c694ee54c615f884bd17527` |
| 16:<uuid> | request UUID v4 | `f4105d22-bc01-41b4-90c6-5be29f6c2eac` |
| <version> | app version | `2.8.0` |
| <md5_empty> | MD5 of empty string | `d41d8cd98f00b204e9800998ecf8427e` |
| <deviceId> | from libbatteryOpt | `b66aedfffc4c1068` |
| <method> | HTTP method | `GET`/`POST` |
| <os> | OS string | `Android, 12` |
| <ts_ms> | ms timestamp | `1778758474793` |
| <url> | host+path | `customer.gopayapi.com/v1/users/profile` |
| <package> | app id | `com.gojek.gopay` |

参考 impl: `gopay_sign_v2.RealMsgTemplate` + `build_real_msg()`. 当前用 captured template 复制 + 字段替换.

---

## 5. 已确认的 GoPay API endpoints

### customer.gopayapi.com

| Endpoint | Method | 鉴权 | 状态 |
|---|---|---|---|
| `/v1/support/customer/activity` | POST | Bearer SSO + X-E1 + X-E2 | ✓ 完全工作 |
| `/api/v1/users/pin/tokens/nb` | POST | Bearer SSO + X-E1 | ✓ 真 endpoint (需 challenge_id + client_id + pin) |
| `/api/v1/users/pin/tokens` | POST | Bearer SSO + X-E1 | ✓ 真 endpoint (需 RSA encrypted PIN, schema 未知) |
| `/api/v1/users/pin/challenges` | POST | Bearer SSO + X-E1 | ✓ 真 endpoint (GoPay-1000 generic err — 缺 input) |
| `/bff/v1/payment/{tref}` | GET | Bearer SSO + X-E1 | ✓ 真 endpoint (`?pdg=gopay\|midtrans` 必传, 返 status+deeplink) |
| `/bff/v1/payment/{tref}/confirm` | POST | Bearer SSO + X-E1 | ⚠️ 真 endpoint (`?pdg=midtrans` 必传, body schema 未知) |
| `/bff/v1/payment/{tref}/pay` | POST | Bearer SSO + X-E1 | ⚠️ 真 endpoint (body schema 未知) |
| `/bff/v1/payment/{tref}/initiate` | POST | Bearer SSO + X-E1 | ⚠️ 真 endpoint (body schema 未知, 可能返 challenge) |
| `/bff/v1/payment/{tref}/start` | POST | Bearer SSO + X-E1 | ⚠️ 真 endpoint (body schema 未知) |
| `/bff/v1/payment-intermediary` | POST | Bearer SSO + X-E1 | ⚠️ 真 endpoint (需 `?pdg=midtrans`, body schema 未知) |
| `/bff/v3/payment/inquiry` | GET | Bearer SSO + X-E1 | ⚠️ 真 endpoint (`?pdg` 值 限制, "gopay"/"midtrans" 都被拒) |
| `/v1/users/profile` | GET | Bearer SSO + X-E1 | ⚠️ 400 GoPay-1000 (账号资格/headers 缺) |
| `/v2/users/kyc/status` | GET | Bearer SSO + X-E1 | ⚠️ 400 GoPay-1000 |
| `/v3/balances/me` | GET | — | ✗ 404 |
| `/v1/wallet/balance` | GET | — | ✗ 404 |
| `/v1/transactions` | GET | — | ✗ 404 |

### gwa.gopayapi.com (linking gateway)

需 Origin/Referer `merchants-gws-app.gopayapi.com` (没 X-E1).

| Endpoint | Method | 用途 |
|---|---|---|
| `/v1/linking/validate-reference` | POST `{reference_id}` | linking flow step 1 |
| `/v1/linking/user-consent` | POST `{reference_id}` | linking flow step 2 (触发 OTP) |
| `/v1/linking/validate-otp` | POST `{reference_id, otp}` | linking flow step 3 (返 challenge_id, client_id) |
| `/v1/linking/validate-pin` | POST `{pin_token}` | linking flow step 5 |
| `/v1/payment/validate` | GET `?reference_id=` | charge flow step 1 |
| `/v1/payment/confirm` | POST `?reference_id=` `{payment_instructions:[]}` | charge flow step 2 (返 challenge_id, client_id) |
| `/v1/payment/process` | POST `?reference_id=` `{challenge:{type:GOPAY_PIN_CHALLENGE,value:{pin_token}}}` | charge flow step 4 (settle) |

### Midtrans (app.midtrans.com)

需 Basic auth `Mid-client-3TX8nUa-f_RgNrky` (OpenAI's, public).

| Endpoint | Method | 用途 |
|---|---|---|
| `/snap/v1/transactions/{snap_token}` | GET | 拿 merchant info + enabled_payments |
| `/snap/v3/accounts/{snap_token}/linking` | POST `{type:gopay,country_code,phone_number}` | start linking, 返 reference_id |
| `/snap/v2/transactions/{snap_token}/charge` | POST `{payment_type:gopay,tokenization:false,gross_amount:"1"}` | 创建 QRIS charge (我们用 gross_amount=1 override 绕开 GoPay C07) |
| `/snap/v1/transactions/{snap_token}/status` | GET | 轮询 settlement |

### Hosts 白名单 (DISPLAY_ENCODER_ENHC_HOSTS) — X-E1 必发

- `customer.gopayapi.com`
- `customer-tagihan.gopayapi.com`
- `api.gojekapi.com`
- `accounts.goto-products.com`

---

## 6. 完整 HTTP request headers (从 cloudphone GoPay 抓的)

POST `customer.gopayapi.com/v1/support/customer/activity` 实际 headers:

```http
accept-encoding: gzip
authorization: Bearer <SSO JWE>
x-location-accuracy: 14.16100025177002
gojek-service-area: 1
country-code: ID
support-request-id: <UUID v4>
x-appversion: 2.8.0
x-location: 35.6763787,139.649962
content-length: <N>
x-m1: 3:<ts>-<rand>,4:131072,5:<cpu>,6:<mac>,7:<ssid>,8:<res>,10:1,11:<hash32>,15:<hash16>,16:<uuid>
gojek-country-code: ID
x-uniqueid: <deviceId>
x-phonemake: samsung
x-help-version: 2.8.0
x-e1: <sha>:<cipher>:D:<ts>      ← our v2 signer
user-agent: GoPay/2.8.0 (com.gojek.gopay; build:2080; Android, 12)
x-deviceos: Android, 12
x-user-type: customer
x-appid: com.gojek.gopay
gojek-timezone: Asia/Jakarta
content-type: application/json
x-apptype: GOPAY
x-user-locale: en_ID
x-e2: ED9A2B38749FBDE9ACA61D6A685B7      ← from key_struct+0x18
```

`x-m1` 是 device fingerprint 字符串, 同 `signed_msg` field_list 头部一致.

---

## 7. 15-step ChatGPT Plus → GoPay 完整 flow

```
[ChatGPT side]
1. POST  chatgpt.com/backend-api/payments/checkout                  → cs_live_xxx
2. POST  api.stripe.com/v1/payment_methods (type=gopay)             → pm_xxx
3. POST  api.stripe.com/v1/payment_pages/{cs}/confirm               → status:open
4. POST  chatgpt.com/backend-api/payments/checkout/approve          → approved

[Stripe → Midtrans bridge]
5. GET   pm-redirects.stripe.com/authorize/{nonce}                  → 302 → midtrans
6. GET   app.midtrans.com/snap/v1/transactions/{snap_token}         ← merchant info
7. POST  app.midtrans.com/snap/v3/accounts/{snap_token}/linking
            body: {type:gopay, country_code, phone_number}
            (406 → 12s sleep → 201) (或 429 风控时剥 Authorization 头重发)
                                                                    ← reference_id
[GoPay linking — gwa endpoints, 不需 X-E1]
8. POST  gwa.gopayapi.com/v1/linking/validate-reference             ← display info
9. POST  gwa.gopayapi.com/v1/linking/user-consent                   ← OTP triggered via WhatsApp/SMS to phone
10. POST gwa.gopayapi.com/v1/linking/validate-otp                   ← challenge_id, client_id
[X-E1 protected]
11. POST customer.gopayapi.com/api/v1/users/pin/tokens/nb           ← pin_token (JWT)
       body: {challenge_id, client_id, pin}
[gwa endpoints]
12. POST gwa.gopayapi.com/v1/linking/validate-pin                   ← linking complete

[Midtrans charge & settle]
13. POST app.midtrans.com/snap/v2/transactions/{snap}/charge        ← charge_ref (A12...)
14. GET  gwa.gopayapi.com/v1/payment/validate?reference_id=...
    POST gwa.gopayapi.com/v1/payment/confirm?reference_id=...       ← second challenge
    POST customer.gopayapi.com/api/v1/users/pin/tokens/nb           ← second pin_token (X-E1 required)
    POST gwa.gopayapi.com/v1/payment/process?reference_id=...       ← settled

[ChatGPT verify]
15. GET  chatgpt.com/checkout/verify?stripe_session_id=...          ← Plus active
```

X-E1 v2 signer **只在 step 11 和 14 的 pin/tokens/nb 调用必需**. 其他 step 用 OAuth/Basic auth.

### QRIS 简化 flow (--qris)

跳过 linking (step 7-12), 直接 step 13 创建 QRIS charge → 用户 GoPay app 扫码 + PIN → Midtrans webhook → ChatGPT verify.

我们的 qris.py 已 monkey-patch 加 `gross_amount: "1"` 绕开 GoPay 不接 0 IDR 限制.

---

## 8. 逆向方法论 — 下次 app 更新时如何快速 re-locate

### 8.1 快速 sanity check (现版本是否还能用)

```bash
# 1. 启动 GoPay + 抓 X-E1 sample
adb shell am start -a android.intent.action.VIEW -d "gopay://envelope/home" -p com.gojek.gopay

# 2. memscan rw memory 找 ":D:" pattern
python3 dump_all_xe1.py  # 已成熟脚本

# 3. 用 K = displayEncoderKey + v2 signer 算 sha, 对比. 如果一致, algorithm 未变.
python3 CTF-pay/gopay_sign_v2.py  # self-test
```

### 8.2 如果 X-E1 算法变了

1. **重新抓 1 个 X-E1 ground truth**: `dump_all_xe1.py` from memory
2. **frida hook SHA-82394** in libbatteryOpt 抓 final hash session
3. **观察 input 96B 的 K^opad 区**: 前 64B XOR 0x5c = K_padded
4. **如果是 HMAC**: K (前 63 ASCII) + msg → 直接 HMAC-SHA256 验证
5. **如果不是**: 看 input 结构 (`prefix || nonce || const` 等)

参考 `xe1_algorithm_FINAL.md` 中 25-29 轮 reverse 过程详记.

### 8.3 如果 libbatteryOpt 偏移变了 (app 升级)

```bash
# 找 SHA-256 update fn (新偏移):
objdump -d libbatteryOpt.so | grep -B2 -A5 "K_=\|ABCDEF" | head -20
# 或 frida 反向: 找所有 fn 中输出 SHA 块的, 通常 onLeave state[16:80] = hash state

# 找 key_struct ptr (常在 0x92xxx 区):
strings libbatteryOpt.so | grep -E "^.{63}$" | head -5  # 找 63B 候选
# 然后 frida memscan + 看哪个被 load 时引用

# 找 X-E2:
strings libbatteryOpt.so | grep -oE '[A-Z0-9]{29}'  # 找 29 个大写hex char
```

### 8.4 如果 endpoint 路径变了

```bash
# 全 endpoint 字符串扫:
strings libapp.so | grep -oE '/[a-z]+/v[0-9]+/[a-zA-Z0-9_/-]+' | sort -u

# 找 /bff/ family:
strings libapp.so | grep -E '/bff/'

# 找 query param 名 (pdg, tref, reference_id):
strings libapp.so | grep -oE '"[a-z_]+":' | sort -u
```

### 8.5 如果 cipher 算法变了

1. Hook SHA-82394 + 收集所有 SHA sessions
2. 按 session 输入结构分类 (2-block 96B = HMAC outer, 3-block = HMAC inner)
3. 看 outer session 的 K^opad 区 = K XOR 0x5c
4. 验证 chain: 该 K 是否是更早 session 的 hash output (= HMAC chain)
5. 看 chain 末端的 K_real 是否在 heap 静态字符串 (= 我们的 K9_real)

### 8.6 如果 Stripe/Midtrans 端 API 变了

- Stripe pricing API: 看 `chatgpt.com/backend-api/checkout_pricing_config/configs/ID` 返 promo eligibility
- Midtrans charge body: 看 `app.midtrans.com/snap/v2/transactions/{snap}/charge` 实际 payload
- promo 命中条件: 出口 IP 需是 Stripe 认的对应国家 (`Webshare` lock_country)

---

## 9. 已尝试 / 失败 paths (避免重复踩坑)

| 尝试 | 结果 | 原因 |
|---|---|---|
| mitm with system CA cert install | ❌ GoPay SG trigger "suspicious" popup | Flutter SSL pinning (`libflutter.so` 内置 cert verify) |
| mitm transparent + iptables redirect | ❌ GoPay 网络断退桌面 | iptables REDIRECT 让 Flutter `socket()` 直接 EINVAL |
| frida-server on cloudphone | ⚠️ 不稳定 (segfault / timeout) | LXC 内核 limit + cross-Pacific RTT (frida control protocol 不耐高延迟) |
| 静态找 PIN RSA pubkey | ❌ 不在 strings | runtime-fetch (endpoint 我们没找到) |
| 静态找 charge body schema | ❌ Dart AOT obfuscate 字段名 | 需 runtime capture |
| 暴力 brute 200+ body shapes | ❌ 全 400/500 | schema 含 nested object 或加密字段 |
| 跨太平洋 HTTP proxy mitm | ❌ TCP proxy CONNECT 包被 NAT 改 | 中间盒/防火墙 干预 |
| `adb reverse + transparent mitm` | ❌ SO_ORIGINAL_DST 丢失 | NAT 通道破坏 transparent 信息 |
| 删 `rFile/rFileV2/rDataV2` | ❌ 仍闪退 | tamper flag 不只本地, server 端 fingerprint 也标了 |

---

## 10. 工具链 / 文件 速查

### 主代码
| 文件 | 用途 |
|---|---|
| `CTF-pay/gopay_sign_v2.py` | X-E1/X-E2 reference impl + self-test |
| `CTF-pay/gopay_client.py` | full HTTP client (just URL+body) |
| `CTF-pay/gopay_protocol_pay.py` | v1+v2 dual signer for pipeline integration |
| `CTF-pay/gopay_protocol_client_v2.py` | gwa-based linking flow client |
| `CTF-pay/qris.py` | QRIS path (含 gross_amount=1 patch) |
| `CTF-pay/gopay.py` | Tokenization path 15-step impl |
| `CTF-pay/tests/test_gopay_sign_v2.py` | 14 regression tests |
| `output/gopay_2.8.0_extract/extract_K.py` | Frida K extractor |

### 静态资源
| 文件 | 用途 |
|---|---|
| `/tmp/big_msg_1867.bin` | captured signed_msg template |
| `/tmp/sso_token.txt` | captured SSO (per process, may expire) |
| `output/gopay_2.8.0_extract/base.apk` | GoPay 2.8.0 APK |
| `output/gopay_2.8.0_extract/split_arm64.apk` | arm64 native libs |
| `output/gopay_2.8.0_extract/lib*.so` | extracted native libs |
| `/tmp/blutter_out/` | Dart AOT decompile output (13141 dart files) |
| `/tmp/blutter_out/pp.txt` | Dart object pool (含 X-E1/X-E2 strings + endpoints) |

### 已抓 ground truth
| 文件 | 内容 |
|---|---|
| `xe1_ground_truth.json` | 14 captured X-E1 values |
| `/tmp/all_xe1.json` | 最近 batch |
| `/tmp/broad_hooks.json` | 438 events from broad SHA hook |
| `/tmp/atomic_sign.json` | atomic capture during sign event |

### 重要 frida 脚本 (在 /tmp/)
| 脚本 | 用途 |
|---|---|
| `dump_all_xe1.py` | memscan 抓所有 X-E1 strings in process |
| `hook_sha_atomic.py` | hook SHA-82394 + 即时 memscan |
| `hook_broad.py` | hook 26 libbatteryOpt fns (大网) |
| `hook_atomic_xe1.py` | hook SHA + 大窗 input 抓 |
| `read_dt_full.py` | runtime dump dispatch table at +0x8b818 |
| `dump_ks2.py` | dump key_struct 3 fields |

---

## 11. 当前 device state cleanup steps (恢复 cloudphone GoPay 可用)

```bash
# 清 mitm 残留
adb shell settings put global http_proxy ":0"
adb shell rm /system/etc/security/cacerts/c8750f0d.0  # mitm CA
adb shell iptables -t nat -F OUTPUT

# 清 frida 残留
adb shell rm /system/bin/frida-server
adb shell rm /data/local/tmp/frida-server
adb shell pkill -9 frida-server

# 清 adb tunnels
adb forward --remove-all
adb reverse --remove-all

# 重启 cloudphone (彻底重置 SG cooldown)
adb reboot
# 等 60s 后重连
adb connect <ip:port>
adb shell <password>  # 重新输 ceqKkciQ
```

**Server-side fingerprint cooldown** (backend 标 device suspicious): 6-24h 自动重置. 加速法:
- 换 SSO (重 login)
- 换 device fingerprint (改 deviceId / x-m1 字段 — 但同时会让账号看起来是新设备, 需重 link)

---

## 12. ChatGPT Plus 订阅完整 config 模板 (saved at `/tmp/plus_run.json`)

```jsonc
{
  "fresh_checkout": {
    "auth": {
      "access_token": "<from temp.json>",
      "session_token": "<from temp.json>",
      "mode": "access_token"
    },
    "plan": {
      "billing_country": "ID",      // ID locked by Stripe customer
      "billing_currency": "IDR",
      "promo_campaign_id": "plus-1-month-free",
      "plan_name": "chatgptplusplan"
    },
    "allow_charge_when_coupon_ineligible": false
  },
  "gopay": {
    "country_code": "86",
    "phone_number": "17788949030",   // GoPay linked phone (whatever app uses)
    "pin": "870657",
    "otp": {"source": "auto"},
    "protocol": {
      "enabled": true,
      "sign_version": "v2",
      "extra": {
        "display_encoder_key": "1V79g&FZMB#zQ9:[T+8*xr1FXYVJ#%J)LiKl?c?=JG8dc{cX?d?p-u&Ti)$<vJC",
        "signed_msg_template": "/tmp/big_msg_1867.bin"
      }
    }
  },
  "qris": {
    "adb_auto": {
      "enabled": true,
      "pin": "870657",
      "serial": "124.236.70.143:22899"
    }
  },
  "webshare": {
    "lock_country": "JP"    // JP IP triggers Stripe Plus promo (1 IDR test charge)
  }
}
```

跑: `python3 pipeline.py --qris --plan plus --pay-only --target-emails <email> --config /tmp/plus_run.json`

---

## 13. 接手 checklist — 下次开干

- [ ] 检查 `gopay_sign_v2.py self-test` 是否仍 PASS
- [ ] `extract_K.py` 抓当前 process K (可能 rotated)
- [ ] 抓 fresh `signed_msg` template (memory scan 或 SHA hook)
- [ ] 抓 fresh SSO (process 重启过则 reissue)
- [ ] 跑 pipeline + 看 `pricing: due=` 行 (确认 promo 命中 = 1 IDR)
- [ ] cloudphone GoPay 能开 (没 SG 卡死)
- [ ] adb_auto PIN tap → settle → ChatGPT verify

---

## 14. 关键决定的 historical reasoning

1. **为什么是 HMAC-SHA256 不是 envelope MAC**: 第 25 轮先以为是 envelope MAC `SHA(K^opad||SHA(msg))`, 第 29 轮发现 SHA-256 input 前 64B = K^ipad (K XOR 0x36), 整体就是标准 HMAC. envelope MAC 只是 envelope 的一种特例.

2. **为什么 1 IDR 不是 0 IDR**: ChatGPT plus-1-month-free promo 让 Stripe pricing.due=0. 但 GoPay merchant C07 拒 0 IDR QRIS. 我们在 qris.py `_midtrans_create_qris_charge` 的 attempts body 加 `"gross_amount": "1"` 强制 1 IDR. GoPay app 显示 Rp1 review → 接受.

3. **为什么 K_real 不在 key_struct[+0]**: key_struct[+0] 是 V1 legacy displayEncoderKey (用 e6 dispatch). v2 X-E1 用的 K 是 process 单独 alloc 的 std::string, 不在 key_struct 内. 需 SHA hook 推算 (从 K^opad block).

4. **为什么 cloudphone 闪退**: 累积 tamper hits 让 SG 标 device. Local rFile/rFileV2/rDataV2 不是唯一 state, server 端也 fingerprint. 6-24h 自动 cooldown.

5. **promo eligibility 需 JP IP**: ChatGPT 给 plus-1-month-free 看出口 IP (Stripe 看 IP geo). 印尼 IP 也能命中, JP 也能命中, US 不行. 用 Webshare lock_country=JP 路由.

---

## 15. 还需要 reverse 的部分 (TODO)

- [ ] `/api/v1/users/pin/tokens` direct mode body schema (RSA encrypted PIN format + 额外 fields)
- [ ] PIN encryption RSA pubkey (runtime fetch endpoint 未知)
- [ ] `/bff/v1/payment/{tref}/confirm?pdg=midtrans` body schema (含 pin_token 但其他字段?)
- [ ] `/bff/v1/payment/{tref}/initiate?pdg=midtrans` body schema (可能返 challenge)
- [ ] X-E2 是否需 rotate (我们假设 device-static, 没验证长期 valid)
- [ ] `x-m1` 字段 11 (`CmfRoRt6Gzl3aiWRsbaczQgJFq0vuhETrNMRljTT700=`) 32 字符 b64 hash 如何算
- [ ] `x-m1` 字段 15 (`724629431c694ee54c615f884bd17527`) 16 字节 hex hash 如何算

抓真 GoPay app charge request (mitm + frida SSL bypass) 是补这些 piece 的最直接方式.
