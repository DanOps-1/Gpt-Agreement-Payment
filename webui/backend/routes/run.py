import asyncio
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from ..auth import CurrentUser
from .. import runner

router = APIRouter(prefix="/api/run", tags=["run"])


class StartRequest(BaseModel):
    mode: str = Field(pattern="^(single|batch|self_dealer|daemon)$")
    paypal: bool = True
    batch: int = 0
    workers: int = 3
    self_dealer: int = 0
    register_only: bool = False
    pay_only: bool = False
    gopay: bool = False


class OTPRequest(BaseModel):
    otp: str = Field(min_length=4, max_length=12)


@router.get("/status")
def get_status(user: str = CurrentUser):
    return runner.status()


@router.post("/start")
def start(req: StartRequest, user: str = CurrentUser):
    try:
        return runner.start(**req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/stop")
def stop(user: str = CurrentUser):
    return runner.stop()


@router.post("/cleanup")
def cleanup(user: str = CurrentUser):
    """手工触发：杀残留 Xvfb/Camoufox/playwright + 清 /tmp 临时 profile。
    pipeline 还在跑时只清孤儿，不影响活跃进程。"""
    return runner.cleanup_orphans()


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
        prev_pending = False
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
            cur_pending = bool(st.get("otp_pending"))
            # 状态翻转才发：避免心跳轰炸；False→True 开模态，True→False 关模态
            if cur_pending and not prev_pending:
                yield {"event": "otp_pending", "data": json.dumps({"pending": True})}
            elif prev_pending and not cur_pending:
                yield {"event": "otp_resolved", "data": json.dumps({"pending": False})}
            prev_pending = cur_pending
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
        req.register_only, req.pay_only, gopay=req.gopay,
    )
    return {"cmd": cmd, "cmd_str": " ".join(cmd)}
