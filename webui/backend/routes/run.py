import asyncio
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from ..auth import CurrentUser
from .. import runner
from ..config_health import build_config_health, health_error_message

router = APIRouter(prefix="/api/run", tags=["run"])


class StartRequest(BaseModel):
    mode: str = Field(pattern="^(single|batch|self_dealer|daemon|free_register|free_backfill_rt|promo_link|no_card_plus)$")
    paypal: bool = True
    batch: int = 0
    workers: int = 3
    self_dealer: int = 0
    register_only: bool = False
    pay_only: bool = False
    gopay: bool = False
    qris: bool = False
    count: int = 0  # free_register 模式下注册次数（0 = 无限）
    # promo_link 模式：自由选择 checkout billing 区域 / 币种 / 活动
    promo_plan: str = Field(default="plus", pattern="^(plus|team)$")
    promo_country: str = Field(default="ID", min_length=2, max_length=2)
    promo_currency: str = Field(default="IDR", min_length=3, max_length=3)
    promo_campaign_id: str = ""
    register_mode: str = Field(default="protocol", pattern="^(browser|protocol)$")
    # 选中账号定向操作：配合 pay_only 或 rt_only
    target_emails: list[str] = []
    rt_only: bool = False
    # 邮箱来源 (二选一, 严格互斥, 不 fallback):
    # - outlook   : Outlook 接码池 (4 段格式 import 到 /outlook 页), IMAP OAuth2 收 OTP
    # - catch_all : 自有域名 catch-all + CF Email Worker → KV 收 OTP, persona 算法生成 alias
    mail_source: str = Field(default="outlook", pattern="^(outlook|catch_all)$")
    # 仅在 mail_source=outlook 时生效, 空 = 池里随便挑, 具体 email = 指定
    outlook_email: str = ""
    # no_card_plus 模式: 调 scripts/no_card_paypal_plus.py 用 Chromium RPA 走 PayPal 0 元开 Plus
    no_card_promo_link_id: int = 0  # 0 = 自动挑最新 fresh plus link
    no_card_phone: str = ""
    no_card_sms_api_url: str = ""  # 接码网关 URL+key, 走 form/env 不进 ps cmdline
    no_card_otp_timeout: int = 240
    no_card_signup_retries: int = 3
    no_card_node_rpa_timeout: int = 900
    no_card_max_due: int = 100
    no_card_allow_already_paid: bool = False
    no_card_allow_full_price: bool = False
    no_card_paypal_country: str = Field(default="US", min_length=2, max_length=2)
    no_card_paypal_lang: str = Field(default="en", min_length=2, max_length=5)
    # auto-gen promo_link 时挑库存账号的邮箱来源过滤
    # - any       : 不限
    # - outlook   : 只挑 microsoft 系 (@outlook/@hotmail/@live/@msn)
    # - catch_all : 只挑 CTF-reg config 里 catch_all_domain(s) 的 alias 账号
    no_card_inventory_mail_source: str = Field(
        default="any", pattern="^(any|outlook|catch_all)$"
    )


class OTPRequest(BaseModel):
    otp: str = Field(min_length=4, max_length=12)


@router.get("/status")
def get_status(user: str = CurrentUser):
    return runner.status()


@router.post("/start")
def start(req: StartRequest, user: str = CurrentUser):
    if req.mode == "batch" and req.batch < 1:
        raise HTTPException(status_code=400, detail="batch 模式下批次数必须 ≥ 1")
    if req.mode == "self_dealer" and req.self_dealer < 1:
        raise HTTPException(status_code=400, detail="self_dealer 模式下成员数必须 ≥ 1")
    if req.mode == "no_card_plus_parallel":
        raise HTTPException(
            status_code=400,
            detail="no_card_plus_parallel 模式请用 /api/run/parallel/start，而不是 /api/run/start（前者跳过单 run 健康检查、按 phone 池 + 并发数 N 启动）",
        )
    health = build_config_health(req.model_dump())
    if not health.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": health_error_message(health) or "配置健康检查未通过",
                "health": health,
            },
        )
    try:
        return runner.start(**req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/stop")
def stop(user: str = CurrentUser):
    return runner.stop()


@router.post("/otp")
def submit_otp(req: OTPRequest, user: str = CurrentUser):
    try:
        return runner.submit_otp(req.otp)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/logs")
def get_logs(tail: int = 500, user: str = CurrentUser):
    return {"lines": runner.get_tail(tail)}


@router.get("/stream")
async def stream(user: str = CurrentUser):
    """SSE: 每 300ms 检查 / 推送新日志行。"""
    last_seq = 0

    async def gen():
        nonlocal last_seq
        # Backlog: 先推最近 200 行
        for entry in runner.get_tail(200):
            last_seq = max(last_seq, entry["seq"])
            yield {"event": "line", "data": json.dumps(entry)}
        # Live
        while True:
            await asyncio.sleep(0.3)
            new_lines = runner.get_lines_since(last_seq, limit=500)
            for entry in new_lines:
                last_seq = entry["seq"]
                yield {"event": "line", "data": json.dumps(entry)}
            st = runner.status()
            # OTP heartbeat: re-send periodically while pending
            if st.get("otp_pending"):
                yield {"event": "otp_pending", "data": json.dumps({"pending": True})}
            if not st["running"]:
                # 进程已退出，再扫一次确保没遗漏，然后发 done
                tail = runner.get_lines_since(last_seq, limit=500)
                for entry in tail:
                    last_seq = entry["seq"]
                    yield {"event": "line", "data": json.dumps(entry)}
                yield {"event": "done", "data": json.dumps(st)}
                break

    return EventSourceResponse(gen())


@router.post("/preview")
def preview(req: StartRequest, user: str = CurrentUser):
    """干跑：只返命令行不实际启动。"""
    cmd = runner.build_cmd(
        req.mode, req.paypal, req.batch, req.workers, req.self_dealer,
        req.register_only, req.pay_only, gopay=req.gopay, qris=req.qris,
        count=req.count,
        promo_plan=req.promo_plan,
        promo_country=req.promo_country,
        promo_currency=req.promo_currency,
        promo_campaign_id=req.promo_campaign_id,
    )
    return {"cmd": cmd, "cmd_str": " ".join(cmd)}


# QRIS：前端轮询拿当前 QR artifacts + PNG bytes
@router.get("/qris/state")
def qris_state(user: str = CurrentUser):
    """返回当前/最近一次 QRIS run 的 reference / 远端 URL / 过期时间 / settled。"""
    return runner.qris_state()


@router.get("/qris/qr.png")
def qris_qr_png(user: str = CurrentUser):
    """返回 QR PNG bytes。前端直接当 <img src> 用。"""
    data = runner.qris_png_bytes()
    if not data:
        raise HTTPException(status_code=404, detail="no QR yet")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "no-store"})
