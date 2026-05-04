import asyncio
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from ..auth import CurrentUser
from .. import runner
from ..config_health import build_config_health, health_error_message

router = APIRouter(prefix="/api/run", tags=["run"])


class StartRequest(BaseModel):
    mode: str = Field(pattern="^(single|batch|self_dealer|daemon|free_register|free_backfill_rt)$")
    paypal: bool = True
    batch: int = 0
    workers: int = 3
    self_dealer: int = 0
    register_only: bool = False
    pay_only: bool = False
    gopay: bool = False
    count: int = 0  # free_register mode registration count (0 = unlimited)


class OTPRequest(BaseModel):
    otp: str = Field(min_length=4, max_length=12)


@router.get("/status")
def get_status(user: str = CurrentUser):
    return runner.status()


@router.post("/start")
def start(req: StartRequest, user: str = CurrentUser):
    if req.mode == "batch" and req.batch < 1:
        raise HTTPException(status_code=400, detail="batch mode requires batch count >= 1")
    if req.mode == "self_dealer" and req.self_dealer < 1:
        raise HTTPException(status_code=400, detail="self_dealer mode requires member count >= 1")
    health = build_config_health(req.model_dump())
    if not health.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": health_error_message(health) or "Config health check failed",
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
    """SSE: check/push new log lines every 300ms."""
    last_seq = 0

    async def gen():
        nonlocal last_seq
        # Backlog: push recent 200 lines first
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
                # Process exited, scan once more to ensure no misses, then send done
                tail = runner.get_lines_since(last_seq, limit=500)
                for entry in tail:
                    last_seq = entry["seq"]
                    yield {"event": "line", "data": json.dumps(entry)}
                yield {"event": "done", "data": json.dumps(st)}
                break

    return EventSourceResponse(gen())


@router.post("/preview")
def preview(req: StartRequest, user: str = CurrentUser):
    """Dry-run: return command line without actually starting."""
    cmd = runner.build_cmd(
        req.mode, req.paypal, req.batch, req.workers, req.self_dealer,
        req.register_only, req.pay_only, gopay=req.gopay, count=req.count,
    )
    return {"cmd": cmd, "cmd_str": " ".join(cmd)}
