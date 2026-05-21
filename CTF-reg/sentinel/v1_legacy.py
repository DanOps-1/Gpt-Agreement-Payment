"""
OpenAI Sentinel Token 生成
基于 https://github.com/leetanshaj/openai-sentinel
流程: PoW 计算 → 请求 /sentinel/req → 解析 Turnstile → 组装 Token
"""
import hashlib
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import pybase64

logger = logging.getLogger(__name__)

# ── 浏览器环境模拟常量 ──
CORES = [8, 16, 24, 32]

CACHED_SCRIPTS = [
    "https://cdn.oaistatic.com/_next/static/cXh69klOLzS0Gy2joLDRS/_ssgManifest.js"
    "?dpl=453ebaec0d44c2decab71692e1bfe39be35a24b3"
]

CACHED_DPL = ["prod-f501fe933b3edf57aea882da888e1a544df99840"]

NAVIGATOR_KEYS = [
    "registerProtocolHandler-function registerProtocolHandler() { [native code] }",
    "storage-[object StorageManager]",
    "locks-[object LockManager]",
    "appCodeName-Mozilla",
    "permissions-[object Permissions]",
    "webdriver-false",
    "product-Gecko",
    "clipboard-[object Clipboard]",
    "productSub-20030107",
    "vendor-Google Inc.",
    "onLine-true",
    "cookieEnabled-true",
    "hardwareConcurrency-32",
    "pdfViewerEnabled-true",
    "appName-Netscape",
]

DOCUMENT_KEYS = [
    "location", "cookie", "title", "scripts", "styleSheets",
    "images", "forms", "links", "head", "body",
]

WINDOW_KEYS = [
    "location", "chrome", "performance", "navigator", "document",
    "crypto", "fetch", "localStorage", "sessionStorage", "indexedDB",
]

MAX_ITERATION = 500000
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


# ── PoW 计算 ──

def _get_parse_time() -> str:
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


def _build_config(user_agent: str) -> list:
    return [
        random.choice([1920 + 1080, 2560 + 1440, 1920 + 1200, 2560 + 1600]),
        _get_parse_time(),
        4294705152,
        0,
        user_agent,
        random.choice(CACHED_SCRIPTS),
        random.choice(CACHED_DPL),
        "en-US",
        "en-US,en",
        0,
        random.choice(NAVIGATOR_KEYS),
        random.choice(DOCUMENT_KEYS),
        random.choice(WINDOW_KEYS),
        random.choice(CORES),
        0,
    ]


def _generate_answer(seed: str, diff: str, config: list):
    diff_len = len(diff) // 2
    target_diff = bytes.fromhex(diff)
    seed_encoded = seed.encode()
    config_json = json.dumps(config, separators=(",", ":")).encode()
    static_config_part1 = b'[' + config_json + b','
    static_config_part3 = b']'

    for i in range(MAX_ITERATION):
        dynamic_json_i = str(i).encode()
        dynamic_json_j = str(i >> 1).encode()
        final_json_bytes = (
            static_config_part1 + dynamic_json_i + b"," +
            dynamic_json_j + static_config_part3
        )
        base_encode = pybase64.b64encode(final_json_bytes)
        hash_value = hashlib.sha3_512(seed_encoded + base_encode).digest()
        if hash_value[:diff_len] <= target_diff:
            return base_encode.decode(), True

    fallback = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + pybase64.b64encode(
        f'"{seed}"'.encode()
    ).decode()
    return fallback, False


def generate_pow_token(user_agent: str = DEFAULT_UA) -> str:
    """生成 Proof of Work token (gAAAAAC...)"""
    config = _build_config(user_agent)
    seed = format(random.random())
    diff = "0fffff"
    solution, found = _generate_answer(seed, diff, config)
    token = "gAAAAAC" + solution
    if found:
        logger.debug(f"PoW 计算成功, token 长度: {len(token)}")
    else:
        logger.warning("PoW 计算未找到解, 使用 fallback")
    return token


# ── Sentinel Token 完整流程 ──

def get_sentinel_token(
    session,
    device_id: str,
    flow: str = "authorize_continue",
    user_agent: str = DEFAULT_UA,
) -> str:
    """
    完整 Sentinel Token 生成:
    1. PoW 计算
    2. 请求 /sentinel/req 获取 Turnstile challenge
    3. 组装完整 token
    """
    # Step 1: PoW
    pow_token = generate_pow_token(user_agent)
    logger.info(f"PoW token 生成完成 (长度: {len(pow_token)})")

    # Step 2: 请求 sentinel/req
    payload = json.dumps({
        "p": pow_token,
        "id": device_id,
        "flow": flow,
    })
    headers = {
        "Origin": "https://sentinel.openai.com",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
        "Content-Type": "text/plain;charset=UTF-8",
        "User-Agent": user_agent,
    }

    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        headers=headers,
        data=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        logger.warning(f"Sentinel req 失败: {resp.status_code}, 回退到无 PoW 模式")
        return json.dumps({
            "p": pow_token, "t": "", "c": "",
            "id": device_id, "flow": flow,
        })

    result = resp.json()
    server_token = result.get("token", "")
    turnstile_dx = result.get("turnstile", {}).get("dx", "")

    logger.info(f"Sentinel 响应: token={bool(server_token)}, turnstile_dx={bool(turnstile_dx)}")

    # Step 3: 组装完整 Sentinel Token
    sentinel = json.dumps({
        "p": pow_token,
        "t": turnstile_dx,
        "c": server_token,
        "id": device_id,
        "flow": flow,
    })
    logger.info("Sentinel Token 组装完成")
    return sentinel
