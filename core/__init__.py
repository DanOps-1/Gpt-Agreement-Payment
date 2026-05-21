"""共享层：从 CTF-pay/CTF-reg 抽出来的、跟具体支付/注册流程无关的纯工具。

模块：
- otp_extractor: 从文本/JSON payload 中解析 OTP 验证码（regex + 时间戳判定）
- otp_providers: CLI / 文件轮询 / HTTP 轮询 / 子命令四种 OTP 提供器
- jwt_decode: OAuth JWT 解码 + 过期判定 + email/plan 字段提取

后续 Wave 会再把 http_session、chatgpt_auth、logging 抽到这里。
"""
