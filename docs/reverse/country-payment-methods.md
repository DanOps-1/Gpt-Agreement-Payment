# OpenAI ChatGPT Plus — 全球 234 国 PaymentMethod 矩阵

> 采集时间:2026-05-13 18:35 JST
> 方法:`probe_country_payment_methods.py` 走 mitm + gost(JP 住宅 socks5),
> 对每个国家创建 checkout session → Stripe init 抠 `payment_method_types`,
> 不走 PayPal / 不签 mandate,**不触发 dedup,不扣钱**(promo 100% off,due=£0)
> 原始数据:`country_payment_methods.json`(同目录)

---

## TL;DR

OpenAI 在全球只启用 **8 种 PaymentMethod**:`card / paypal / pix / gopay / upi / kr_card / kakao_pay / naver_pay`

- **198 国 (85.3%)**:`card + paypal`(EU/US/GB/AU/CA + 大多数小国)
- **30 国 (12.9%)**:`card` 单一(CH/SE/SG/JP/PL 等)
- **5 国 (2.1%)** 有"非卡非 PayPal"的本地协议:**BR(pix)/ ID(gopay)/ IN(upi)/ KR(kr_card+kakao_pay+naver_pay)**
- **3 错误**:`EU`/`US2` 不是合法 ISO 代码;`HK` HKD 不在 OpenAI currency enum 内

**所有银行转账 / EU 本地 / 亚洲钱包 / BNPL** 都被 OpenAI 在 Stripe 那层 disable,实际可用的只有 8 种。

---

## 完整分组

### 1. `card + paypal` (198 国)
```
AD AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
BM BN BO BQ BS BT BV BW BY BZ CC CD CF CG CI CK CM CN CR CU CV CW CY DE DJ
DM DO DZ EC EE ER ES ET FI FJ FK FM FO FR GA GD GE GF GG GH GI GL GM GN GP
GQ GR GS GT GU GW GY HM HN HR HT ID IE IM IO IQ IR IS IT JE JM JO KE KG KH
KI KM KN KP KW KY LA LB LC LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML
MM MN MO MP MQ MR MS MT MU MV MW MZ NA NC NE NF NI NL NP NR NU NZ OM PA PG
PM PN PR PS PT PY RE RS RU RW SB SC SD SH SI SJ SK SL SM SN SO SR SS ST SV
SX SY SZ TC TD TF TG TJ TK TL TM TN TO TR TT TV TZ UA UG UM UY UZ VA VC VE
VG VI VN VU WF WS XK YE YT ZM ZW
```

### 2. `card` 单一 (30 国)
```
AE  CH  CL  CO  CZ  DK  EG  HU  IL  JP
KR(等等,KR有 4 种,不在这)... 实际 30 国:
AE CH CL CO CZ DK EG HU IL JP KZ LI MX MY NG NO PE PH PK PL QA RO SA SE SG
TH TW UA UY VN ZA
```
*(注:KR 不在此组,KR 有 4 种独立 PaymentMethod 单列)*

### 3. 本地协议国家 (5 国 × 1-3 种本地通道)
| 国家 | 货币 | 本地通道 | 备注 |
|---|---|---|---|
| BR 🇧🇷 | BRL | **`pix`** | 巴西央行 Bacen Open Finance 强制 API,SDK 公开 |
| ID 🇮🇩 | IDR | **`gopay`** | 项目 dev 分支已逆向 Midtrans / GoPay SDK |
| IN 🇮🇳 | INR | **`upi`** | NPCI 统一协议,需要印度 KYC + 银行账户种子 |
| KR 🇰🇷 | KRW | **`kr_card` `kakao_pay` `naver_pay`** | 4 种 PaymentMethod 唯一国家;app 加固重 |

### 4. 错误 (3)
- `EU`:不是 ISO country code,而是欧盟代号;OpenAI 后端拒绝(`Input should be 'US' | 'GB' | ...`)
- `US2`:同上,内部 placeholder
- `HK`:`HKD` 不在 OpenAI checkout currency enum,需要用 USD billing(用 country=HK + currency=USD 应该能过,本次测试用 HKD 失败)

---

## OpenAI 完全没启用的 Stripe PaymentMethod

下面这些 Stripe 支持 + 当地市场有大量份额,但 OpenAI 完全没启用:

| 类型 | 描述 | 为什么 OpenAI 不启 |
|---|---|---|
| `sepa_debit` | EU SEPA 直接借记 | bank debit 类 chargeback 周期长(56 天)+ 反欺诈难 |
| `bacs_debit` | GB BACS 直接借记 | 同上 |
| `acss_debit` | CA 加拿大银行借记 | 同上 |
| `au_becs_debit` | AU 澳大利亚直接借记 | 同上 |
| `customer_balance` | Stripe 银行转账钱包 | 同上 |
| `ideal` / `bancontact` / `sofort` / `giropay` / `eps` / `blik` / `p24` / `multibanco` | EU 各国银行转账 | bank 类 + 反欺诈 |
| `mobilepay` / `klarna` / `satispay` / `twint` / `bizum` | EU 钱包 / BNPL | 风控难 + 退款机制不同 |
| `alipay` / `wechat_pay` | 中国钱包 | 中国大陆 ToS 限制(OpenAI 不向 CN 提供服务) |
| `promptpay`(TH)/ `fpx`(MY)/ `grabpay`(SG/MY)/ `paynow`(SG) | 东南亚本地 | OpenAI 选不启用,东南亚走 card |
| `oxxo`(MX 现金券)/ `boleto`(BR 现金券) | 拉美现金 | 无法立即 settle |
| `cashapp` / `affirm` / `afterpay_clearpay` / `venmo` | 美国 BNPL/P2P | 同上 |

模式总结:**OpenAI 只启用三类**:
1. **卡**(全球通用,3DS 协议化成熟)
2. **PayPal**(全球普及,且 PayPal 自身承担反欺诈)
3. **国家级强制本地协议**(印度 UPI / 印尼 GoPay / 巴西 PIX / 韩国 ISP card + kakao + naver) —— 这些国家有当地央行 / 监管强制要求,不启用就根本拿不到当地用户

---

## 对 card.py / batch 项目的意义

### 主战场
**card** 仍然是 batch 最稳路径(232/234 国可用),Stripe customer 按卡 fingerprint dedup,1 张卡 → 1 个独立 customer → 1 个 ChatGPT Plus,跟 PayPal payer-level dedup 不互通。

### 多通道 batch 候选(绕开 PayPal payer dedup 的另一条路)

| 通道 | 协议公开度 | 项目契合 | 当下推荐 |
|---|---|---|---|
| **ID gopay** | 中(Midtrans SDK,项目已逆向) | ✅✅ dev 分支有 `gopay_sign.py` / `gopay_protocol_pay.py` | 最高 |
| **BR pix** | 高(Bacen Open Finance 公开 spec) | 0(无积累) | 中,需巴西 PSP 账号种子 |
| **IN upi** | 高(NPCI 协议公开) | 中(抓包已采到 Stripe UPI 协议链) | 中,需印度 UPI 账号种子 |
| **KR kr_card** | 低(韩国 ISP 认证,ActiveX 历史包袱) | 0 | 低,韩国卡协议复杂 |
| **KR kakao_pay / naver_pay** | 低(app SDK 加固重) | 0 | 低,韩国 KYC 难取得 |

### KR 是唯一一个支持 4 种 PaymentMethod 的国家

理论上 1 个韩国身份能在 OpenAI 后端拿到 4 个独立 PaymentMethod fingerprint(card / kr_card / kakao_pay / naver_pay),**各自走独立 dedup namespace**,所以 1 韩国 user 理论可开 4 个 Plus(?待实测)。但门槛:
- 韩国实名(SSN / 住民登录番号)
- 韩国手机号 + 韩国银行账户(开 kr_card)
- Kakao Talk 实名认证(开 kakao_pay)
- Naver 实名认证(开 naver_pay)

---

## 文件索引

```
CTF-pay/reverse_tools/bitbrowser_capture/
└─ probe_country_payment_methods.py    采集脚本

docs/reverse/
├─ country-payment-methods.md          本文档
├─ country_payment_methods.json        脱敏后 235 国原始数据
└─ paypal-dedup-reverse.md             dedup 模型反推
```

---

*采集自 user-tRRcNJzmLbHXK7TeZfFIWG84 在 BitBrowser profile B(JP 出口 IP 82.24.43.131)上的 ChatGPT session,
通过 235 次轻量 `POST /backend-api/payments/checkout + POST /v1/payment_pages/<sid>/init` 探测获取。
没有走完 PayPal/Stripe 协议授权,因此不触发 dedup,不创建 zombie billing agreement,不扣款。*
