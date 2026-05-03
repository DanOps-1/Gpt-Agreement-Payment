from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..db import get_db

router = APIRouter(prefix="/api/wizard", tags=["wizard"])


class WizardState(BaseModel):
    current_step: int = 1
    answers: dict = Field(default_factory=dict)


def _read() -> WizardState:
    data = get_db().get_runtime_json("wizard_state", {})
    if not isinstance(data, dict):
        return WizardState()
    try:
        return WizardState(**data)
    except Exception:
        return WizardState()


def _write(state: WizardState) -> None:
    get_db().set_runtime_json("wizard_state", state.model_dump())


@router.get("/state")
def get_state(user: str = CurrentUser):
    return _read()


@router.post("/state")
def set_state(state: WizardState, user: str = CurrentUser):
    _write(state)
    return {"ok": True}
