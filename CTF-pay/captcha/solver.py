#!/usr/bin/env python3
"""Auto-solve the local Stripe hCaptcha bridge page.

当前主要覆盖两类在 Stripe challenge 中实际观察到的题型：

1. `Tap on each vehicle that is made for water travel`
   - 使用 CLIP 做 3x3 图片分类，点击船/水上交通工具。

2. `Please drag the object to complete the pair`
   - 使用 OpenCV/颜色聚类定位右侧 source object；
   - 在左侧找到缺失 source object 的骨架图形；
   - 通过与完整图形做 skeleton 匹配，推算目标落点并拖拽。

用法：
    ~/.venvs/ctfml/bin/python hcaptcha_auto_solver.py http://127.0.0.1:PORT/index.html
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import itertools
import json
import math
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
import torch
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from transformers import CLIPModel, CLIPProcessor

MODEL_ID = "openai/clip-vit-base-patch32"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_LOCALE = "en-US"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_VLM_ENABLED = True
DEFAULT_VLM_BASE_URL = os.environ.get("CTF_VLM_BASE_URL", "https://api.openai.com/v1")
DEFAULT_VLM_API_KEY = os.environ.get("CTF_VLM_API_KEY", "")
DEFAULT_VLM_MODEL = os.environ.get("CTF_VLM_MODEL", "gpt-4o")
_CLIP_MODEL: CLIPModel | None = None
_CLIP_PROCESSOR: CLIPProcessor | None = None


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _load_clip() -> tuple[CLIPModel, CLIPProcessor]:
    global _CLIP_MODEL, _CLIP_PROCESSOR
    if _CLIP_MODEL is None or _CLIP_PROCESSOR is None:
        log(f"加载 CLIP 模型: {MODEL_ID}")
        _CLIP_MODEL = CLIPModel.from_pretrained(MODEL_ID)
        _CLIP_PROCESSOR = CLIPProcessor.from_pretrained(MODEL_ID)
        _CLIP_MODEL.eval()
    return _CLIP_MODEL, _CLIP_PROCESSOR


@dataclass
class VisibleCanvas:
    full: np.ndarray
    visible: np.ndarray
    y_offset: int


@dataclass
class SolverAction:
    kind: str
    prompt: str
    click_points: list[tuple[float, float]] | None = None
    drag_from: tuple[float, float] | None = None
    drag_to: tuple[float, float] | None = None
    debug: dict | None = None


@dataclass
class CandidateBox:
    id: str
    box: tuple[int, int, int, int]
    center: tuple[float, float]
    kind: str = "click"
    score: float | None = None


def _png_data_url(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise RuntimeError("VLM 返回为空")
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S)
    if fence:
        raw = fence.group(1).strip()
    if raw.startswith("{") and raw.endswith("}"):
        return json.loads(raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return json.loads(raw[start : end + 1])
    raise RuntimeError(f"VLM 返回不是 JSON: {raw[:300]}")


def _normalize_api_base(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        raise RuntimeError("VLM base_url 为空")
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def _merge_overlapping_boxes(
    boxes: list[tuple[int, int, int, int]],
    *,
    min_iou: float = 0.25,
    max_center_distance: float = 40.0,
) -> list[tuple[int, int, int, int]]:
    merged: list[tuple[int, int, int, int]] = []
    for box in boxes:
        x, y, w, h = box
        cx = x + w / 2.0
        cy = y + h / 2.0
        placed = False
        for idx, existing in enumerate(merged):
            ex, ey, ew, eh = existing
            ecx = ex + ew / 2.0
            ecy = ey + eh / 2.0
            if (
                _box_iou((x, y, x + w, y + h), (ex, ey, ex + ew, ey + eh)) >= min_iou
                or math.hypot(cx - ecx, cy - ecy) <= max_center_distance
            ):
                nx0 = min(x, ex)
                ny0 = min(y, ey)
                nx1 = max(x + w, ex + ew)
                ny1 = max(y + h, ey + eh)
                merged[idx] = (int(nx0), int(ny0), int(nx1 - nx0), int(ny1 - ny0))
                placed = True
                break
        if not placed:
            merged.append((int(x), int(y), int(w), int(h)))
    return merged


def _build_object_candidates_generic(arr: np.ndarray) -> tuple[list[CandidateBox], dict]:
    visible = extract_visible_canvas(arr)
    vis = visible.visible
    h, w = vis.shape[:2]
    gray = cv2.cvtColor(vis, cv2.COLOR_RGB2GRAY)
    sat = vis.max(axis=2) - vis.min(axis=2)
    edges = cv2.Canny(gray, 45, 150)
    texture = ((sat > 18) | (gray < 242)).astype(np.uint8)
    mask = ((edges > 0) | (texture > 0)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    raw_boxes: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        area = int(area)
        if area < 1200:
            continue
        if bw < 40 or bh < 40:
            continue
        if bw >= int(w * 0.72) or bh >= int(h * 0.72):
            continue
        raw_boxes.append((int(x), int(y), int(bw), int(bh)))

    merged_boxes = _merge_overlapping_boxes(raw_boxes)
    merged_boxes = sorted(merged_boxes, key=lambda b: (b[1], b[0], -(b[2] * b[3])))
    merged_boxes = merged_boxes[:12]
    candidates = [
        CandidateBox(
            id=f"O{i+1}",
            box=box,
            center=(float(box[0] + box[2] / 2.0), float(visible.y_offset + box[1] + box[3] / 2.0)),
            kind="object",
        )
        for i, box in enumerate(merged_boxes)
    ]
    debug = {
        "mode": "generic_object_candidates",
        "visible_y_offset": visible.y_offset,
        "raw_box_count": len(raw_boxes),
        "merged_box_count": len(merged_boxes),
        "boxes": [list(b) for b in merged_boxes],
    }
    return candidates, debug


def _build_grid_candidates(arr: np.ndarray) -> tuple[list[CandidateBox], dict]:
    visible = extract_visible_canvas(arr)
    cols, rows = detect_label_grid(visible.visible)
    candidates: list[CandidateBox] = []
    idx = 1
    for y0, y1 in rows:
        for x0, x1 in cols:
            box = (int(x0), int(visible.y_offset + y0), int(x1 - x0 + 1), int(y1 - y0 + 1))
            candidates.append(
                CandidateBox(
                    id=f"G{idx}",
                    box=box,
                    center=(float((x0 + x1) / 2.0), float(visible.y_offset + (y0 + y1) / 2.0)),
                    kind="grid",
                )
            )
            idx += 1
    debug = {
        "mode": "grid_candidates",
        "rows": rows,
        "cols": cols,
        "visible_y_offset": visible.y_offset,
    }
    return candidates, debug


def _build_click_candidates(arr: np.ndarray) -> tuple[list[CandidateBox], np.ndarray, dict]:
    visible = extract_visible_canvas(arr)
    cols, rows = detect_label_grid(visible.visible)
    if _looks_like_full_grid(cols, rows, visible.visible):
        candidates, debug = _build_grid_candidates(arr)
        return candidates, visible.full, debug
    candidates, debug = _build_object_candidates_generic(arr)
    return candidates, visible.full, debug


def _draw_candidate_overlay(
    arr: np.ndarray,
    candidates: list[CandidateBox],
    *,
    title: str = "",
) -> np.ndarray:
    img = Image.fromarray(arr.copy())
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    if title:
        draw.rectangle((0, 0, img.width, 18), fill=(0, 0, 0))
        draw.text((4, 3), title[:100], fill=(255, 255, 255), font=font)
    palette = [
        (255, 99, 71),
        (0, 191, 255),
        (255, 215, 0),
        (50, 205, 50),
        (186, 85, 211),
        (255, 140, 0),
    ]
    for idx, cand in enumerate(candidates):
        color = palette[idx % len(palette)]
        x, y, w, h = cand.box
        draw.rectangle((x, y, x + w, y + h), outline=color, width=3)
        label = cand.id
        tx = x + 3
        ty = max(0, y - 14)
        tw = 7 * max(2, len(label)) + 8
        draw.rectangle((tx - 2, ty - 1, tx + tw, ty + 12), fill=color)
        draw.text((tx, ty), label, fill=(0, 0, 0), font=font)
    return np.array(img)


def _candidate_metadata(candidates: list[CandidateBox], full_shape: tuple[int, int, int]) -> list[dict[str, Any]]:
    full_h, full_w = full_shape[:2]
    meta: list[dict[str, Any]] = []
    for cand in candidates:
        x, y, w, h = cand.box
        cx, cy = cand.center
        meta.append(
            {
                "id": cand.id,
                "kind": cand.kind,
                "box": [int(x), int(y), int(w), int(h)],
                "center": [round(float(cx), 2), round(float(cy), 2)],
                "normalized_center": [round(float(cx) / max(full_w, 1), 4), round(float(cy) / max(full_h, 1), 4)],
            }
        )
    return meta


def _vlm_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_text: str,
    images: list[np.ndarray],
    timeout_s: int = 45,
) -> dict[str, Any]:
    endpoint = _normalize_api_base(base_url) + "/chat/completions"
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for arr in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _png_data_url(arr),
                    "detail": "high",
                },
            }
        )
    # Claude 模型不支持 response_format，改用 prompt 约束 JSON 输出
    _is_claude = "claude" in model.lower()
    if _is_claude:
        system_prompt = system_prompt.rstrip() + (
            "\n\nIMPORTANT: You MUST respond with ONLY a valid JSON object. "
            "No explanation, no markdown, no text before or after the JSON. "
            "Start your response with '{' and end with '}'."
        )
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "max_tokens": 1024,
    }
    if not _is_claude:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # 对 Claude 模型：首次失败后用更强约束重试一次
    for _attempt in range(2 if _is_claude else 1):
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"VLM 无 choices: {json.dumps(data, ensure_ascii=False)[:500]}")
        message = choices[0].get("message") or {}
        raw_content = message.get("content")
        if isinstance(raw_content, list):
            text = "".join(str(item.get("text") or "") for item in raw_content if isinstance(item, dict))
        else:
            text = str(raw_content or "")
        try:
            decision = _extract_json_object(text)
            decision["_raw_text"] = text
            return decision
        except RuntimeError:
            if _attempt == 0 and _is_claude:
                # 重试：在 user message 末尾追加强制 JSON 提示
                retry_msg = {"role": "user", "content": "You did not return valid JSON. Reply with ONLY a JSON object, nothing else. Start with {"}
                payload["messages"] = list(payload["messages"]) + [
                    {"role": "assistant", "content": text[:200]},
                    retry_msg,
                ]
                log(f"VLM 首次返回非 JSON，重试 ...")
                continue
            raise


def _vlm_click_decision(
    prompt: str,
    arr: np.ndarray,
    candidates: list[CandidateBox],
    *,
    submit_text: str,
    vlm_cfg: dict[str, Any],
) -> dict[str, Any]:
    overlay = _draw_candidate_overlay(arr, candidates, title="Click candidates")
    candidate_meta = _candidate_metadata(candidates, arr.shape)
    system_prompt = (
        "你是一个验证码视觉决策器。"
        "用户会给你 challenge prompt、原始图片、带候选框编号的图片，以及候选框元数据。"
        "你的任务是只根据图片内容和 prompt，判断应该点击哪些候选框。"
        "只返回 JSON，不要输出任何额外解释。"
        "JSON 结构必须是: "
        "{\"action\":\"click\",\"selected_ids\":[\"ID1\",\"ID2\"],\"confidence\":0.0,\"reasoning\":\"...\"}。"
        "如果是单选题，也仍然返回 selected_ids，只包含一个元素。"
        "不要返回候选框之外的坐标。"
    )
    user_text = (
        f"Prompt: {prompt}\n"
        f"Submit button text: {submit_text or ''}\n"
        f"Candidates JSON:\n{json.dumps(candidate_meta, ensure_ascii=False)}\n"
        "请输出应该点击的 candidate ids。"
    )
    decision = _vlm_chat_completion(
        base_url=str(vlm_cfg.get('base_url') or DEFAULT_VLM_BASE_URL),
        api_key=str(vlm_cfg.get('api_key') or DEFAULT_VLM_API_KEY),
        model=str(vlm_cfg.get('model') or DEFAULT_VLM_MODEL),
        system_prompt=system_prompt,
        user_text=user_text,
        images=[arr, overlay],
        timeout_s=int(vlm_cfg.get("timeout_s") or 45),
    )
    decision["_overlay"] = overlay
    decision["_candidate_meta"] = candidate_meta
    return decision


def _vlm_drag_decision_missing_piece(
    prompt: str,
    arr: np.ndarray,
    *,
    submit_text: str,
    vlm_cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    heuristic = solve_missing_pieces_drag(arr, prompt)
    debug = heuristic.debug or {}
    visible_y_offset = float(debug.get("visible_y_offset") or 0.0)
    sources: list[CandidateBox] = []
    targets: list[CandidateBox] = []
    for idx, piece in enumerate(debug.get("pieces") or []):
        box = piece.get("box") or [0, 0, 0, 0]
        x, y, w, h = map(int, box)
        sources.append(
            CandidateBox(
                id=f"S{idx+1}",
                box=(x + int(debug.get("split") or 0), int(visible_y_offset + y), w, h),
                center=(float((x + w / 2.0) + float(debug.get("split") or 0)), float(visible_y_offset + y + h / 2.0)),
                kind="source",
            )
        )
    for idx, hole in enumerate(debug.get("holes") or []):
        box = hole.get("box") or [0, 0, 0, 0]
        x, y, w, h = map(int, box)
        targets.append(
            CandidateBox(
                id=f"T{idx+1}",
                box=(x, int(visible_y_offset + y), w, h),
                center=(float(x + w / 2.0), float(visible_y_offset + y + h / 2.0)),
                kind="target",
            )
        )
    overlay = _draw_candidate_overlay(arr, sources + targets, title="Drag candidates")
    meta = _candidate_metadata(sources + targets, arr.shape)
    system_prompt = (
        "你是一个验证码视觉决策器。"
        "根据原图和带编号候选框的图，判断应该把哪个 source 拖到哪个 target。"
        "只返回 JSON，不要输出任何额外解释。"
        "JSON 结构必须是: "
        "{\"action\":\"drag\",\"source_id\":\"S1\",\"target_id\":\"T2\",\"confidence\":0.0,\"reasoning\":\"...\"}"
    )
    user_text = (
        f"Prompt: {prompt}\n"
        f"Submit button text: {submit_text or ''}\n"
        f"Candidates JSON:\n{json.dumps(meta, ensure_ascii=False)}\n"
        "请选择一个 source_id 和一个 target_id。"
    )
    decision = _vlm_chat_completion(
        base_url=str(vlm_cfg.get('base_url') or DEFAULT_VLM_BASE_URL),
        api_key=str(vlm_cfg.get('api_key') or DEFAULT_VLM_API_KEY),
        model=str(vlm_cfg.get('model') or DEFAULT_VLM_MODEL),
        system_prompt=system_prompt,
        user_text=user_text,
        images=[arr, overlay],
        timeout_s=int(vlm_cfg.get("timeout_s") or 45),
    )
    decision["_overlay"] = overlay
    decision["_candidate_meta"] = meta
    decision["_heuristic_debug"] = debug
    return decision, {"sources": sources, "targets": targets, "heuristic": heuristic}


def _candidate_map(candidates: list[CandidateBox]) -> dict[str, CandidateBox]:
    return {str(cand.id).strip().upper(): cand for cand in candidates}


def _is_missing_piece_style_prompt(prompt: str) -> bool:
    p = (prompt or "").lower()
    return (
        "missing piece" in p
        or "missing pieces" in p
        or "complete the image" in p
    )


def _build_candidate_click_sets(
    candidates: list[CandidateBox],
    selected_ids: list[str],
    *,
    max_sets: int = 12,
) -> list[list[tuple[float, float]]]:
    cand_map = _candidate_map(candidates)
    ordered_ids = [cand.id.upper() for cand in candidates]
    preferred = [cid for cid in selected_ids if cid in cand_map]
    if not preferred:
        return []

    click_sets: list[list[tuple[float, float]]] = []
    seen: set[tuple[str, ...]] = set()

    def _append(ids: list[str]):
        normalized = tuple(ids)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        click_sets.append([cand_map[cid].center for cid in normalized])

    _append(preferred)

    k = len(preferred)
    if k == 1:
        for cid in ordered_ids:
            _append([cid])
            if len(click_sets) >= max_sets:
                break
        return click_sets

    if 1 < k <= 3 and len(ordered_ids) <= 9:
        for combo in itertools.combinations(ordered_ids, k):
            _append(list(combo))
            if len(click_sets) >= max_sets:
                break

    return click_sets[:max_sets]


def _build_vlm_click_action(
    prompt: str,
    arr: np.ndarray,
    *,
    submit_text: str,
    vlm_cfg: dict[str, Any],
) -> SolverAction:
    candidates, full_arr, candidate_debug = _build_click_candidates(arr)
    if not candidates:
        raise RuntimeError("VLM 未识别到可点击候选框")

    decision = _vlm_click_decision(
        prompt,
        full_arr,
        candidates,
        submit_text=submit_text,
        vlm_cfg=vlm_cfg,
    )
    raw_selected = decision.get("selected_ids") or []
    if isinstance(raw_selected, str):
        raw_selected = [raw_selected]
    if not isinstance(raw_selected, list):
        raise RuntimeError(f"VLM selected_ids 类型非法: {type(raw_selected).__name__}")

    cand_map = _candidate_map(candidates)
    selected_ids: list[str] = []
    for raw in raw_selected:
        cid = str(raw or "").strip().upper()
        if cid and cid in cand_map and cid not in selected_ids:
            selected_ids.append(cid)
    if not selected_ids:
        raise RuntimeError(f"VLM 未返回有效 selected_ids: {raw_selected!r}")

    click_points = [cand_map[cid].center for cid in selected_ids]
    debug = {
        "mode": "vlm_click",
        "candidate_builder": candidate_debug,
        "candidate_meta": decision.get("_candidate_meta") or [],
        "vlm_overlay": decision.get("_overlay"),
        "vlm_decision": {k: v for k, v in decision.items() if k != "_overlay"},
        "selected_ids": selected_ids,
        "candidate_click_sets": _build_candidate_click_sets(candidates, selected_ids),
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def _build_direct_click_set_variations(
    points: list[tuple[float, float]],
    *,
    full_shape: tuple[int, int, int],
) -> list[list[tuple[float, float]]]:
    h, w = full_shape[:2]
    variations: list[list[tuple[float, float]]] = []
    deltas = [
        (0.0, 0.0),
        (-8.0, 0.0),
        (8.0, 0.0),
        (0.0, -8.0),
        (0.0, 8.0),
        (-6.0, -6.0),
        (6.0, 6.0),
    ]
    for dx, dy in deltas:
        variant = []
        for x, y in points:
            variant.append((
                min(max(float(x) + dx, 1.0), max(1.0, float(w) - 2.0)),
                min(max(float(y) + dy, 1.0), max(1.0, float(h) - 2.0)),
            ))
        _append_unique_click_set(variations, variant)
    return variations


def _build_vlm_direct_click_action(
    prompt: str,
    arr: np.ndarray,
    *,
    submit_text: str,
    vlm_cfg: dict[str, Any],
    expected_count: int | None = None,
) -> SolverAction:
    height, width = arr.shape[:2]
    system_prompt = (
        "你是一个视觉点击坐标标注助手。"
        "用户会给你 challenge prompt 和题图。"
        "你的任务是根据当前截图状态，返回‘下一步需要点击哪些位置’。"
        "注意：某些题图里已经被选中的对象会显示明显的圆圈/叉号/关闭图标覆盖层；"
        "如果一个对象被错误选中，你应该返回它的中心点，让用户再次点击以取消选择。"
        "如果一个正确目标尚未被选中，你应该返回它的中心点，让用户点击选中。"
        "因此你返回的是‘纠正到正确最终状态所需的点击点’，而不只是目标列表。"
        "只输出 JSON，格式为 "
        "{\"action\":\"click\",\"click_points\":[[x,y],...],\"confidence\":0.0,\"reasoning\":\"...\"}。"
        "其中 x,y 默认使用 0 到 1 之间的归一化坐标，相对于整张图片宽高；"
        "如果你输出像素坐标，也必须确保落在图片范围内。"
    )
    count_hint = ""
    if expected_count and expected_count > 0:
        count_hint = (
            f"\nExpected final selected target count: {int(expected_count)}."
            " 这是最终正确选中目标的数量，不一定等于本轮需要点击的次数；"
            " 如果存在错误已选中对象，需要同时返回它们用于取消。"
        )
    user_text = (
        f"Prompt: {prompt}\n"
        f"Submit text: {submit_text}\n"
        f"Image size: width={width}, height={height}\n"
        f"{count_hint}\n"
        "请只选择真正匹配 prompt 的目标，避免背景或干扰物。"
    )
    decision = _vlm_chat_completion(
        base_url=str(vlm_cfg.get("base_url") or DEFAULT_VLM_BASE_URL),
        api_key=str(vlm_cfg.get("api_key") or DEFAULT_VLM_API_KEY),
        model=str(vlm_cfg.get("model") or DEFAULT_VLM_MODEL),
        system_prompt=system_prompt,
        user_text=user_text,
        images=[arr],
        timeout_s=int(vlm_cfg.get("timeout_s") or 45),
    )
    raw_points = decision.get("click_points")
    if not isinstance(raw_points, list) or not raw_points:
        raise RuntimeError(f"VLM 直出坐标失败: {raw_points!r}")
    click_points: list[tuple[float, float]] = []
    for item in raw_points:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            x = float(item[0])
            y = float(item[1])
        except Exception:
            continue
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            px = x * width
            py = y * height
        else:
            px = x
            py = y
        px = min(max(px, 1.0), max(1.0, float(width) - 2.0))
        py = min(max(py, 1.0), max(1.0, float(height) - 2.0))
        click_points.append((float(px), float(py)))
    if not click_points:
        raise RuntimeError("VLM 直出坐标未返回有效点位")
    debug = {
        "mode": "vlm_direct_click",
        "vlm_decision": decision,
        "expected_count": expected_count,
        "candidate_click_sets": _build_direct_click_set_variations(click_points, full_shape=arr.shape),
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def _build_vlm_direct_drag_action(
    prompt: str,
    arr: np.ndarray,
    *,
    submit_text: str,
    vlm_cfg: dict[str, Any],
) -> SolverAction:
    height, width = arr.shape[:2]
    system_prompt = (
        "你是一个验证码视觉拖拽定位助手。"
        "用户会给你 challenge prompt 和当前题图。"
        "请直接返回拖拽起点和终点坐标。"
        "只输出 JSON，格式为 "
        "{\"action\":\"drag\",\"drag_from\":[x,y],\"drag_to\":[x,y],\"confidence\":0.0,\"reasoning\":\"...\"}。"
        "其中 x,y 优先使用 0 到 1 之间的归一化坐标，相对于整张图片宽高；"
        "如果你输出像素坐标，也必须确保落在图片范围内。"
    )
    user_text = (
        f"Prompt: {prompt}\n"
        f"Submit text: {submit_text}\n"
        f"Image size: width={width}, height={height}\n"
        "请根据图中左右配对关系，给出一个最合理的拖拽起点和终点。"
    )
    decision = _vlm_chat_completion(
        base_url=str(vlm_cfg.get("base_url") or DEFAULT_VLM_BASE_URL),
        api_key=str(vlm_cfg.get("api_key") or DEFAULT_VLM_API_KEY),
        model=str(vlm_cfg.get("model") or DEFAULT_VLM_MODEL),
        system_prompt=system_prompt,
        user_text=user_text,
        images=[arr],
        timeout_s=int(vlm_cfg.get("timeout_s") or 45),
    )

    def _parse_point(raw: Any, *, name: str) -> tuple[float, float]:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise RuntimeError(f"VLM direct drag 返回非法 {name}: {raw!r}")
        try:
            x = float(raw[0])
            y = float(raw[1])
        except Exception as e:
            raise RuntimeError(f"VLM direct drag 解析 {name} 失败: {raw!r}") from e
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            px = x * width
            py = y * height
        else:
            px = x
            py = y
        px = min(max(px, 1.0), max(1.0, float(width) - 2.0))
        py = min(max(py, 1.0), max(1.0, float(height) - 2.0))
        return float(px), float(py)

    drag_from = _parse_point(decision.get("drag_from") or decision.get("source_point"), name="drag_from")
    drag_to = _parse_point(decision.get("drag_to") or decision.get("target_point"), name="drag_to")
    debug = {
        "mode": "vlm_direct_drag",
        "vlm_decision": decision,
    }
    return SolverAction(kind="drag", prompt=prompt, drag_from=drag_from, drag_to=drag_to, debug=debug)


def _build_vlm_drag_action(
    prompt: str,
    arr: np.ndarray,
    *,
    submit_text: str,
    vlm_cfg: dict[str, Any],
) -> SolverAction:
    if _is_missing_piece_style_prompt(prompt):
        decision, aux = _vlm_drag_decision_missing_piece(
            prompt,
            arr,
            submit_text=submit_text,
            vlm_cfg=vlm_cfg,
        )
        sources = list(aux.get("sources") or [])
        targets = list(aux.get("targets") or [])
        source_map = _candidate_map(sources)
        target_map = _candidate_map(targets)

        source_id = str(decision.get("source_id") or "").strip().upper()
        target_id = str(decision.get("target_id") or "").strip().upper()
        if source_id not in source_map or target_id not in target_map:
            raise RuntimeError(
                f"VLM drag 返回非法候选: source_id={source_id!r}, target_id={target_id!r}"
            )

        heuristic_action = aux.get("heuristic")
        heuristic_debug = {}
        if isinstance(heuristic_action, SolverAction):
            heuristic_debug = dict(heuristic_action.debug or {})

        debug = {
            **heuristic_debug,
            "mode": "vlm_drag_missing_piece",
            "candidate_meta": decision.get("_candidate_meta") or [],
            "vlm_overlay": decision.get("_overlay"),
            "vlm_decision": {k: v for k, v in decision.items() if k != "_overlay"},
            "selected_source_id": source_id,
            "selected_target_id": target_id,
        }
        return SolverAction(
            kind="drag",
            prompt=prompt,
            drag_from=source_map[source_id].center,
            drag_to=target_map[target_id].center,
            debug=debug,
        )

    heuristic_action = solve_pair_drag(arr, prompt)
    heuristic_debug = dict(heuristic_action.debug or {})
    components = list(heuristic_debug.get("components") or [])
    if len(components) < 2:
        raise RuntimeError("generic pair drag 候选组件不足")

    visible_y_offset = float(heuristic_debug.get("visible_y_offset") or 0.0)
    targets: list[CandidateBox] = []
    for idx, comp in enumerate(components):
        box = comp.get("box") or [0, 0, 0, 0]
        x, y, w, h = map(int, box)
        targets.append(
            CandidateBox(
                id=f"T{idx+1}",
                box=(x, int(visible_y_offset + y), w, h),
                center=(float(x + w / 2.0), float(visible_y_offset + y + h / 2.0)),
                kind="target",
            )
        )
    overlay = _draw_candidate_overlay(arr, targets, title="Pair drag targets")
    meta = _candidate_metadata(targets, arr.shape)
    system_prompt = (
        "你是一个验证码视觉决策器。"
        "图中右侧有一个可拖动的 source object，左侧有多个候选 target box。"
        "请选择应该接收 source object 的 target，并估计拖拽完成后 source object 的中心点应落在该 target box 内的位置。"
        "只返回 JSON，不要输出任何额外解释。"
        "JSON 结构必须是: "
        "{\"action\":\"drag\",\"target_id\":\"T1\",\"drop_point\":[0.5,0.5],\"confidence\":0.0,\"reasoning\":\"...\"}"
        "其中 drop_point 是相对于 target box 的归一化坐标，范围 0~1。"
    )
    user_text = (
        f"Prompt: {prompt}\n"
        f"Submit button text: {submit_text or ''}\n"
        f"当前 draggable source center: {list(map(lambda v: round(float(v), 2), heuristic_action.drag_from or (0.0, 0.0)))}\n"
        f"Candidates JSON:\n{json.dumps(meta, ensure_ascii=False)}\n"
        "请根据原图判断应拖到哪个 target，以及 source 中心应放在该 target box 的哪里。"
    )
    decision = _vlm_chat_completion(
        base_url=str(vlm_cfg.get("base_url") or DEFAULT_VLM_BASE_URL),
        api_key=str(vlm_cfg.get("api_key") or DEFAULT_VLM_API_KEY),
        model=str(vlm_cfg.get("model") or DEFAULT_VLM_MODEL),
        system_prompt=system_prompt,
        user_text=user_text,
        images=[arr, overlay],
        timeout_s=int(vlm_cfg.get("timeout_s") or 45),
    )
    target_map = _candidate_map(targets)
    target_id = str(decision.get("target_id") or "").strip().upper()
    if target_id not in target_map:
        raise RuntimeError(f"VLM pair drag 返回非法 target_id: {target_id!r}")
    drop_point = decision.get("drop_point") or decision.get("drop_point_norm") or []
    if not (isinstance(drop_point, list) and len(drop_point) == 2):
        raise RuntimeError(f"VLM pair drag 返回非法 drop_point: {drop_point!r}")
    try:
        rel_x = min(1.0, max(0.0, float(drop_point[0])))
        rel_y = min(1.0, max(0.0, float(drop_point[1])))
    except Exception as e:
        raise RuntimeError(f"VLM pair drag drop_point 解析失败: {drop_point!r}") from e
    target_box = target_map[target_id].box
    tx, ty, tw, th = target_box
    drag_to = (
        float(tx + rel_x * tw),
        float(ty + rel_y * th),
    )
    debug = {
        **heuristic_debug,
        "mode": "vlm_drag_pair_completion",
        "candidate_meta": meta,
        "vlm_overlay": overlay,
        "vlm_decision": {k: v for k, v in decision.items() if k != "_overlay"},
        "selected_target_id": target_id,
        "drop_point_norm": [rel_x, rel_y],
        "drag_to_vlm": drag_to,
        "heuristic_drag_to": heuristic_action.drag_to,
    }
    return SolverAction(
        kind="drag",
        prompt=prompt,
        drag_from=heuristic_action.drag_from,
        drag_to=drag_to,
        debug=debug,
    )

def _append_unique_point(points: list[tuple[float, float]], pt: tuple[float, float] | None):
    if pt is None:
        return
    x, y = float(pt[0]), float(pt[1])
    key = (round(x, 2), round(y, 2))
    if not any((round(px, 2), round(py, 2)) == key for px, py in points):
        points.append((x, y))


def build_drag_target_variations(action: SolverAction) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    _append_unique_point(points, action.drag_to)

    debug = action.debug or {}
    incomplete = debug.get("incomplete") or {}
    candidate_scores = debug.get("candidate_scores") or []
    visible_y_offset = float(debug.get("visible_y_offset") or 0.0)
    box = incomplete.get("box") or []
    if len(box) == 4:
        x, y, w, h = map(float, box)
        ranked = sorted(
            candidate_scores,
            key=lambda item: float(item.get("adjusted_score", item.get("raw_score", -1e9))),
            reverse=True,
        )
        for item in ranked:
            rel = item.get("rel")
            if not rel or len(rel) != 2:
                continue
            target = (
                x + float(rel[0]) * w,
                visible_y_offset + y + float(rel[1]) * h,
            )
            _append_unique_point(points, target)

    base_points = list(points)
    jitter_offsets = [
        (0.0, 0.0),
        (10.0, 0.0),
        (-10.0, 0.0),
        (0.0, 10.0),
        (0.0, -10.0),
        (16.0, 0.0),
        (-16.0, 0.0),
        (0.0, 16.0),
        (0.0, -16.0),
        (8.0, 8.0),
        (-8.0, 8.0),
        (8.0, -8.0),
        (-8.0, -8.0),
    ]
    for bx, by in base_points:
        for dx, dy in jitter_offsets:
            _append_unique_point(points, (bx + dx, by + dy))
    return points


def build_drag_start_variations(action: SolverAction) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    _append_unique_point(points, action.drag_from)

    debug = action.debug or {}
    box = debug.get("source_box") or []
    if len(box) == 4:
        x, y, w, h = map(float, box)
        anchor_points = [
            (0.50, 0.50),
            (0.35, 0.50),
            (0.65, 0.50),
            (0.50, 0.35),
            (0.50, 0.65),
            (0.35, 0.35),
            (0.65, 0.35),
            (0.35, 0.65),
            (0.65, 0.65),
        ]
        for rx, ry in anchor_points:
            _append_unique_point(points, (x + rx * w, y + ry * h))
    return points


def build_click_point_variations(base_points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    jitter_offsets = [
        (0.0, 0.0),
        (8.0, 0.0),
        (-8.0, 0.0),
        (0.0, 8.0),
        (0.0, -8.0),
    ]
    for bx, by in base_points:
        for dx, dy in jitter_offsets:
            _append_unique_point(points, (bx + dx, by + dy))
    return points


def _append_unique_click_set(
    click_sets: list[list[tuple[float, float]]],
    pts: list[tuple[float, float]] | tuple[tuple[float, float], ...] | None,
):
    if not pts:
        return
    normalized = [(float(x), float(y)) for x, y in pts]
    key = tuple(sorted((round(x, 2), round(y, 2)) for x, y in normalized))
    if not any(tuple(sorted((round(x, 2), round(y, 2)) for x, y in existing)) == key for existing in click_sets):
        click_sets.append(normalized)


def _clip_probs(tile_images: list[Image.Image], texts: list[str]) -> tuple[np.ndarray, list[str]]:
    model, processor = _load_clip()
    inputs = processor(text=texts, images=tile_images, return_tensors="pt", padding=True)
    with torch.no_grad():
        logits = model(**inputs).logits_per_image
    return logits.softmax(dim=1).cpu().numpy(), texts


def classify_tiles_binary_prompt_groups(
    tile_images: list[Image.Image],
    positive_texts: list[str],
    negative_texts: list[str],
) -> list[dict[str, float]]:
    probs, texts = _clip_probs(tile_images, positive_texts + negative_texts)
    positive_end = len(positive_texts)
    debug_rows: list[dict[str, float]] = []
    for row in probs:
        positive_scores = row[:positive_end]
        negative_scores = row[positive_end:]
        score_map = {texts[i]: float(row[i]) for i in range(len(texts))}
        score_map["positive_score"] = float(positive_scores.max()) if len(positive_scores) else 0.0
        score_map["negative_score"] = float(negative_scores.max()) if len(negative_scores) else 0.0
        score_map["margin"] = score_map["positive_score"] - score_map["negative_score"]
        debug_rows.append(score_map)
    return debug_rows


def build_candidate_click_sets(
    centers: list[tuple[float, float]],
    debug_rows: list[dict[str, float]],
    *,
    singular: bool = False,
    positive_threshold: float = 0.55,
) -> list[list[tuple[float, float]]]:
    ranked = sorted(
        range(len(debug_rows)),
        key=lambda i: (
            float(debug_rows[i].get("margin", -1e9)),
            float(debug_rows[i].get("positive_score", 0.0)),
        ),
        reverse=True,
    )
    strong = [
        i
        for i in ranked
        if float(debug_rows[i].get("positive_score", 0.0)) >= positive_threshold
        and float(debug_rows[i].get("positive_score", 0.0)) > float(debug_rows[i].get("negative_score", 0.0))
    ]
    medium = [
        i
        for i in ranked
        if float(debug_rows[i].get("positive_score", 0.0)) >= max(0.35, positive_threshold - 0.15)
    ]

    click_sets: list[list[tuple[float, float]]] = []
    if singular:
        for idx in ranked[: min(4, len(ranked))]:
            _append_unique_click_set(click_sets, [centers[idx]])
        if strong:
            _append_unique_click_set(click_sets, [centers[strong[0]]])
        return click_sets

    if strong:
        _append_unique_click_set(click_sets, [centers[i] for i in strong])
    if medium:
        _append_unique_click_set(click_sets, [centers[i] for i in medium[: min(3, len(medium))]])
    if ranked:
        _append_unique_click_set(click_sets, [centers[ranked[0]]])
    if len(ranked) >= 2:
        _append_unique_click_set(click_sets, [centers[i] for i in ranked[:2]])
    if len(ranked) >= 3:
        _append_unique_click_set(click_sets, [centers[i] for i in ranked[:3]])
    return click_sets


def decode_canvas_data_url(data_url: str) -> np.ndarray:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return np.array(Image.open(io.BytesIO(raw)).convert("RGB"))


def extract_visible_canvas(arr: np.ndarray) -> VisibleCanvas:
    row_mean = arr.mean(axis=2).mean(axis=1)
    rows = np.where(row_mean > 30)[0]
    if len(rows) == 0:
        raise RuntimeError("canvas 中未检测到可见区域")
    y0 = int(rows.min())
    return VisibleCanvas(full=arr, visible=arr[y0:], y_offset=y0)


def _runs_above_threshold(arr: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    idx = np.where(arr > threshold)[0]
    if len(idx) == 0:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = int(idx[0])
    for cur in idx[1:]:
        cur = int(cur)
        if cur == prev + 1:
            prev = cur
            continue
        runs.append((start, prev))
        start = prev = cur
    runs.append((start, prev))
    return runs


def _pick_top_runs(arr: np.ndarray, count: int) -> list[tuple[int, int]]:
    runs = _runs_above_threshold(arr, float(arr.mean()))
    runs = sorted(runs, key=lambda r: (r[1] - r[0]), reverse=True)[:count]
    runs = sorted(runs)
    if len(runs) != count:
        raise RuntimeError(f"未能稳定识别 {count} 个内容带，当前只有 {len(runs)} 个")
    return runs


def _pick_top_runs_with_thresholds(
    arr: np.ndarray,
    count: int,
    thresholds: tuple[float, ...] = (0.08, 0.05, 0.02),
) -> list[tuple[int, int]]:
    last_len = 0
    for threshold in thresholds:
        runs = _runs_above_threshold(arr, threshold)
        runs = sorted(runs, key=lambda r: (r[1] - r[0]), reverse=True)[:count]
        if len(runs) == count:
            return sorted(runs)
        last_len = len(runs)
    raise RuntimeError(f"未能稳定识别 {count} 个内容带，当前只有 {last_len} 个")


def _runs_are_balanced(runs: list[tuple[int, int]], *, min_ratio: float = 0.6) -> bool:
    if len(runs) < 2:
        return False
    sizes = sorted((r1 - r0 + 1) for r0, r1 in runs)
    if sizes[0] <= 0:
        return False
    return (sizes[0] / sizes[-1]) >= min_ratio


def _detect_label_grid_by_nonwhite(vis: np.ndarray) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    gray = vis.mean(axis=2)
    nonwhite = (gray < 245).astype(np.float32)
    cols = _pick_top_runs_with_thresholds(nonwhite.mean(axis=0), 3)
    rows = _pick_top_runs_with_thresholds(nonwhite.mean(axis=1), 3)
    return cols, rows


def detect_label_grid(vis: np.ndarray) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    std_cols = std_rows = None
    std_error: RuntimeError | None = None
    try:
        col_std = vis.std(axis=(0, 2))
        row_std = vis.std(axis=(1, 2))
        std_cols = _pick_top_runs(col_std, 3)
        std_rows = _pick_top_runs(row_std, 3)
        if _runs_are_balanced(std_cols) and _runs_are_balanced(std_rows):
            return std_cols, std_rows
    except RuntimeError as e:
        std_error = e

    try:
        fallback_cols, fallback_rows = _detect_label_grid_by_nonwhite(vis)
        if _runs_are_balanced(fallback_cols) and _runs_are_balanced(fallback_rows):
            return fallback_cols, fallback_rows
        if std_cols and std_rows:
            return std_cols, std_rows
    except RuntimeError:
        if std_cols and std_rows:
            return std_cols, std_rows
        if std_error is not None:
            raise std_error
        raise

    if std_cols and std_rows:
        return std_cols, std_rows
    if std_error is not None:
        raise std_error
    raise RuntimeError("未能稳定识别 3x3 challenge 网格")


def classify_tiles_water_travel(tile_images: list[Image.Image]) -> tuple[list[int], list[dict[str, float]]]:
    positive_texts = [
        "a boat on water",
        "a ship on the sea",
        "a ferry on water",
        "a speedboat on water",
        "a sailboat on the ocean",
        "a kayak on a lake",
        "a canoe on water",
        "a jet ski on water",
        "a personal watercraft on water",
        "a cruise ship at sea",
        "a cargo ship on water",
        "a tugboat on water",
    ]
    negative_texts = [
        "a fire truck on a road",
        "a bus in a field",
        "a cement mixer truck",
        "a car on a road",
        "a van on a road",
        "a delivery van",
        "a minibus on a road",
        "a sedan parked on land",
        "a pickup truck on a road",
        "a train on tracks",
        "an airplane on a runway",
    ]
    debug_rows = classify_tiles_binary_prompt_groups(tile_images, positive_texts, negative_texts)
    selected: list[int] = []
    for idx, score_map in enumerate(debug_rows):
        boat_score = float(score_map["positive_score"])
        land_score = float(score_map["negative_score"])
        score_map["boat_score"] = boat_score
        score_map["land_score"] = land_score
        if boat_score > land_score and boat_score >= 0.60:
            selected.append(idx)

    if not selected:
        ranked = sorted(range(len(debug_rows)), key=lambda i: debug_rows[i]["boat_score"] - debug_rows[i]["land_score"], reverse=True)
        selected = ranked[:2]
    return selected, debug_rows


def _classify_candidate_boxes(
    arr: np.ndarray,
    candidates: list[CandidateBox],
    *,
    positive_texts: list[str],
    negative_texts: list[str],
    padding: int = 8,
) -> tuple[list[CandidateBox], list[dict[str, float]]]:
    full_h, full_w = arr.shape[:2]
    kept: list[CandidateBox] = []
    crops: list[Image.Image] = []
    for cand in candidates:
        x, y, w, h = cand.box
        x0 = max(0, int(x) - padding)
        y0 = max(0, int(y) - padding)
        x1 = min(full_w, int(x + w) + padding)
        y1 = min(full_h, int(y + h) + padding)
        if x1 - x0 < 24 or y1 - y0 < 24:
            continue
        crop = arr[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        kept.append(cand)
        crops.append(Image.fromarray(crop))
    if not kept:
        return [], []
    scores = classify_tiles_binary_prompt_groups(crops, positive_texts, negative_texts)
    return kept, scores


def solve_water_travel(
    arr: np.ndarray,
    prompt: str,
    expected_count: int | None = None,
    prefer_object_candidates: bool = False,
) -> SolverAction:
    visible = extract_visible_canvas(arr)
    cols, rows = detect_label_grid(visible.visible)
    singular = "the vehicle" in prompt.lower() and "vehicles" not in prompt.lower()
    use_grid = (not prefer_object_candidates) and _looks_like_full_grid(cols, rows, visible.visible)
    candidate_debug: dict[str, Any] = {"prefer_object_candidates": prefer_object_candidates}
    overlay: np.ndarray | None = None
    meta: list[dict[str, Any]] | None = None

    if use_grid:
        tile_images: list[Image.Image] = []
        centers: list[tuple[float, float]] = []
        for y0, y1 in rows:
            for x0, x1 in cols:
                tile = visible.visible[y0:y1 + 1, x0:x1 + 1]
                tile_images.append(Image.fromarray(tile))
                centers.append(((x0 + x1) / 2.0, visible.y_offset + (y0 + y1) / 2.0))
        selected, scores = classify_tiles_water_travel(tile_images)
        candidate_click_sets = build_candidate_click_sets(
            centers,
            scores,
            singular=singular,
            positive_threshold=0.60,
        )
    else:
        candidates, candidate_debug = _build_object_candidates_generic(arr)
        positive_texts = [
            "a boat on water",
            "a ship on the sea",
            "a ferry on water",
            "a speedboat on water",
            "a sailboat on the ocean",
            "a kayak on a lake",
            "a canoe on water",
            "a jet ski on water",
            "a cruise ship at sea",
            "a cargo ship on water",
            "a tugboat on water",
        ]
        negative_texts = [
            "a fire truck on a road",
            "a bus in a field",
            "a cement mixer truck",
            "a car on a road",
            "a van on a road",
            "a minibus on a road",
            "a delivery van",
            "a pickup truck on a road",
            "a train on tracks",
            "an airplane on a runway",
        ]
        candidates, scores = _classify_candidate_boxes(
            arr,
            candidates,
            positive_texts=positive_texts,
            negative_texts=negative_texts,
            padding=10,
        )
        if not candidates:
            raise RuntimeError("未识别到可点击候选物体")
        centers = [cand.center for cand in candidates]
        for cand, score_map in zip(candidates, scores):
            cand.score = float(score_map.get("margin", 0.0))
            score_map["boat_score"] = float(score_map.get("positive_score", 0.0))
            score_map["land_score"] = float(score_map.get("negative_score", 0.0))
        selected = [
            idx for idx, score_map in enumerate(scores)
            if float(score_map.get("positive_score", 0.0)) >= 0.54
            and float(score_map.get("positive_score", 0.0)) > float(score_map.get("negative_score", 0.0))
        ]
        if not selected:
            ranked = sorted(
                range(len(scores)),
                key=lambda i: (
                    float(scores[i].get("margin", -1e9)),
                    float(scores[i].get("positive_score", 0.0)),
                ),
                reverse=True,
            )
            selected = ranked[: min(2, len(ranked))]
        candidate_click_sets = build_candidate_click_sets(
            centers,
            scores,
            singular=singular,
            positive_threshold=0.54,
        )
        overlay = _draw_candidate_overlay(arr, candidates, title="Water travel object candidates")
        meta = _candidate_metadata(candidates, arr.shape)

    expected_click_set: list[tuple[float, float]] = []
    if expected_count and expected_count > 0 and expected_count <= len(centers):
        ranked = sorted(
            range(len(scores)),
            key=lambda i: float(scores[i].get("boat_score", 0.0)) - float(scores[i].get("land_score", 0.0)),
            reverse=True,
        )
        expected_click_set = [centers[i] for i in ranked[:expected_count]]
        if expected_click_set:
            _append_unique_click_set(candidate_click_sets, expected_click_set)
            candidate_click_sets = [expected_click_set] + [
                click_set for click_set in candidate_click_sets
                if tuple(sorted((round(x, 2), round(y, 2)) for x, y in click_set))
                != tuple(sorted((round(x, 2), round(y, 2)) for x, y in expected_click_set))
            ]
    click_points = candidate_click_sets[0] if candidate_click_sets else [centers[i] for i in selected]
    debug = {
        "mode": "grid" if use_grid else "object_candidates",
        "rows": rows,
        "cols": cols,
        "selected": selected,
        "scores": scores,
        "singular": singular,
        "expected_count": expected_count,
        "expected_click_set": expected_click_set,
        "candidate_click_sets": candidate_click_sets,
        "candidate_debug": candidate_debug,
        "candidate_meta": meta,
        "candidate_overlay": overlay,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def solve_float_on_water(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    cols, rows = detect_label_grid(visible.visible)

    tile_images: list[Image.Image] = []
    centers: list[tuple[float, float]] = []
    for y0, y1 in rows:
        for x0, x1 in cols:
            tile = visible.visible[y0:y1 + 1, x0:x1 + 1]
            tile_images.append(Image.fromarray(tile))
            centers.append(((x0 + x1) / 2.0, visible.y_offset + (y0 + y1) / 2.0))

    scores = classify_tiles_binary_prompt_groups(
        tile_images,
        positive_texts=[
            "a rubber duck floating on water",
            "a toy duck that floats",
            "a pelican floating on water",
            "a duck swimming on water",
            "a bird floating on a lake",
            "an object floating on water",
        ],
        negative_texts=[
            "a bicycle crank",
            "a bicycle wheel",
            "a computer keyboard",
            "a metal bike part",
            "an object that sinks in water",
            "a land vehicle part",
        ],
    )
    candidate_click_sets = build_candidate_click_sets(centers, scores, singular=False, positive_threshold=0.44)
    click_points = candidate_click_sets[0] if candidate_click_sets else []
    debug = {
        "rows": rows,
        "cols": cols,
        "scores": scores,
        "candidate_click_sets": candidate_click_sets,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def solve_hot_food(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    cols, rows = detect_label_grid(visible.visible)

    tile_images: list[Image.Image] = []
    centers: list[tuple[float, float]] = []
    for y0, y1 in rows:
        for x0, x1 in cols:
            tile = visible.visible[y0:y1 + 1, x0:x1 + 1]
            tile_images.append(Image.fromarray(tile))
            centers.append(((x0 + x1) / 2.0, visible.y_offset + (y0 + y1) / 2.0))

    scores = classify_tiles_binary_prompt_groups(
        tile_images,
        positive_texts=[
            "a bowl of hot soup",
            "pizza served hot",
            "steaming noodles",
            "hot french fries",
            "a hot drink",
        ],
        negative_texts=[
            "ice cream",
            "cold salad",
            "cold fruit",
            "a glass of water",
            "a chilled dessert",
        ],
    )
    candidate_click_sets = build_candidate_click_sets(centers, scores, singular=False, positive_threshold=0.52)
    click_points = candidate_click_sets[0] if candidate_click_sets else []
    debug = {
        "rows": rows,
        "cols": cols,
        "scores": scores,
        "candidate_click_sets": candidate_click_sets,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def solve_heat_work(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    positive_texts = [
        "a radiator heater",
        "an electric heater",
        "a space heater",
        "a home radiator",
        "a hair dryer",
        "an iron for clothes",
        "a toaster",
        "an oven",
        "a stove burner",
        "a welding torch",
        "a heat gun",
        "a clothes dryer",
    ]
    negative_texts = [
        "a hard hat",
        "a ladder",
        "a desk lamp",
        "a construction helmet",
        "a bicycle",
        "a book",
        "a chair",
        "a clock",
        "a plant pot",
        "a pair of scissors",
    ]
    cols, rows = detect_label_grid(visible.visible)
    use_grid = _looks_like_full_grid(cols, rows, visible.visible)
    candidate_debug: dict[str, Any] = {}

    if use_grid:
        tile_images: list[Image.Image] = []
        centers: list[tuple[float, float]] = []
        for y0, y1 in rows:
            for x0, x1 in cols:
                tile = visible.visible[y0:y1 + 1, x0:x1 + 1]
                tile_images.append(Image.fromarray(tile))
                centers.append(((x0 + x1) / 2.0, visible.y_offset + (y0 + y1) / 2.0))
        scores = classify_tiles_binary_prompt_groups(tile_images, positive_texts, negative_texts)
    else:
        candidates, candidate_debug = _build_object_candidates_generic(arr)
        candidates, scores = _classify_candidate_boxes(
            arr, candidates,
            positive_texts=positive_texts,
            negative_texts=negative_texts,
            padding=10,
        )
        if not candidates:
            raise RuntimeError("未识别到 heat work 候选物体")
        centers = [cand.center for cand in candidates]
        for cand, score_map in zip(candidates, scores):
            cand.score = float(score_map.get("margin", 0.0))

    candidate_click_sets = build_candidate_click_sets(centers, scores, singular=False, positive_threshold=0.40)
    click_points = candidate_click_sets[0] if candidate_click_sets else []
    debug = {
        "mode": "grid" if use_grid else "object_candidates",
        "rows": rows,
        "cols": cols,
        "scores": scores,
        "candidate_click_sets": candidate_click_sets,
        "candidate_debug": candidate_debug,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def _runs_coverage_ratio(runs: list[tuple[int, int]], total: int) -> float:
    if total <= 0 or not runs:
        return 0.0
    covered = sum(max(0, int(end) - int(start) + 1) for start, end in runs)
    return float(covered) / float(total)


def _runs_balance_ratio(runs: list[tuple[int, int]]) -> float:
    if not runs:
        return 0.0
    lengths = [max(1, int(end) - int(start) + 1) for start, end in runs]
    return float(min(lengths)) / float(max(lengths))


def _looks_like_full_grid(
    cols: list[tuple[int, int]],
    rows: list[tuple[int, int]],
    vis: np.ndarray,
) -> bool:
    h, w = vis.shape[:2]
    col_coverage = _runs_coverage_ratio(cols, w)
    row_coverage = _runs_coverage_ratio(rows, h)
    col_balance = _runs_balance_ratio(cols)
    row_balance = _runs_balance_ratio(rows)
    return (
        col_coverage >= 0.72
        and row_coverage >= 0.72
        and col_balance >= 0.55
        and row_balance >= 0.55
    )


def _box_center(box: tuple[int, int, int, int]) -> tuple[float, float]:
    x0, y0, x1, y1 = box
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _box_iou(box_a: tuple[int, int, int, int], box_b: tuple[int, int, int, int]) -> float:
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    inter_x0 = max(ax0, bx0)
    inter_y0 = max(ay0, by0)
    inter_x1 = min(ax1, bx1)
    inter_y1 = min(ay1, by1)
    inter_w = max(0, inter_x1 - inter_x0)
    inter_h = max(0, inter_y1 - inter_y0)
    inter = inter_w * inter_h
    if inter <= 0:
        return 0.0
    area_a = max(1, ax1 - ax0) * max(1, ay1 - ay0)
    area_b = max(1, bx1 - bx0) * max(1, by1 - by0)
    return float(inter) / float(area_a + area_b - inter)


def _cluster_ranked_boxes(
    ranked_boxes: list[dict],
    *,
    max_distance: float,
    min_iou: float = 0.18,
) -> list[dict]:
    clusters: list[dict] = []
    for cand in ranked_boxes:
        center = cand["center"]
        placed = False
        for cluster in clusters:
            distance = math.hypot(center[0] - cluster["center"][0], center[1] - cluster["center"][1])
            overlap = _box_iou(cand["box"], cluster["best"]["box"])
            if distance <= max_distance or overlap >= min_iou:
                cluster["items"].append(cand)
                total_weight = sum(max(float(item["score"]["margin"]), 0.001) for item in cluster["items"])
                cluster["center"] = (
                    sum(item["center"][0] * max(float(item["score"]["margin"]), 0.001) for item in cluster["items"]) / total_weight,
                    sum(item["center"][1] * max(float(item["score"]["margin"]), 0.001) for item in cluster["items"]) / total_weight,
                )
                placed = True
                break
        if not placed:
            clusters.append(
                {
                    "best": cand,
                    "center": center,
                    "items": [cand],
                }
            )
    return clusters


def solve_hop_animals_cutout(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    vis = visible.visible
    h, w = vis.shape[:2]
    window_size = max(170, min(210, int(round(min(h, w) * 0.28))))
    step = max(100, int(round(window_size * 0.63)))
    positive_texts = [
        "a frog",
        "a rabbit",
        "a kangaroo",
        "a grasshopper",
        "a hare",
        "an animal that hops",
        "an animal that jumps",
    ]
    negative_texts = [
        "a bird",
        "a toucan",
        "a wolf",
        "a fish",
        "a dolphin",
        "a whale",
        "a turtle",
        "a dog",
        "a cat",
        "a purple round sticker",
        "a colorful round badge",
        "a wolf face badge",
    ]

    xs = list(range(0, max(1, w - window_size + 1), step))
    ys = list(range(0, max(1, h - window_size + 1), step))
    if xs[-1] != w - window_size:
        xs.append(max(0, w - window_size))
    if ys[-1] != h - window_size:
        ys.append(max(0, h - window_size))

    window_rows: list[dict] = []
    tile_images: list[Image.Image] = []
    for y in ys:
        for x in xs:
            box = (int(x), int(y), int(x + window_size), int(y + window_size))
            window_rows.append({"box": box, "center": _box_center(box)})
            tile_images.append(Image.fromarray(vis[y:y + window_size, x:x + window_size]))

    scores = classify_tiles_binary_prompt_groups(
        tile_images,
        positive_texts=positive_texts,
        negative_texts=negative_texts,
    )
    for row, score in zip(window_rows, scores):
        row["score"] = score

    ranked = sorted(
        window_rows,
        key=lambda item: (
            float(item["score"]["margin"]),
            float(item["score"]["positive_score"]),
        ),
        reverse=True,
    )
    positive_windows = [
        item
        for item in ranked
        if float(item["score"]["margin"]) >= 0.12
        and float(item["score"]["positive_score"]) > float(item["score"]["negative_score"])
    ][:18]
    if not positive_windows:
        positive_windows = [
            item
            for item in ranked
            if float(item["score"]["margin"]) >= 0.04
            and float(item["score"]["positive_score"]) >= 0.30
        ][:10]

    if not positive_windows:
        raise RuntimeError("未识别到 hop/jump 候选物体")

    clusters = _cluster_ranked_boxes(
        positive_windows,
        max_distance=max(120.0, window_size * 0.72),
        min_iou=0.18,
    )
    clusters = sorted(
        clusters,
        key=lambda cluster: (
            float(cluster["best"]["score"]["margin"]),
            float(cluster["best"]["score"]["positive_score"]),
            len(cluster["items"]),
        ),
        reverse=True,
    )

    refine_size = max(126, int(round(window_size * 0.68)))
    refine_images: list[Image.Image] = []
    for cluster in clusters:
        cx, cy = cluster["center"]
        x0 = max(0, int(round(cx - refine_size / 2.0)))
        y0 = max(0, int(round(cy - refine_size / 2.0)))
        x1 = min(w, x0 + refine_size)
        y1 = min(h, y0 + refine_size)
        x0 = max(0, x1 - refine_size)
        y0 = max(0, y1 - refine_size)
        cluster["refine_box"] = (int(x0), int(y0), int(x1), int(y1))
        refine_images.append(Image.fromarray(vis[y0:y1, x0:x1]))

    refine_scores = classify_tiles_binary_prompt_groups(
        refine_images,
        positive_texts=positive_texts,
        negative_texts=negative_texts,
    )
    for cluster, refine_score in zip(clusters, refine_scores):
        cluster["refine_score"] = refine_score
        cluster["combined_margin"] = (
            float(cluster["best"]["score"]["margin"]) * 0.55
            + float(refine_score["margin"]) * 0.45
        )

    filtered_clusters = [
        cluster
        for cluster in clusters
        if (
            (
                float(cluster["refine_score"]["margin"]) >= 0.18
                and float(cluster["refine_score"]["positive_score"]) >= 0.50
            )
            or (
                float(cluster["best"]["score"]["margin"]) >= 0.78
                and float(cluster["best"]["score"]["positive_score"]) >= 0.92
            )
            or (
                float(cluster["combined_margin"]) >= 0.32
                and float(cluster["refine_score"]["positive_score"]) > float(cluster["refine_score"]["negative_score"])
            )
        )
    ]
    if not filtered_clusters:
        filtered_clusters = clusters[:3]

    filtered_clusters = sorted(
        filtered_clusters,
        key=lambda cluster: (
            float(cluster["combined_margin"]),
            float(cluster["refine_score"]["positive_score"]),
            float(cluster["best"]["score"]["positive_score"]),
        ),
        reverse=True,
    )

    click_points_ranked = [
        (float(cluster["center"][0]), visible.y_offset + float(cluster["center"][1]))
        for cluster in filtered_clusters
    ]
    candidate_click_sets: list[list[tuple[float, float]]] = []
    if len(click_points_ranked) > 3:
        _append_unique_click_set(candidate_click_sets, click_points_ranked[:3])
    max_take = min(4, len(click_points_ranked))
    for n in range(max_take, 0, -1):
        _append_unique_click_set(candidate_click_sets, click_points_ranked[:n])
    if len(click_points_ranked) > 1:
        odd_ranked = [click_points_ranked[i] for i in range(0, min(len(click_points_ranked), 5), 2)]
        _append_unique_click_set(candidate_click_sets, odd_ranked)

    click_points = candidate_click_sets[0] if candidate_click_sets else []
    debug = {
        "mode": "cutout_windows",
        "window_size": window_size,
        "step": step,
        "window_count": len(window_rows),
        "positive_windows": [
            {
                "box": list(item["box"]),
                "center": [round(item["center"][0], 2), round(item["center"][1], 2)],
                "margin": float(item["score"]["margin"]),
                "positive_score": float(item["score"]["positive_score"]),
                "negative_score": float(item["score"]["negative_score"]),
            }
            for item in positive_windows
        ],
        "clusters": [
            {
                "center": [round(cluster["center"][0], 2), round(cluster["center"][1], 2)],
                "items": len(cluster["items"]),
                "best_box": list(cluster["best"]["box"]),
                "best_margin": float(cluster["best"]["score"]["margin"]),
                "best_positive_score": float(cluster["best"]["score"]["positive_score"]),
                "best_negative_score": float(cluster["best"]["score"]["negative_score"]),
                "refine_box": list(cluster["refine_box"]),
                "refine_margin": float(cluster["refine_score"]["margin"]),
                "refine_positive_score": float(cluster["refine_score"]["positive_score"]),
                "refine_negative_score": float(cluster["refine_score"]["negative_score"]),
                "combined_margin": float(cluster["combined_margin"]),
                "selected": cluster in filtered_clusters,
            }
            for cluster in clusters
        ],
        "candidate_click_sets": candidate_click_sets,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def solve_hop_animals(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    cols, rows = detect_label_grid(visible.visible)
    if not _looks_like_full_grid(cols, rows, visible.visible):
        return solve_hop_animals_cutout(arr, prompt)

    tile_images: list[Image.Image] = []
    centers: list[tuple[float, float]] = []
    for y0, y1 in rows:
        for x0, x1 in cols:
            tile = visible.visible[y0:y1 + 1, x0:x1 + 1]
            tile_images.append(Image.fromarray(tile))
            centers.append(((x0 + x1) / 2.0, visible.y_offset + (y0 + y1) / 2.0))

    scores = classify_tiles_binary_prompt_groups(
        tile_images,
        positive_texts=[
            "a rabbit",
            "a kangaroo",
            "a frog",
            "a grasshopper",
            "a hare",
        ],
        negative_texts=[
            "a dog",
            "a cat",
            "a cow",
            "a turtle",
            "a fish",
        ],
    )
    candidate_click_sets = build_candidate_click_sets(centers, scores, singular=False, positive_threshold=0.50)
    click_points = candidate_click_sets[0] if candidate_click_sets else []
    debug = {
        "mode": "grid",
        "rows": rows,
        "cols": cols,
        "grid_col_coverage": _runs_coverage_ratio(cols, visible.visible.shape[1]),
        "grid_row_coverage": _runs_coverage_ratio(rows, visible.visible.shape[0]),
        "scores": scores,
        "candidate_click_sets": candidate_click_sets,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def solve_shiny_thing(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    cols, rows = detect_label_grid(visible.visible)

    tile_images: list[Image.Image] = []
    centers: list[tuple[float, float]] = []
    for y0, y1 in rows:
        for x0, x1 in cols:
            tile = visible.visible[y0:y1 + 1, x0:x1 + 1]
            tile_images.append(Image.fromarray(tile))
            centers.append(((x0 + x1) / 2.0, visible.y_offset + (y0 + y1) / 2.0))

    scores = classify_tiles_binary_prompt_groups(
        tile_images,
        positive_texts=[
            "a shiny thing",
            "a shiny object",
            "a sparkling jewel",
            "a golden butterfly",
            "a reflective object",
        ],
        negative_texts=[
            "a sock",
            "snowy trees",
            "a winter forest",
            "an orange sock",
            "a group of trees",
        ],
    )
    candidate_click_sets = build_candidate_click_sets(centers, scores, singular=True, positive_threshold=0.45)
    click_points = candidate_click_sets[0] if candidate_click_sets else []
    debug = {
        "rows": rows,
        "cols": cols,
        "scores": scores,
        "candidate_click_sets": candidate_click_sets,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def solve_kept_outside(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    cols, rows = detect_label_grid(visible.visible)

    tile_images: list[Image.Image] = []
    centers: list[tuple[float, float]] = []
    for y0, y1 in rows:
        for x0, x1 in cols:
            tile = visible.visible[y0:y1 + 1, x0:x1 + 1]
            tile_images.append(Image.fromarray(tile))
            centers.append(((x0 + x1) / 2.0, visible.y_offset + (y0 + y1) / 2.0))

    scores = classify_tiles_binary_prompt_groups(
        tile_images,
        positive_texts=[
            "an item typically kept outside",
            "an outdoor object",
            "a garden item",
            "a patio item",
            "something usually left outside",
        ],
        negative_texts=[
            "an indoor household item",
            "something typically kept inside",
            "an indoor object",
            "a room item",
            "something used indoors",
        ],
    )
    candidate_click_sets = build_candidate_click_sets(centers, scores, singular=False, positive_threshold=0.45)
    click_points = candidate_click_sets[0] if candidate_click_sets else []
    debug = {
        "rows": rows,
        "cols": cols,
        "scores": scores,
        "candidate_click_sets": candidate_click_sets,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def _normalize_rect_angle(rect: tuple[tuple[float, float], tuple[float, float], float]) -> float:
    (_, _), (w, h), angle = rect
    if float(w) > float(h):
        angle += 90.0
    while angle <= -90.0:
        angle += 180.0
    while angle > 90.0:
        angle -= 180.0
    return float(angle)


def _rect_angle_delta(angle_a: float, angle_b: float) -> float:
    delta = abs(float(angle_a) - float(angle_b)) % 180.0
    return min(delta, 180.0 - delta)


def _collect_contour_components(mask: np.ndarray, *, min_area: float = 1000.0) -> list[dict]:
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    components: list[dict] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_area:
            continue
        rect = cv2.minAreaRect(contour)
        x, y, w, h = cv2.boundingRect(contour)
        local_mask = np.zeros((h, w), dtype=np.uint8)
        shifted = contour.copy()
        shifted[:, 0, 0] -= x
        shifted[:, 0, 1] -= y
        cv2.drawContours(local_mask, [shifted], -1, 1, thickness=-1)
        components.append(
            {
                "area": area,
                "box": (int(x), int(y), int(w), int(h)),
                "center": (float(rect[0][0]), float(rect[0][1])),
                "rect": rect,
                "angle": _normalize_rect_angle(rect),
                "shape_mask": local_mask,
            }
        )
    return components


def solve_missing_pieces_drag(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    vis = visible.visible

    sat = vis.max(axis=2) - vis.min(axis=2)
    col_score = sat.mean(axis=0)
    width = vis.shape[1]
    split = min(range(int(width * 0.45), int(width * 0.8)), key=lambda x: float(col_score[x]))
    left = vis[:, :split]
    right = vis[:, split:]

    left_hsv = cv2.cvtColor(left, cv2.COLOR_RGB2HSV)
    holes_mask = (
        (left_hsv[:, :, 0] >= 95)
        & (left_hsv[:, :, 0] <= 135)
        & (left_hsv[:, :, 1] >= 25)
        & (left_hsv[:, :, 2] >= 120)
    ).astype(np.uint8)
    holes_mask = cv2.morphologyEx(holes_mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    holes_mask = cv2.dilate(holes_mask, np.ones((7, 7), np.uint8), iterations=1)
    holes = _collect_contour_components(holes_mask, min_area=2500.0)
    if not holes:
        raise RuntimeError("未识别到 missing piece 目标槽位")

    right_bg = np.median(right[:80, :80].reshape(-1, 3), axis=0)
    right_diff = np.abs(right.astype(np.int16) - right_bg.astype(np.int16)).sum(axis=2)
    right_sat = right.max(axis=2) - right.min(axis=2)
    pieces_mask = ((right_sat > 25) & (right_diff > 45)).astype(np.uint8)
    pieces_mask = cv2.morphologyEx(pieces_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    pieces_mask = cv2.dilate(pieces_mask, np.ones((5, 5), np.uint8), iterations=1)
    pieces = _collect_contour_components(pieces_mask, min_area=2500.0)
    if not pieces:
        raise RuntimeError("未识别到 missing piece 源图块")

    candidate_pairs: list[dict] = []
    for piece in pieces:
        pw = max(1.0, float(piece["box"][2]))
        ph = max(1.0, float(piece["box"][3]))
        piece_aspect = pw / ph
        for hole in holes:
            hw = max(1.0, float(hole["box"][2]))
            hh = max(1.0, float(hole["box"][3]))
            hole_aspect = hw / hh
            resized_piece_mask = cv2.resize(
                piece["shape_mask"].astype(np.uint8),
                (hole["shape_mask"].shape[1], hole["shape_mask"].shape[0]),
                interpolation=cv2.INTER_NEAREST,
            )
            inter = float(((resized_piece_mask > 0) & (hole["shape_mask"] > 0)).sum())
            union = float(((resized_piece_mask > 0) | (hole["shape_mask"] > 0)).sum())
            shape_iou = inter / max(1.0, union)
            angle_delta = _rect_angle_delta(float(piece["angle"]), float(hole["angle"]))
            area_penalty = abs(math.log(max(float(piece["area"]), 1.0) / max(float(hole["area"]), 1.0)))
            aspect_penalty = abs(math.log(max(piece_aspect, 1e-6) / max(hole_aspect, 1e-6)))
            adjusted_score = (
                shape_iou
                - 0.06 * angle_delta
                - 0.20 * area_penalty
                - 0.12 * aspect_penalty
            )
            candidate_pairs.append(
                {
                    "piece": piece,
                    "hole": hole,
                    "shape_iou": shape_iou,
                    "angle_delta": angle_delta,
                    "area_penalty": area_penalty,
                    "aspect_penalty": aspect_penalty,
                    "adjusted_score": adjusted_score,
                    "rel": (0.5, 0.5),
                }
            )

    if not candidate_pairs:
        raise RuntimeError("未找到 missing piece 可用匹配对")

    candidate_pairs = sorted(candidate_pairs, key=lambda item: float(item["adjusted_score"]), reverse=True)
    best_pair = candidate_pairs[0]
    best_piece = best_pair["piece"]
    best_hole = best_pair["hole"]

    src_center = (
        split + float(best_piece["center"][0]),
        visible.y_offset + float(best_piece["center"][1]),
    )
    hx, hy, hw, hh = best_hole["box"]
    target = (
        float(best_hole["center"][0]),
        visible.y_offset + float(best_hole["center"][1]),
    )
    debug = {
        "mode": "missing_pieces_drag",
        "split": split,
        "visible_y_offset": visible.y_offset,
        "holes": [
            {
                "box": list(item["box"]),
                "center": [round(item["center"][0], 2), round(item["center"][1], 2)],
                "angle": float(item["angle"]),
                "area": float(item["area"]),
            }
            for item in holes
        ],
        "pieces": [
            {
                "box": list(item["box"]),
                "center": [round(item["center"][0], 2), round(item["center"][1], 2)],
                "angle": float(item["angle"]),
                "area": float(item["area"]),
            }
            for item in pieces
        ],
        "candidate_scores": [
            {
                "piece": {
                    "box": list(item["piece"]["box"]),
                    "center": [round(item["piece"]["center"][0], 2), round(item["piece"]["center"][1], 2)],
                    "angle": float(item["piece"]["angle"]),
                },
                "component": {
                    "box": list(item["hole"]["box"]),
                    "center": [round(item["hole"]["center"][0], 2), round(item["hole"]["center"][1], 2)],
                    "angle": float(item["hole"]["angle"]),
                },
                "raw_score": float(item["shape_iou"]),
                "angle_penalty": float(item["angle_delta"]),
                "area_penalty": float(item["area_penalty"]),
                "aspect_penalty": float(item["aspect_penalty"]),
                "adjusted_score": float(item["adjusted_score"]),
                "rel": item["rel"],
            }
            for item in candidate_pairs[:6]
        ],
        "incomplete": {
            "box": [int(hx), int(hy), int(hw), int(hh)],
            "center": [round(best_hole["center"][0], 2), round(best_hole["center"][1], 2)],
            "area": float(best_hole["area"]),
        },
        "best_pair": {
            "piece_box": list(best_piece["box"]),
            "piece_center": [round(best_piece["center"][0], 2), round(best_piece["center"][1], 2)],
            "hole_box": list(best_hole["box"]),
            "hole_center": [round(best_hole["center"][0], 2), round(best_hole["center"][1], 2)],
            "shape_iou": float(best_pair["shape_iou"]),
            "angle_delta": float(best_pair["angle_delta"]),
            "adjusted_score": float(best_pair["adjusted_score"]),
        },
        "target": target,
    }
    return SolverAction(kind="drag", prompt=prompt, drag_from=src_center, drag_to=target, debug=debug)


def solve_pair_drag(arr: np.ndarray, prompt: str) -> SolverAction:
    lower_prompt = prompt.lower().strip()
    if "missing piece" in lower_prompt or "missing pieces" in lower_prompt or "complete the image" in lower_prompt:
        return solve_missing_pieces_drag(arr, prompt)

    visible = extract_visible_canvas(arr)
    vis = visible.visible

    sat = vis.max(axis=2) - vis.min(axis=2)
    col_score = sat.mean(axis=0)
    width = vis.shape[1]
    split = min(range(int(width * 0.45), int(width * 0.8)), key=lambda x: float(col_score[x]))

    left = vis[:, :split]
    right = vis[:, split:]

    right_sat = right.max(axis=2) - right.min(axis=2)
    right_bg = np.median(right[:50, :50].reshape(-1, 3), axis=0)
    right_diff = np.abs(right.astype(np.int16) - right_bg.astype(np.int16)).sum(axis=2)
    src_mask = ((right_sat > 20) & (right_diff > 25)).astype(np.uint8)
    src_mask = cv2.morphologyEx(src_mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(src_mask, 8)
    if n <= 1:
        raise RuntimeError("右侧 source object 未识别到")
    src_idx = max(range(1, n), key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
    src_x, src_y, src_w, src_h, _src_area = stats[src_idx]
    src_center_right = tuple(map(float, centroids[src_idx]))
    src_center = (split + src_center_right[0], visible.y_offset + src_center_right[1])
    source_box = (int(split + src_x), int(visible.y_offset + src_y), int(src_w), int(src_h))
    src_pixels = right[labels == src_idx]
    src_mean = src_pixels.mean(axis=0)

    src_dist = np.linalg.norm(left.astype(np.float32) - src_mean.astype(np.float32), axis=2)
    src_like = (src_dist < 55).astype(np.uint8)
    src_like = cv2.morphologyEx(src_like, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    left_sat = left.max(axis=2) - left.min(axis=2)
    left_bg = np.median(left[:50, :50].reshape(-1, 3), axis=0)
    left_diff = np.abs(left.astype(np.int16) - left_bg.astype(np.int16)).sum(axis=2)
    obj_mask = ((left_sat > 20) & (left_diff > 25)).astype(np.uint8)
    obj_mask = cv2.dilate(obj_mask, np.ones((9, 9), np.uint8), iterations=2)
    obj_mask = cv2.morphologyEx(obj_mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

    n, obj_labels, obj_stats, obj_centroids = cv2.connectedComponentsWithStats(obj_mask, 8)
    components: list[dict] = []
    for i in range(1, n):
        x, y, w, h, area = obj_stats[i]
        if int(area) < 1500:
            continue
        comp_mask = (obj_labels == i)
        components.append(
            {
                "i": i,
                "box": (int(x), int(y), int(w), int(h)),
                "area": int(area),
                "src_pixels": int((src_like & comp_mask).sum()),
                "center": tuple(map(float, obj_centroids[i])),
            }
        )
    if len(components) < 2:
        raise RuntimeError("左侧 object cluster 数量不足")

    incomplete = min(components, key=lambda c: c["src_pixels"])
    complete = [c for c in components if c["i"] != incomplete["i"]]

    def extract_masks(comp: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        x, y, w, h = comp["box"]
        comp_mask = (obj_labels[y:y + h, x:x + w] == comp["i"]).astype(np.uint8)
        src = src_like[y:y + h, x:x + w]
        skeleton = ((comp_mask > 0) & (src == 0)).astype(np.uint8)
        return comp_mask, src, skeleton

    inc_mask, inc_src, inc_skeleton = extract_masks(incomplete)
    inc_x, inc_y, inc_w, inc_h = incomplete["box"]
    inc_center_x, inc_center_y = incomplete["center"]
    best_score = -1e9
    best_rel: tuple[float, float] | None = None
    best_component: dict | None = None
    candidate_debug: list[dict] = []

    for comp in complete:
        _, src, skeleton = extract_masks(comp)
        resized = cv2.resize(skeleton.astype(np.uint8), (inc_skeleton.shape[1], inc_skeleton.shape[0]), interpolation=cv2.INTER_NEAREST)
        inter = float(((resized > 0) & (inc_skeleton > 0)).sum())
        union = float(((resized > 0) | (inc_skeleton > 0)).sum())
        score = inter / max(1.0, union)
        ys, xs = np.where(src > 0)
        if len(xs) == 0:
            continue
        rel = (float(xs.mean() / src.shape[1]), float(ys.mean() / src.shape[0]))
        comp_x, comp_y, comp_w, comp_h = comp["box"]
        size_penalty = abs(math.log(max(comp_w, 1) / max(inc_w, 1))) + abs(math.log(max(comp_h, 1) / max(inc_h, 1)))
        aspect_penalty = abs(math.log((max(comp_w, 1) / max(comp_h, 1)) / (max(inc_w, 1) / max(inc_h, 1))))
        comp_center_x, comp_center_y = comp["center"]
        row_penalty = abs(float(comp_center_y) - float(inc_center_y)) / max(float(inc_h), float(comp_h), 1.0)
        adjusted_score = score - 0.20 * size_penalty - 0.20 * aspect_penalty - 0.15 * row_penalty
        candidate_debug.append(
            {
                "component": comp,
                "raw_score": score,
                "size_penalty": size_penalty,
                "aspect_penalty": aspect_penalty,
                "row_penalty": row_penalty,
                "adjusted_score": adjusted_score,
                "rel": rel,
            }
        )
        if adjusted_score > best_score:
            best_score = adjusted_score
            best_rel = rel
            best_component = comp

    if best_rel is None or best_component is None:
        raise RuntimeError("未找到可用于推算落点的完整 pair")

    x, y, w, h = incomplete["box"]
    target = (x + best_rel[0] * w, visible.y_offset + y + best_rel[1] * h)
    debug = {
        "split": split,
        "visible_y_offset": visible.y_offset,
        "source_mean": src_mean.tolist(),
        "source_box": source_box,
        "source_center": src_center,
        "components": components,
        "incomplete": incomplete,
        "best_component": best_component,
        "best_score": best_score,
        "candidate_scores": candidate_debug,
        "target": target,
    }
    return SolverAction(kind="drag", prompt=prompt, drag_from=src_center, drag_to=target, debug=debug)


def solve_hidden_under_reference(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    vis = visible.visible
    gray = cv2.cvtColor(vis, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    mask = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

    n, labels, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    h, w = vis.shape[:2]
    candidates: list[dict] = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        area = int(area)
        if area < 2500:
            continue
        if bw >= int(w * 0.45) or bh >= int(h * 0.45):
            continue
        center = [float(centroids[i][0]), float(centroids[i][1])]
        candidates.append(
            {
                "i": i,
                "box": [int(x), int(y), int(bw), int(bh)],
                "area": area,
                "center": center,
            }
        )

    if not candidates:
        raise RuntimeError("未识别到可点击候选物体")

    candidates = sorted(candidates, key=lambda c: (c["center"][1], c["center"][0]))
    base_points = [
        (float(c["center"][0]), visible.y_offset + float(c["center"][1]))
        for c in candidates
    ]
    candidate_click_points = build_click_point_variations(base_points)
    debug = {
        "visible_y_offset": visible.y_offset,
        "candidates": candidates,
        "candidate_click_points": candidate_click_points,
    }
    return SolverAction(kind="click_one", prompt=prompt, click_points=None, debug=debug)


def solve_road_completion(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    vis = visible.visible
    gray = cv2.cvtColor(vis, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    mask = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

    n, labels, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    candidates: list[dict] = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        area = int(area)
        if area < 800 or area > 7000:
            continue
        if bw < 70 or bh < 40 or bw > 180 or bh > 180:
            continue
        center = [float(centroids[i][0]), float(centroids[i][1])]
        candidates.append(
            {
                "i": i,
                "box": [int(x), int(y), int(bw), int(bh)],
                "area": area,
                "center": center,
            }
        )

    if not candidates:
        raise RuntimeError("未识别到 road 候选块")

    candidates = sorted(candidates, key=lambda c: (c["center"][1], c["center"][0]))
    base_points = [
        (float(c["center"][0]), visible.y_offset + float(c["center"][1]))
        for c in candidates
    ]
    candidate_click_points = build_click_point_variations(base_points)
    debug = {
        "visible_y_offset": visible.y_offset,
        "candidates": candidates,
        "candidate_click_points": candidate_click_points,
    }
    return SolverAction(kind="click_one", prompt=prompt, click_points=None, debug=debug)


def solve_dissolve_melt(arr: np.ndarray, prompt: str) -> SolverAction:
    visible = extract_visible_canvas(arr)
    vis = visible.visible
    gray = cv2.cvtColor(vis, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    mask = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

    n, labels, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    h, w = vis.shape[:2]
    candidates: list[dict] = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        area = int(area)
        if area < 1500:
            continue
        if bw < 140 or bh < 120:
            continue
        if bw >= int(w * 0.5) or bh >= int(h * 0.5):
            continue
        candidates.append(
            {
                "i": i,
                "box": [int(x), int(y), int(bw), int(bh)],
                "area": area,
                "center": [float(centroids[i][0]), float(centroids[i][1])],
            }
        )

    if not candidates:
        raise RuntimeError("未识别到 dissolve/melt 候选框")

    candidates = sorted(candidates, key=lambda c: (c["center"][1], c["center"][0]))
    texts = [
        "ice cube",
        "sugar cube",
        "soap bar",
        "chocolate bar",
        "marshmallow",
        "candle wax",
        "wood block",
        "plastic toy",
        "rock",
        "metal spoon",
        "paper cup",
        "toy car",
    ]
    positive = {"ice cube", "sugar cube", "soap bar", "chocolate bar", "marshmallow", "candle wax"}
    model, processor = _load_clip()
    images = []
    for cand in candidates:
        x, y, bw, bh = cand["box"]
        images.append(Image.fromarray(vis[y:y + bh, x:x + bw]))
    inputs = processor(text=texts, images=images, return_tensors="pt", padding=True)
    with torch.no_grad():
        probs = model(**inputs).logits_per_image.softmax(dim=1).cpu().numpy()

    scored_candidates: list[dict] = []
    centers: list[tuple[float, float]] = []
    score_rows: list[dict[str, float]] = []
    for cand, row in zip(candidates, probs):
        scores = {texts[i]: float(row[i]) for i in range(len(texts))}
        pos_score = sum(scores[t] for t in positive)
        neg_score = 1.0 - pos_score
        scored = {
            **cand,
            "positive_score": pos_score,
            "negative_score": neg_score,
            "scores": scores,
        }
        scored_candidates.append(scored)
        cx, cy = cand["center"]
        centers.append((float(cx), visible.y_offset + float(cy)))
        score_rows.append(
            {
                "positive_score": float(pos_score),
                "negative_score": float(neg_score),
                "margin": float(pos_score - neg_score),
            }
        )

    candidate_click_sets = build_candidate_click_sets(
        centers,
        score_rows,
        singular=False,
        positive_threshold=0.42,
    )
    click_points = candidate_click_sets[0] if candidate_click_sets else []

    debug = {
        "visible_y_offset": visible.y_offset,
        "candidates": scored_candidates,
        "candidate_click_sets": candidate_click_sets,
    }
    return SolverAction(kind="click", prompt=prompt, click_points=click_points, debug=debug)


def image_to_canvas_point(img_point: tuple[float, float], full_shape: tuple[int, int, int], visible_y0: int, canvas_box: dict) -> tuple[float, float]:
    x_img, y_img = img_point
    full_h, full_w = full_shape[:2]
    visible_h = full_h - visible_y0
    x = canvas_box["x"] + (x_img / full_w) * canvas_box["width"]
    y = canvas_box["y"] + ((y_img - visible_y0) / visible_h) * canvas_box["height"]
    return float(x), float(y)


def _raster_diff_metrics(prev_arr: np.ndarray | None, new_arr: np.ndarray | None) -> dict[str, Any]:
    if prev_arr is None or new_arr is None:
        return {
            "shape_changed": prev_arr is None or new_arr is None,
            "changed_pixels": 0,
            "mean_diff": 0.0,
            "bbox": None,
        }
    if prev_arr.shape != new_arr.shape:
        return {
            "shape_changed": True,
            "changed_pixels": -1,
            "mean_diff": 999.0,
            "bbox": None,
        }
    diff = np.abs(prev_arr.astype(np.int16) - new_arr.astype(np.int16))
    mask = diff.sum(axis=2) > 24
    changed_pixels = int(mask.sum())
    bbox = None
    if changed_pixels > 0:
        ys, xs = np.where(mask)
        bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    return {
        "shape_changed": False,
        "changed_pixels": changed_pixels,
        "mean_diff": float(diff.mean()),
        "bbox": bbox,
    }


def _looks_like_interaction_change(metrics: dict[str, Any]) -> bool:
    if metrics.get("shape_changed"):
        return True
    if int(metrics.get("changed_pixels") or 0) >= 900:
        return True
    if float(metrics.get("mean_diff") or 0.0) >= 0.35:
        return True
    return False


def _click_image_point_with_feedback(
    page,
    ch,
    *,
    arr: np.ndarray,
    raster_box: dict,
    img_point: tuple[float, float],
    prompt_hint: str,
    submit_text_hint: str,
) -> tuple[bool, np.ndarray, dict, str, dict[str, Any]]:
    visible = extract_visible_canvas(arr)
    base_x, base_y = image_to_canvas_point(img_point, arr.shape, visible.y_offset, raster_box)
    click_offsets = [
        (0.0, 0.0),
        (-10.0, 0.0),
        (10.0, 0.0),
        (0.0, -10.0),
        (0.0, 10.0),
        (-16.0, 0.0),
        (16.0, 0.0),
        (-8.0, -8.0),
        (8.0, 8.0),
    ]
    last_metrics: dict[str, Any] = {}
    latest_arr = arr
    latest_box = raster_box
    latest_source = "unknown"

    for attempt_idx, (dx, dy) in enumerate(click_offsets):
        click_x = float(base_x + dx)
        click_y = float(base_y + dy)
        _human_click(page, click_x, click_y)
        page.wait_for_timeout(260)
        try:
            new_arr, new_box, new_source = get_challenge_raster(ch)
        except Exception:
            return True, arr, raster_box, "raster_unavailable_after_click", {
                "attempt": attempt_idx,
                "canvas_point": [round(click_x, 2), round(click_y, 2)],
                "changed_pixels": None,
                "mean_diff": 0.0,
                "bbox": None,
                "shape_changed": True,
                "status": "raster_unavailable_after_click",
            }
        latest_arr = new_arr
        latest_box = new_box
        latest_source = new_source
        metrics = _raster_diff_metrics(arr, new_arr)
        try:
            new_prompt = get_prompt(ch, prompt_hint)
        except Exception:
            new_prompt = prompt_hint
        try:
            new_submit_text = get_submit_text(ch)
        except Exception:
            new_submit_text = submit_text_hint
        metrics.update(
            {
                "attempt": attempt_idx,
                "canvas_point": [round(click_x, 2), round(click_y, 2)],
                "prompt_changed": bool(new_prompt and new_prompt != prompt_hint),
                "submit_changed": bool(new_submit_text and new_submit_text != submit_text_hint),
                "new_prompt": new_prompt,
                "new_submit_text": new_submit_text,
            }
        )
        last_metrics = metrics
        if (
            metrics["prompt_changed"]
            or metrics["submit_changed"]
            or _looks_like_interaction_change(metrics)
        ):
            return True, new_arr, new_box, new_source, metrics
        page.wait_for_timeout(120)

    return False, latest_arr, latest_box, latest_source, last_metrics


def _drag_with_feedback(
    page,
    ch,
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    arr: np.ndarray,
    raster_box: dict,
) -> tuple[bool, np.ndarray, dict, str, dict[str, Any]]:
    start_offsets = [
        (0.0, 0.0),
        (-8.0, 0.0),
        (8.0, 0.0),
        (0.0, -8.0),
        (0.0, 8.0),
    ]
    end_offsets = [
        (0.0, 0.0),
        (-10.0, 0.0),
        (10.0, 0.0),
        (0.0, -10.0),
        (0.0, 10.0),
    ]
    last_metrics: dict[str, Any] = {}
    latest_arr = arr
    latest_box = raster_box
    latest_source = "unknown"
    gesture_idx = 0
    for sdx, sdy in start_offsets:
        for edx, edy in end_offsets:
            gesture_idx += 1
            drag_start = (float(start[0] + sdx), float(start[1] + sdy))
            drag_end = (float(end[0] + edx), float(end[1] + edy))
            _human_drag(page, drag_start, drag_end)
            page.wait_for_timeout(420)
            try:
                new_arr, new_box, new_source = get_challenge_raster(ch)
            except Exception:
                return True, arr, raster_box, "raster_unavailable_after_drag", {
                    "gesture_idx": gesture_idx,
                    "drag_start": [round(drag_start[0], 2), round(drag_start[1], 2)],
                    "drag_end": [round(drag_end[0], 2), round(drag_end[1], 2)],
                    "changed_pixels": None,
                    "mean_diff": 0.0,
                    "bbox": None,
                    "shape_changed": True,
                    "status": "raster_unavailable_after_drag",
                }
            latest_arr = new_arr
            latest_box = new_box
            latest_source = new_source
            metrics = _raster_diff_metrics(arr, new_arr)
            metrics.update(
                {
                    "gesture_idx": gesture_idx,
                    "drag_start": [round(drag_start[0], 2), round(drag_start[1], 2)],
                    "drag_end": [round(drag_end[0], 2), round(drag_end[1], 2)],
                }
            )
            last_metrics = metrics
            if _looks_like_interaction_change(metrics):
                return True, new_arr, new_box, new_source, metrics
            page.wait_for_timeout(120)
    return False, latest_arr, latest_box, latest_source, last_metrics


def challenge_frame(page):
    return next((f for f in page.frames if "frame=challenge" in f.url), None)


def checkbox_frame(page):
    return next((f for f in page.frames if "frame=checkbox" in f.url), None)


def click_checkbox(page) -> bool:
    cb = checkbox_frame(page)
    if not cb:
        return False
    for sel in ["#checkbox", '[role="checkbox"]', 'div[aria-checked]']:
        try:
            loc = cb.locator(sel).first
            for force in (False, True):
                try:
                    loc.click(timeout=1_500, force=force)
                    return True
                except Exception:
                    continue
            try:
                box = loc.bounding_box(timeout=1_500)
            except Exception:
                box = None
            if box:
                page.mouse.click(
                    float(box["x"] + box["width"] / 2.0),
                    float(box["y"] + box["height"] / 2.0),
                )
                return True
        except Exception:
            continue
    return False


def _human_move(page, x: float, y: float, *, steps: int = 10, hover_ms: int = 40):
    page.mouse.move(float(x), float(y), steps=max(1, int(steps)))
    if hover_ms > 0:
        page.wait_for_timeout(int(hover_ms))


def _human_click(page, x: float, y: float):
    x = float(x)
    y = float(y)
    pre_points = [
        (x - 14.0, y - 10.0),
        (x - 6.0, y - 4.0),
        (x + 2.0, y + 1.0),
        (x, y),
    ]
    for idx, (px, py) in enumerate(pre_points):
        _human_move(page, px, py, steps=7 + idx * 2, hover_ms=25 + idx * 10)
    page.mouse.down()
    page.wait_for_timeout(55)
    page.mouse.up()
    page.wait_for_timeout(180)


def _human_drag(page, start: tuple[float, float], end: tuple[float, float]):
    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    mx1 = sx + (ex - sx) * 0.35
    my1 = sy + (ey - sy) * 0.18
    mx2 = sx + (ex - sx) * 0.72
    my2 = sy + (ey - sy) * 0.82
    _human_move(page, sx - 12.0, sy - 8.0, steps=8, hover_ms=35)
    _human_move(page, sx, sy, steps=6, hover_ms=60)
    page.mouse.down()
    page.wait_for_timeout(90)
    page.mouse.move(mx1, my1, steps=12)
    page.wait_for_timeout(45)
    page.mouse.move(mx2, my2, steps=14)
    page.wait_for_timeout(45)
    page.mouse.move(ex, ey, steps=12)
    page.wait_for_timeout(110)
    page.mouse.up()
    page.wait_for_timeout(220)


def _normalize_prompt_text(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.strip(" \t\r\n:：-")


def _looks_like_prompt_line(line: str) -> bool:
    lower = _normalize_prompt_text(line).lower()
    if not lower:
        return False
    if lower.startswith("please try again"):
        return False
    prompt_markers = (
        "please click",
        "please select",
        "please drag",
        "please drop",
        "tap on",
        "click on",
        "select all",
        "drag the object",
        "drop the object",
        "complete the pair",
        "complete the image",
        "missing piece",
        "match the pair",
        "finish line",
        "water travel",
        "float on water",
        "served hot",
        "hop or jump",
        "jumping or hopping",
        "produce heat to work",
        "shiny thing",
        "dissolve or melt",
        "fully hidden under the reference object",
        "entirely concealed by the shown object",
        "entirely hidden by the shown object",
    )
    return any(marker in lower for marker in prompt_markers)


def _extract_prompt_from_hcaptcha_payload(data: Any) -> str:
    if isinstance(data, str):
        text = _normalize_prompt_text(data)
        return text if _looks_like_prompt_line(text) else ""
    if isinstance(data, list):
        for item in data:
            prompt = _extract_prompt_from_hcaptcha_payload(item)
            if prompt:
                return prompt
        return ""
    if not isinstance(data, dict):
        return ""

    requester_question = data.get("requester_question")
    if isinstance(requester_question, dict):
        for key in ("en", "en-US", "text"):
            value = requester_question.get(key)
            prompt = _extract_prompt_from_hcaptcha_payload(value)
            if prompt:
                return prompt
        for value in requester_question.values():
            prompt = _extract_prompt_from_hcaptcha_payload(value)
            if prompt:
                return prompt
    elif requester_question:
        prompt = _extract_prompt_from_hcaptcha_payload(requester_question)
        if prompt:
            return prompt

    for key in ("prompt", "instruction", "question", "label"):
        value = data.get(key)
        prompt = _extract_prompt_from_hcaptcha_payload(value)
        if prompt:
            return prompt

    for value in data.values():
        prompt = _extract_prompt_from_hcaptcha_payload(value)
        if prompt:
            return prompt
    return ""


def get_prompt(ch, network_prompt: str = "") -> str:
    for sel in [
        "#prompt-question",
        "[data-theme='challenge-container'] h2",
        "[data-theme='challenge-container'] .prompt-text",
        ".prompt-text",
        ".challenge-example .prompt-text",
    ]:
        try:
            txt = _normalize_prompt_text(ch.locator(sel).first.inner_text(timeout=2_000))
            if txt:
                return txt
        except Exception:
            continue
    try:
        body = ch.locator("body").inner_text(timeout=2_000)
    except Exception:
        return _normalize_prompt_text(network_prompt)
    if not body:
        return _normalize_prompt_text(network_prompt)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return _normalize_prompt_text(network_prompt)
    for line in lines:
        text = _normalize_prompt_text(line)
        if _looks_like_prompt_line(text):
            return text
    fallback_prompt = _normalize_prompt_text(network_prompt)
    if fallback_prompt:
        return fallback_prompt
    first = _normalize_prompt_text(lines[0])
    if _looks_like_prompt_line(first):
        return first
    return ""


def get_submit_text(ch) -> str:
    for sel in [".button-submit .text", ".button-submit"]:
        try:
            txt = ch.locator(sel).first.inner_text(timeout=1_000).strip()
            if txt:
                return txt
        except Exception:
            continue
    return ""


def resolve_effective_prompt(
    dom_prompt: str,
    network_prompt: str = "",
    *,
    request_type: str = "",
    network_updated_at: float = 0.0,
) -> str:
    """在 DOM 题面与网络题面不一致时，尽量选用更接近当前真实 challenge 的 prompt。"""
    dom = _normalize_prompt_text(dom_prompt)
    net = _normalize_prompt_text(network_prompt)
    req = str(request_type or "").strip().lower()

    if not net:
        return dom
    if not dom:
        return net

    dom_is_drag = is_drag_completion_prompt(dom)
    net_is_drag = is_drag_completion_prompt(net)

    # 网络已明确切到 drag 题型，但 DOM 仍停留在旧 click prompt。
    if req == "image_drag_drop" and (not dom_is_drag or net_is_drag):
        return net

    # 网络已明确切回点击题，但 DOM 还残留旧 drag prompt。
    if req == "image_label_area_select" and dom_is_drag and not net_is_drag:
        return net

    # 若刚收到新的 getcaptcha，而 DOM 还没同步，短时间内优先信任网络题面。
    if req and dom != net and network_updated_at:
        if (time.time() - float(network_updated_at)) <= 8.0:
            return net

    return dom


def is_water_vehicle_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    if not p:
        return False
    if "water travel" in p or "operate on water" in p:
        return True
    if "vehicle" in p and "water" in p:
        return True
    return False


def is_drag_completion_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    if not p:
        return False
    if "drag" in p:
        return True
    if "drop" in p and ("pair" in p or "image" in p or "match" in p):
        return True
    if "complete the pair" in p:
        return True
    if "complete the image" in p:
        return True
    if "missing piece" in p or "missing pieces" in p:
        return True
    # road completion 也是 drag 题型（拖拽道路块到空位）
    if "complete the road" in p:
        return True
    return False


def is_float_on_water_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return "float on water" in p


def is_hidden_under_reference_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return (
        "fully hidden under the reference object" in p
        or "entirely concealed by the shown object" in p
        or "entirely hidden by the shown object" in p
    )


def is_road_completion_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return "complete the road" in p and "finish line" in p


def is_dissolve_melt_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return "dissolve or melt in water" in p


def is_hot_food_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return "served hot" in p


def is_hop_animals_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return (
        "hop or jump" in p
        or "jumping or hopping" in p
        or ("animals" in p and "hopping" in p)
    )


def is_heat_work_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return (
        "produce heat to work" in p
        or "produce heat when they work" in p
        or "things that produce heat when they work" in p
    )


def is_shiny_thing_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return "shiny thing" in p


def _looks_like_full_grid_fallback(arr: np.ndarray) -> bool:
    try:
        visible = extract_visible_canvas(arr)
        cols, rows = detect_label_grid(visible.visible)
        return len(cols) >= 3 and len(rows) >= 3
    except Exception:
        return False


def is_kept_outside_prompt(prompt: str) -> bool:
    p = prompt.lower().strip()
    return (
        "kept outside" in p
        or "typically kept outside" in p
        or "items that are typically kept outside" in p
    )


def get_canvas_image(ch) -> np.ndarray:
    data_url = ch.evaluate(
        """() => {
            const canvas = document.querySelector('canvas');
            return canvas ? canvas.toDataURL('image/png') : null;
        }"""
    )
    if not data_url:
        raise RuntimeError("challenge canvas 不可用")
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return np.array(Image.open(io.BytesIO(raw)).convert("RGB"))


def _raster_is_visually_empty(arr: np.ndarray) -> bool:
    if arr is None or arr.size == 0:
        return True
    try:
        gray = arr.astype(np.float32)
        if gray.ndim == 3:
            gray = gray.mean(axis=2)
        mean = float(gray.mean())
        std = float(gray.std())
        p1 = float(np.percentile(gray, 1))
        p99 = float(np.percentile(gray, 99))
        bright_ratio = float((gray > 24.0).mean())
    except Exception:
        return False

    # 某些 Stripe / hCaptcha 场景下 canvas 已存在，但导出的位图几乎全黑/全空。
    # 这会导致 VLM / heuristic 都把 challenge 误判为“无可见区域”，因此要回退 body screenshot。
    if mean < 6.0 and p99 < 18.0:
        return True
    if std < 1.5 and (mean < 20.0 or mean > 245.0):
        return True
    if bright_ratio < 0.002 and (p99 - p1) < 8.0:
        return True
    return False


def get_body_raster(ch) -> tuple[np.ndarray, dict, str]:
    body_loc = ch.locator("body")
    body_box = body_loc.bounding_box(timeout=1_500)
    if not body_box:
        raise RuntimeError("challenge raster 不可用")
    raw = body_loc.screenshot(timeout=2_500)
    arr = np.array(Image.open(io.BytesIO(raw)).convert("RGB"))
    if arr.size == 0:
        raise RuntimeError("challenge raster 不可用")
    return arr, body_box, "body_screenshot"


def get_challenge_raster(ch) -> tuple[np.ndarray, dict, str]:
    try:
        canvas_box = ch.locator("canvas").bounding_box(timeout=1_500)
    except Exception:
        canvas_box = None
    if canvas_box:
        try:
            arr = get_canvas_image(ch)
            if not _raster_is_visually_empty(arr):
                return arr, canvas_box, "canvas"
        except Exception:
            pass

    return get_body_raster(ch)


def click_submit(ch, page=None):
    for sel in ["text=Next", "text=Verify", ".button-submit"]:
        try:
            loc = ch.locator(sel).first
            try:
                if page is not None:
                    box = loc.bounding_box(timeout=1_000)
                else:
                    box = None
            except Exception:
                box = None
            if page is not None and box:
                _human_click(
                    page,
                    float(box["x"] + box["width"] / 2.0),
                    float(box["y"] + box["height"] / 2.0),
                )
            else:
                loc.click(timeout=1_500, force=True)
            return True
        except Exception:
            continue
    return False


def save_debug_artifacts(out_dir: Path, round_idx: int, prompt: str, arr: np.ndarray, debug: dict | None):
    out_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(out_dir / f"round_{round_idx:02d}.png")
    image_counter = {"value": 0}

    def _sanitize(value: Any, *, key_hint: str = "debug") -> Any:
        if isinstance(value, np.ndarray):
            image_counter["value"] += 1
            suffix = f"{key_hint}_{image_counter['value']:02d}".replace("/", "_")
            path = out_dir / f"round_{round_idx:02d}_{suffix}.png"
            try:
                Image.fromarray(value).save(path)
                return {
                    "__type__": "ndarray_image",
                    "path": str(path),
                    "shape": list(value.shape),
                }
            except Exception:
                return {
                    "__type__": "ndarray",
                    "shape": list(value.shape),
                }
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            return float(value)
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(k): _sanitize(v, key_hint=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_sanitize(v, key_hint=key_hint) for v in value]
        return value

    meta = {"prompt": prompt, "debug": _sanitize(debug or {})}
    (out_dir / f"round_{round_idx:02d}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def solve_bridge(
    bridge_url: str,
    timeout_s: int,
    out_dir: Path,
    headless: bool = True,
    proxy_url: str = "",
    vlm_cfg: dict[str, Any] | None = None,
    verify_url: str = "",
    verify_form_base: dict[str, Any] | None = None,
    browser_locale: str = DEFAULT_LOCALE,
    browser_timezone: str = DEFAULT_TIMEZONE,
    accept_language: str = "en-US,en;q=0.9",
) -> dict | None:
    deadline = time.time() + timeout_s
    parsed_bridge_url = urllib.parse.urlsplit(bridge_url)
    bridge_origin = f"{parsed_bridge_url.scheme}://{parsed_bridge_url.netloc}" if parsed_bridge_url.scheme and parsed_bridge_url.netloc else ""
    effective_vlm_cfg = {
        "enabled": DEFAULT_VLM_ENABLED,
        "base_url": DEFAULT_VLM_BASE_URL,
        "api_key": DEFAULT_VLM_API_KEY,
        "model": DEFAULT_VLM_MODEL,
        "timeout_s": 45,
    }
    if vlm_cfg:
        effective_vlm_cfg.update({k: v for k, v in vlm_cfg.items() if v not in (None, "")})
    effective_vlm_cfg["enabled"] = bool(effective_vlm_cfg.get("enabled", DEFAULT_VLM_ENABLED))
    if effective_vlm_cfg["enabled"]:
        log(
            "VLM 已启用: "
            f"model={effective_vlm_cfg.get('model')} "
            f"base_url={effective_vlm_cfg.get('base_url')} "
            f"timeout_s={effective_vlm_cfg.get('timeout_s')}"
        )
    else:
        log("VLM 已禁用，回退到本地 heuristic solver")

    def _extract_checkcaptcha_ekey(url: str) -> str:
        try:
            parsed = urllib.parse.urlsplit(url)
            parts = [p for p in (parsed.path or "").split("/") if p]
            if len(parts) >= 3 and parts[0] == "checkcaptcha" and parts[2].startswith("E1_"):
                return parts[2]
        except Exception:
            pass
        return ""

    verify_url = (verify_url or "").strip()
    verify_form_base = dict(verify_form_base or {})

    def _write_session_meta(**extra):
        try:
            payload = {
                "created_at": int(time.time() * 1000),
                "bridge_url": bridge_url,
                "verify_url": verify_url,
                "headless": headless,
                "proxy_url": proxy_url,
                **extra,
            }
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"session_meta_{int(time.time() * 1000)}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    _write_session_meta(
        phase="startup",
        use_synthetic_stripe_origin=False,
        synthetic_bridge_url="",
        synthetic_wrapper_url="",
    )

    def _local_http_request(method: str, url: str, **kwargs):
        session = requests.Session()
        session.trust_env = False
        try:
            return session.request(method, url, timeout=kwargs.pop("timeout", 10), **kwargs)
        finally:
            session.close()

    def _report_to_bridge(path: str, payload: dict[str, Any]):
        if not bridge_origin:
            return
        try:
            _local_http_request(
                "POST",
                urllib.parse.urljoin(bridge_origin, path),
                json=payload,
                timeout=5,
            )
        except Exception as e:
            log(f"回传本地 bridge {path} 失败，忽略: {e}")

    def _load_bridge_html() -> str:
        resp = _local_http_request("GET", bridge_url, timeout=15)
        resp.raise_for_status()
        return resp.text

    def _prepare_synthetic_stripe_bridge(html_text: str) -> tuple[str, str] | tuple[None, None]:
        m = re.search(r'<iframe[^>]+id="stripeCaptchaFrame"[^>]+src="([^"]+)"', html_text)
        if not m:
            return None, None
        original_wrapper_url = m.group(1)
        parsed_wrapper = urllib.parse.urlsplit(original_wrapper_url)
        q = urllib.parse.parse_qs(parsed_wrapper.query, keep_blank_values=True)
        q["origin"] = ["https://js.stripe.com"]
        rebuilt_query = urllib.parse.urlencode(q, doseq=True, safe=":/")
        synthetic_wrapper_url = urllib.parse.urlunsplit(
            (
                parsed_wrapper.scheme,
                parsed_wrapper.netloc,
                parsed_wrapper.path,
                rebuilt_query,
                parsed_wrapper.fragment,
            )
        )
        synthetic_html = html_text.replace(original_wrapper_url, synthetic_wrapper_url, 1)
        return synthetic_html, synthetic_wrapper_url

    use_synthetic_stripe_origin = bool(
        verify_url
        and parsed_bridge_url.hostname in {"127.0.0.1", "localhost"}
    )
    synthetic_bridge_html = ""
    synthetic_wrapper_url = ""
    synthetic_bridge_url = "https://js.stripe.com/"
    if use_synthetic_stripe_origin:
        try:
            original_bridge_html = _load_bridge_html()
            synthetic_bridge_html, synthetic_wrapper_url = _prepare_synthetic_stripe_bridge(original_bridge_html)
            if not synthetic_bridge_html:
                use_synthetic_stripe_origin = False
            else:
                log(
                    "启用 synthetic Stripe origin bridge: "
                    f"top={synthetic_bridge_url} wrapper={synthetic_wrapper_url}"
                )
        except Exception as e:
            log(f"准备 synthetic Stripe origin bridge 失败，回退本地 bridge: {e}")
            use_synthetic_stripe_origin = False

    _write_session_meta(
        phase="post_synthetic_prepare",
        use_synthetic_stripe_origin=use_synthetic_stripe_origin,
        synthetic_bridge_url=synthetic_bridge_url if use_synthetic_stripe_origin else "",
        synthetic_wrapper_url=synthetic_wrapper_url if use_synthetic_stripe_origin else "",
    )

    with sync_playwright() as p:
        launch_kwargs = {"headless": headless}
        proxy_url = (proxy_url or "").strip()
        if proxy_url:
            parsed = urllib.parse.urlsplit(proxy_url)
            proxy_host = parsed.hostname or ""
            proxy_scheme = parsed.scheme or "http"
            proxy_port = parsed.port
            if proxy_host:
                server = f"{proxy_scheme}://{proxy_host}"
                if proxy_port:
                    server += f":{proxy_port}"
                proxy_cfg = {
                    "server": server,
                    "bypass": "127.0.0.1,localhost",
                }
                proxy_user = urllib.parse.unquote(parsed.username or "")
                proxy_pass = urllib.parse.unquote(parsed.password or "")
                if proxy_user:
                    proxy_cfg["username"] = proxy_user
                if proxy_pass:
                    proxy_cfg["password"] = proxy_pass
                launch_kwargs["proxy"] = proxy_cfg
                log(f"使用浏览器代理: {proxy_host}:{proxy_port}")
        browser = p.chromium.launch(**launch_kwargs)
        browser_locale = str(browser_locale or DEFAULT_LOCALE)
        browser_timezone = str(browser_timezone or DEFAULT_TIMEZONE)
        accept_language = str(accept_language or f"{browser_locale},{browser_locale.split('-', 1)[0]};q=0.9")
        extra_http_headers = {
            "Accept-Language": accept_language,
            "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Priority": "u=1, i",
        }
        context = browser.new_context(
            viewport={"width": 1280, "height": 960},
            user_agent=DEFAULT_USER_AGENT,
            locale=browser_locale,
            timezone_id=browser_timezone,
            color_scheme="light",
            extra_http_headers=extra_http_headers,
        )
        spoof_cfg = {
            "userAgent": DEFAULT_USER_AGENT,
            "appVersion": DEFAULT_USER_AGENT.replace("Mozilla/", ""),
            "language": browser_locale,
            "languages": [browser_locale, browser_locale.split("-", 1)[0]],
            "userAgentData": {
                "brands": [
                    {"brand": "Chromium", "version": "146"},
                    {"brand": "Not-A.Brand", "version": "24"},
                    {"brand": "Google Chrome", "version": "146"},
                ],
                "mobile": False,
                "platform": "Windows",
            },
        }
        init_script = """
            (() => {{
              const cfg = {cfg_json};
              const spoofValue = (obj, prop, value) => {{
                try {{
                  Object.defineProperty(obj, prop, {{
                    get: () => value,
                    configurable: true,
                  }});
                }} catch (e) {{}}
              }};
              spoofValue(Navigator.prototype, 'webdriver', false);
              spoofValue(Navigator.prototype, 'platform', 'Win32');
              spoofValue(Navigator.prototype, 'language', cfg.language);
              spoofValue(Navigator.prototype, 'languages', cfg.languages);
              spoofValue(Navigator.prototype, 'userAgent', cfg.userAgent);
              spoofValue(Navigator.prototype, 'appVersion', cfg.appVersion);
              spoofValue(Navigator.prototype, 'hardwareConcurrency', 8);
              spoofValue(Navigator.prototype, 'deviceMemory', 8);
              try {{
                Object.defineProperty(Navigator.prototype, 'userAgentData', {{
                  get: () => cfg.userAgentData,
                  configurable: true,
                }});
              }} catch (e) {{}}
              spoofValue(Screen.prototype, 'width', 1272);
              spoofValue(Screen.prototype, 'height', 716);
              spoofValue(Screen.prototype, 'availWidth', 1272);
              spoofValue(Screen.prototype, 'availHeight', 684);
              spoofValue(Screen.prototype, 'colorDepth', 32);
              spoofValue(Screen.prototype, 'pixelDepth', 32);
              spoofValue(window, 'outerWidth', 1272);
              spoofValue(window, 'outerHeight', 716);
            }})();
            """.format(cfg_json=json.dumps(spoof_cfg, ensure_ascii=False))
        context.add_init_script(init_script)
        if use_synthetic_stripe_origin and synthetic_bridge_html:
            def _synthetic_route_handler(route):
                req = route.request
                url = req.url
                if url == synthetic_bridge_url and req.is_navigation_request():
                    return route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=synthetic_bridge_html,
                    )
                parsed = urllib.parse.urlsplit(url)
                if parsed.netloc == "js.stripe.com" and parsed.path in {"/event", "/result", "/cancel", "/error"}:
                    if parsed.path == "/result":
                        try:
                            payload = json.loads(req.post_data or "{}")
                            _report_to_bridge("/result", payload)
                        except Exception:
                            pass
                    return route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body='{"ok":true}',
                    )
                if parsed.netloc == "js.stripe.com" and parsed.path == "/favicon.ico":
                    return route.fulfill(status=204, body="")
                return route.continue_()

            context.route("https://js.stripe.com/**", _synthetic_route_handler)
        page = context.new_page()
        observed_network_solution: dict[str, str] = {}
        observed_challenge_state: dict[str, Any] = {
            "prompt": "",
            "request_type": "",
            "task_count": 0,
            "ekey": "",
            "last_getcaptcha_url": "",
            "updated_at": 0.0,
        }

        def _publish_network_solution(payload: dict):
            response_token = str(payload.get("response") or "")
            response_ekey = str(payload.get("ekey") or "")
            if not response_token or not response_ekey:
                return
            if (
                observed_network_solution.get("response") == response_token
                and observed_network_solution.get("ekey") == response_ekey
            ):
                return
            observed_network_solution.clear()
            observed_network_solution.update(payload)
            log(
                "监听到 hCaptcha checkcaptcha pass=true，"
                f"直接回填真实 P1/E1 (token={len(response_token)} chars, ekey={len(response_ekey)} chars)"
            )
            try:
                page.evaluate(
                    """async (payload) => {
                        window.__stripeChallengeResult = payload;
                        try {
                            await fetch('/result', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(payload),
                                keepalive: true,
                            });
                        } catch (e) {}
                    }""",
                    payload,
                )
            except Exception as e:
                log(f"回填 bridge result 失败，忽略: {e}")
            _report_to_bridge("/result", payload)

        def _push_augmented_result(payload: dict[str, Any]):
            try:
                page.evaluate(
                    """async (payload) => {
                        window.__stripeChallengeResult = payload;
                        try {
                            await fetch('/result', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(payload),
                                keepalive: true,
                            });
                        } catch (e) {}
                    }""",
                    payload,
                )
            except Exception as e:
                log(f"回填增强 bridge result 失败，忽略: {e}")
            _report_to_bridge("/result", payload)

        def _persist_checkcaptcha_snapshot(resp, data: dict[str, Any], *, pass_value: Any, ekey: str):
            try:
                req = resp.request
                req_body = req.post_data or ""
                parsed_req = json.loads(req_body) if req_body else {}
                motion = {}
                if isinstance(parsed_req, dict):
                    try:
                        motion = json.loads(parsed_req.get("motionData") or "{}")
                    except Exception:
                        motion = {}
                top = motion.get("topLevel") or {}
                snapshot = {
                    "url": resp.url,
                    "request_headers": dict(req.headers),
                    "response_headers": dict(resp.headers),
                    "pass": pass_value,
                    "generated_pass_len": len(str(data.get("generated_pass_UUID") or "")),
                    "ekey_len": len(ekey or ""),
                    "request_json": parsed_req,
                    "response_json": data,
                    "motion_summary": {
                        "mm_len": len(motion.get("mm") or []),
                        "md_len": len(motion.get("md") or []),
                        "mu_len": len(motion.get("mu") or []),
                        "pm_len": len(motion.get("pm") or []),
                        "top_mm_len": len(top.get("mm") or []),
                        "top_xy_len": len(top.get("xy") or []),
                        "top_mm_mp": top.get("mm-mp"),
                        "top_xy_mp": top.get("xy-mp"),
                        "top_lpt": top.get("lpt"),
                        "top_dr": top.get("dr"),
                    },
                }
                out_dir.mkdir(parents=True, exist_ok=True)
                stamp = int(time.time() * 1000)
                suffix = "pass" if pass_value is True else "fail"
                (out_dir / f"checkcaptcha_{suffix}_{stamp}.json").write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                log(f"保存 checkcaptcha 快照失败，忽略: {e}")

        def _handle_response(resp):
            try:
                url = resp.url or ""
                host = (urllib.parse.urlsplit(url).hostname or "").lower()
                if "hcaptcha.com" not in host:
                    return

                if "getcaptcha/" in url or "/getcaptcha?" in url:
                    data = resp.json()
                    if not isinstance(data, dict):
                        return
                    prompt = _extract_prompt_from_hcaptcha_payload(data)
                    request_type = str(data.get("request_type") or data.get("mode") or "").strip()
                    task_count = 0
                    tasklist = data.get("tasklist")
                    if isinstance(tasklist, list):
                        task_count = len(tasklist)
                    observed_challenge_state.update(
                        {
                            "prompt": prompt or observed_challenge_state.get("prompt") or "",
                            "request_type": request_type,
                            "task_count": task_count,
                            "last_getcaptcha_url": url,
                            "updated_at": time.time(),
                        }
                    )
                    key_hint = str(data.get("key") or data.get("generated_pass_UUID") or "").strip()
                    if key_hint.startswith("E1_"):
                        observed_challenge_state["ekey"] = key_hint
                    log(
                        "观察到 hCaptcha getcaptcha："
                        f" type={request_type or '?'}"
                        f" tasks={task_count}"
                        f" prompt={prompt!r}"
                    )
                    return

                if "checkcaptcha/" not in url:
                    return
                if resp.request.method.upper() != "POST":
                    return
                ekey = _extract_checkcaptcha_ekey(url)
                if not ekey:
                    return
                observed_challenge_state["ekey"] = ekey
                data = resp.json()
                if not isinstance(data, dict):
                    return

                pass_value = data.get("pass")
                if pass_value is False:
                    _persist_checkcaptcha_snapshot(resp, data, pass_value=pass_value, ekey=ekey)
                    next_req = ((data.get("c") or {}).get("req") or "")
                    if next_req:
                        log(
                            "观察到 hCaptcha checkcaptcha pass=false，"
                            f"服务端已下发下一轮 challenge (ekey={ekey[:24]}...)"
                        )
                    else:
                        log(
                            "观察到 hCaptcha checkcaptcha pass=false，"
                            f"本轮未通过 (ekey={ekey[:24]}...)"
                        )
                    return

                generated_pass = str(data.get("generated_pass_UUID") or "")
                if pass_value is True and generated_pass:
                    _persist_checkcaptcha_snapshot(resp, data, pass_value=pass_value, ekey=ekey)
                    _publish_network_solution(
                        {
                            "type": "response",
                            "response": generated_pass,
                            "ekey": ekey,
                            "raw": {
                                "source": "network_checkcaptcha",
                                "url": url,
                                "expiration": data.get("expiration"),
                            },
                        }
                    )
            except Exception:
                return

        def _browser_verify(solution: dict[str, Any]) -> dict[str, Any] | None:
            if not verify_url or not verify_form_base:
                return None

            form = {k: str(v) for k, v in verify_form_base.items() if v not in (None, "")}
            form["challenge_response_token"] = str(solution.get("response") or "")
            ekey_value = str(solution.get("ekey") or "")
            if ekey_value:
                form["challenge_response_ekey"] = ekey_value

            verify_page = context.new_page()
            try:
                target = synthetic_bridge_url if use_synthetic_stripe_origin else "https://js.stripe.com/"
                log(f"尝试在浏览器内执行 verify_challenge: {verify_url}")
                verify_page.goto(target, wait_until="domcontentloaded", timeout=30_000)
                result = verify_page.evaluate(
                    """async ({url, form}) => {
                        const body = new URLSearchParams();
                        for (const [k, v] of Object.entries(form || {})) {
                            if (v !== undefined && v !== null) {
                                body.append(k, String(v));
                            }
                        }
                        const resp = await fetch(url, {
                            method: "POST",
                            headers: {
                                "Accept": "application/json",
                                "Content-Type": "application/x-www-form-urlencoded",
                            },
                            body,
                            credentials: "omit",
                        });
                        const text = await resp.text();
                        let json = null;
                        try {
                            json = JSON.parse(text);
                        } catch (e) {}
                        return {
                            status: resp.status,
                            ok: resp.ok,
                            url: resp.url,
                            text,
                            json,
                        };
                    }""",
                    {"url": verify_url, "form": form},
                )
                status = int(result.get("status") or 0)
                text = str(result.get("text") or "")
                log(
                    "浏览器内 verify_challenge 完成: "
                    f"status={status} body_len={len(text)}"
                )
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    stamp = int(time.time() * 1000)
                    (out_dir / f"browser_verify_{stamp}.json").write_text(
                        json.dumps(
                            {
                                "verify_url": verify_url,
                                "form_keys": sorted(form.keys()),
                                "form_lengths": {k: len(str(v)) for k, v in form.items()},
                                "result": result,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                return result
            except Exception as e:
                log(f"浏览器内 verify_challenge 失败，忽略并回退外层: {e}")
                return None
            finally:
                try:
                    verify_page.close()
                except Exception:
                    pass

        context.on("response", _handle_response)
        page.on("response", _handle_response)
        page_target = synthetic_bridge_url if use_synthetic_stripe_origin else bridge_url
        page.goto(page_target, wait_until="domcontentloaded", timeout=60_000)
        round_idx = 0
        last_action_key = None
        last_wait_log_at = 0.0
        prompt_attempt_counts: dict[str, int] = {}

        try:
            while time.time() < deadline:
                if observed_network_solution:
                    verify_result = _browser_verify(observed_network_solution)
                    if verify_result is not None:
                        observed_network_solution.setdefault("raw", {})
                        observed_network_solution["raw"]["browser_verify"] = verify_result
                        _push_augmented_result(observed_network_solution)
                    log("challenge 已完成，拿到网络侧真实 checkcaptcha 结果")
                    return dict(observed_network_solution)
                result = page.evaluate("window.__stripeChallengeResult")
                if result:
                    if str((result.get("raw") or {}).get("source") or "") != "network_checkcaptcha":
                        wait_deadline = time.time() + 1.2
                        while time.time() < wait_deadline and not observed_network_solution:
                            page.wait_for_timeout(100)
                        if observed_network_solution:
                            log("bridge result 已出现，但随后抓到网络侧真实 checkcaptcha，优先使用网络侧结果")
                            verify_result = _browser_verify(observed_network_solution)
                            if verify_result is not None:
                                observed_network_solution.setdefault("raw", {})
                                observed_network_solution["raw"]["browser_verify"] = verify_result
                                _push_augmented_result(observed_network_solution)
                            return dict(observed_network_solution)
                    if observed_network_solution:
                        same = (
                            str(result.get("response") or "") == str(observed_network_solution.get("response") or "")
                            and str(result.get("ekey") or "") == str(observed_network_solution.get("ekey") or "")
                        )
                        if not same:
                            log("bridge result 与网络侧 P1/E1 不一致，优先使用网络侧结果")
                            verify_result = _browser_verify(observed_network_solution)
                            if verify_result is not None:
                                observed_network_solution.setdefault("raw", {})
                                observed_network_solution["raw"]["browser_verify"] = verify_result
                                _push_augmented_result(observed_network_solution)
                            return dict(observed_network_solution)
                    verify_result = _browser_verify(result)
                    if verify_result is not None:
                        result.setdefault("raw", {})
                        result["raw"]["browser_verify"] = verify_result
                        _push_augmented_result(result)
                    log("challenge 已完成，拿到 bridge result")
                    return result

                ch = challenge_frame(page)
                if ch is None:
                    if click_checkbox(page):
                        log("已点击 checkbox，等待 challenge ...")
                    elif time.time() - last_wait_log_at >= 5:
                        log("等待 challenge frame 出现 ...")
                        last_wait_log_at = time.time()
                    page.wait_for_timeout(1000)
                    continue

                network_prompt = observed_challenge_state.get("prompt") or ""
                request_type = str(observed_challenge_state.get("request_type") or "").strip().lower()
                prompt_dom = get_prompt(ch, network_prompt)
                prompt = resolve_effective_prompt(
                    prompt_dom,
                    network_prompt,
                    request_type=request_type,
                    network_updated_at=float(observed_challenge_state.get("updated_at") or 0.0),
                )
                if prompt and prompt != prompt_dom:
                    log(
                        "检测到 DOM prompt 与网络题面不一致，优先使用网络题面: "
                        f"dom={prompt_dom!r} network={network_prompt!r} request_type={request_type!r}"
                    )
                if not prompt:
                    if click_checkbox(page):
                        log("prompt 为空，补点 checkbox ...")
                    elif time.time() - last_wait_log_at >= 5:
                        try:
                            body_preview = ch.locator("body").inner_text(timeout=1_000)[:120]
                        except Exception:
                            body_preview = "<body unavailable>"
                        log(
                            "等待题目文本加载 ..."
                            f" body={body_preview!r}"
                            f" network_prompt={observed_challenge_state.get('prompt')!r}"
                            f" request_type={observed_challenge_state.get('request_type')!r}"
                        )
                        last_wait_log_at = time.time()
                    page.wait_for_timeout(1000)
                    continue

                try:
                    arr, raster_box, raster_source = get_challenge_raster(ch)
                except Exception:
                    if time.time() - last_wait_log_at >= 5:
                        log(f"等待 challenge 图像就绪 ... prompt={prompt!r}")
                        last_wait_log_at = time.time()
                    page.wait_for_timeout(1000)
                    continue
                if (not _looks_like_full_grid_fallback(arr)):
                    _cur_request_type = str(observed_challenge_state.get("request_type") or "").strip().lower()
                    _non_grid_type = _cur_request_type in ("image_label_area_select",)
                    if not _non_grid_type and (time.time() - last_wait_log_at) < 3.0:
                        page.wait_for_timeout(1200)
                        continue
                    if time.time() - last_wait_log_at >= 5:
                        log(f"challenge 图像非标准网格，放行 ... prompt={prompt!r} source={raster_source!r} type={_cur_request_type!r}")
                        last_wait_log_at = time.time()
                submit_text = get_submit_text(ch)
                log(
                    f"round {round_idx + 1}: prompt={prompt!r} "
                    f"submit={submit_text!r} source={raster_source!r} "
                    f"request_type={observed_challenge_state.get('request_type')!r} "
                    f"tasks={observed_challenge_state.get('task_count')!r}"
                )

                action: SolverAction | None = None
                vlm_error: str | None = None
                task_count = int(observed_challenge_state.get("task_count") or 0)
                try:
                    if effective_vlm_cfg.get("enabled"):
                        try:
                            if is_drag_completion_prompt(prompt):
                                log("  尝试 VLM drag 决策 ...")
                                action = _build_vlm_drag_action(
                                    prompt,
                                    arr,
                                    submit_text=submit_text,
                                    vlm_cfg=effective_vlm_cfg,
                                )
                            else:
                                log("  尝试 VLM click 决策 ...")
                                action = _build_vlm_click_action(
                                    prompt,
                                    arr,
                                    submit_text=submit_text,
                                    vlm_cfg=effective_vlm_cfg,
                                )
                            if action and action.debug is not None:
                                action.debug["solver_path"] = "vlm_first"
                        except Exception as e:
                            vlm_error = str(e)
                            log(f"  VLM 候选框决策失败: {vlm_error}")
                            if is_drag_completion_prompt(prompt):
                                try:
                                    log("  尝试 VLM 直出拖拽坐标 ...")
                                    action = _build_vlm_direct_drag_action(
                                        prompt,
                                        arr,
                                        submit_text=submit_text,
                                        vlm_cfg=effective_vlm_cfg,
                                    )
                                    if action.debug is not None:
                                        action.debug["solver_path"] = "vlm_direct_drag_fallback"
                                except Exception as direct_drag_e:
                                    vlm_error = f"{vlm_error}; direct_drag={direct_drag_e}"
                                    log(f"  VLM 直出拖拽坐标也失败，回退 heuristic: {direct_drag_e}")
                            else:
                                try:
                                    direct_expected_count = None
                                    if request_type == "image_label_area_select" and task_count > 0:
                                        direct_expected_count = task_count
                                    log("  尝试 VLM 直出点击坐标 ...")
                                    action = _build_vlm_direct_click_action(
                                        prompt,
                                        arr,
                                        submit_text=submit_text,
                                        vlm_cfg=effective_vlm_cfg,
                                        expected_count=direct_expected_count,
                                    )
                                    if action.debug is not None:
                                        action.debug["solver_path"] = "vlm_direct_fallback"
                                    if is_water_vehicle_prompt(prompt):
                                        try:
                                            heuristic_alt = solve_water_travel(
                                                arr,
                                                prompt,
                                                expected_count=direct_expected_count,
                                                prefer_object_candidates=(request_type == "image_label_area_select"),
                                            )
                                            alt_sets = list((heuristic_alt.debug or {}).get("candidate_click_sets") or [])
                                            merged_sets = list((action.debug or {}).get("candidate_click_sets") or [])
                                            for click_set in alt_sets:
                                                _append_unique_click_set(merged_sets, click_set)
                                            action.debug["candidate_click_sets"] = merged_sets
                                            action.debug["heuristic_alt_sets"] = alt_sets
                                        except Exception as heuristic_alt_error:
                                            action.debug["heuristic_alt_error"] = str(heuristic_alt_error)
                                except Exception as direct_e:
                                    vlm_error = f"{vlm_error}; direct={direct_e}"
                                    log(f"  VLM 直出坐标也失败，回退 heuristic: {direct_e}")

                    if action is None:
                        if is_water_vehicle_prompt(prompt):
                            expected_count = None
                            prefer_object_candidates = request_type == "image_label_area_select"
                            if request_type == "image_label_area_select" and task_count > 0:
                                expected_count = task_count
                            action = solve_water_travel(
                                arr,
                                prompt,
                                expected_count=expected_count,
                                prefer_object_candidates=prefer_object_candidates,
                            )
                        elif is_road_completion_prompt(prompt):
                            action = solve_road_completion(arr, prompt)
                        elif is_drag_completion_prompt(prompt):
                            action = solve_pair_drag(arr, prompt)
                        elif is_float_on_water_prompt(prompt):
                            action = solve_float_on_water(arr, prompt)
                        elif is_heat_work_prompt(prompt):
                            action = solve_heat_work(arr, prompt)
                        elif is_hot_food_prompt(prompt):
                            action = solve_hot_food(arr, prompt)
                        elif is_hop_animals_prompt(prompt):
                            action = solve_hop_animals(arr, prompt)
                        elif is_shiny_thing_prompt(prompt):
                            action = solve_shiny_thing(arr, prompt)
                        elif is_kept_outside_prompt(prompt):
                            action = solve_kept_outside(arr, prompt)
                        elif is_dissolve_melt_prompt(prompt):
                            action = solve_dissolve_melt(arr, prompt)
                        elif is_hidden_under_reference_prompt(prompt):
                            action = solve_hidden_under_reference(arr, prompt)
                        else:
                            save_debug_artifacts(
                                out_dir,
                                round_idx + 1,
                                prompt,
                                arr,
                                {
                                    "submit_text": submit_text,
                                    "error": "unknown_prompt",
                                    "vlm_error": vlm_error,
                                },
                            )
                            raise RuntimeError(f"未知 challenge prompt: {prompt}")
                        if action.debug is None:
                            action.debug = {}
                        action.debug.setdefault("solver_path", "heuristic")
                        if vlm_error:
                            action.debug["vlm_error"] = vlm_error
                except RuntimeError as e:
                    msg = str(e)
                    transient_errors = [
                        "canvas 中未检测到可见区域",
                        "未能稳定识别 3 个内容带",
                        "未识别到 road 候选块",
                        "未识别到足够的 road 候选块",
                        "未识别到 dissolve/melt 候选框",
                        "未识别到可点击候选物体",
                    ]
                    if any(token in msg for token in transient_errors):
                        if time.time() - last_wait_log_at >= 5:
                            log(f"题面内容尚未稳定，等待重试 ... prompt={prompt!r} err={msg}")
                            last_wait_log_at = time.time()
                        page.wait_for_timeout(1000)
                        continue
                    save_debug_artifacts(out_dir, round_idx + 1, prompt, arr, {"submit_text": submit_text, "error": msg})
                    raise

                image_hash = hashlib.sha1(arr.tobytes()).hexdigest()[:16]
                attempt_key = f"{prompt}\n{image_hash}"

                if action.kind == "drag":
                    attempt_index = int(prompt_attempt_counts.get(attempt_key, 0))
                    drag_starts = build_drag_start_variations(action)
                    drag_targets = build_drag_target_variations(action)
                    total_variations = len(drag_starts) * len(drag_targets)
                    if attempt_index >= total_variations:
                        save_debug_artifacts(
                            out_dir,
                            round_idx + 1,
                            prompt,
                            arr,
                            {
                                **(action.debug or {}),
                                "submit_text": submit_text,
                                "error": "drag_variations_exhausted",
                                "attempt_index": attempt_index,
                                "drag_variations_total": len(drag_targets),
                                "drag_start_variations_total": len(drag_starts),
                                "drag_combo_variations_total": total_variations,
                            },
                        )
                        raise RuntimeError("drag candidate 已耗尽，疑似卡死")
                    target_index = attempt_index % len(drag_targets)
                    start_index = attempt_index // len(drag_targets)
                    action.drag_from = drag_starts[start_index]
                    action.drag_to = drag_targets[target_index]
                    if action.debug is None:
                        action.debug = {}
                    action.debug["attempt_index"] = attempt_index
                    action.debug["drag_variations_total"] = len(drag_targets)
                    action.debug["drag_start_variations_total"] = len(drag_starts)
                    action.debug["drag_combo_variations_total"] = total_variations
                    action.debug["selected_drag_from"] = action.drag_from
                    action.debug["selected_drag_to"] = action.drag_to
                elif action.kind == "click_one":
                    attempt_index = int(prompt_attempt_counts.get(attempt_key, 0))
                    candidate_click_points = list((action.debug or {}).get("candidate_click_points") or [])
                    if attempt_index >= len(candidate_click_points):
                        save_debug_artifacts(
                            out_dir,
                            round_idx + 1,
                            prompt,
                            arr,
                            {
                                **(action.debug or {}),
                                "submit_text": submit_text,
                                "error": "click_variations_exhausted",
                                "attempt_index": attempt_index,
                                "click_variations_total": len(candidate_click_points),
                            },
                        )
                        raise RuntimeError("click candidate 已耗尽，疑似卡死")
                    action.click_points = [candidate_click_points[attempt_index]]
                    if action.debug is None:
                        action.debug = {}
                    action.debug["attempt_index"] = attempt_index
                    action.debug["click_variations_total"] = len(candidate_click_points)
                    action.debug["selected_click_point"] = action.click_points[0]
                elif action.kind == "click":
                    attempt_index = int(prompt_attempt_counts.get(attempt_key, 0))
                    candidate_click_sets = list((action.debug or {}).get("candidate_click_sets") or [])
                    if candidate_click_sets:
                        if attempt_index >= len(candidate_click_sets):
                            save_debug_artifacts(
                                out_dir,
                                round_idx + 1,
                                prompt,
                                arr,
                                {
                                    **(action.debug or {}),
                                    "submit_text": submit_text,
                                    "raster_source": raster_source,
                                    "error": "click_set_variations_exhausted",
                                    "attempt_index": attempt_index,
                                    "click_set_variations_total": len(candidate_click_sets),
                                },
                            )
                            raise RuntimeError("click set candidate 已耗尽，疑似卡死")
                        selected_set = candidate_click_sets[attempt_index]
                        action.click_points = [(float(x), float(y)) for x, y in selected_set]
                        if action.debug is None:
                            action.debug = {}
                        action.debug["attempt_index"] = attempt_index
                        action.debug["click_set_variations_total"] = len(candidate_click_sets)
                        action.debug["selected_click_set"] = action.click_points

                prompt_attempt_counts[attempt_key] = int(prompt_attempt_counts.get(attempt_key, 0)) + 1

                action_key = json.dumps(
                    {
                        "prompt": action.prompt,
                        "image_hash": image_hash,
                        "raster_source": raster_source,
                        "kind": action.kind,
                        "click_points": action.click_points,
                        "drag_from": action.drag_from,
                        "drag_to": action.drag_to,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if action_key == last_action_key:
                    save_debug_artifacts(out_dir, round_idx + 1, prompt, arr, {**(action.debug or {}), "submit_text": submit_text, "error": "repeated_action"})
                    raise RuntimeError("solver 动作重复，疑似卡死")
                last_action_key = action_key

                save_debug_artifacts(
                    out_dir,
                    round_idx + 1,
                    prompt,
                    arr,
                    {
                        **(action.debug or {}),
                        "submit_text": submit_text,
                        "raster_source": raster_source,
                        "image_hash": image_hash,
                        "dom_prompt": prompt_dom,
                        "effective_prompt": prompt,
                        "network_prompt": observed_challenge_state.get("prompt"),
                        "network_request_type": observed_challenge_state.get("request_type"),
                        "network_task_count": observed_challenge_state.get("task_count"),
                        "network_ekey": observed_challenge_state.get("ekey"),
                    },
                )

                visible = extract_visible_canvas(arr)
                if action.kind in {"click", "click_one"}:
                    points = []
                    click_feedback: list[dict[str, Any]] = []
                    current_arr = arr
                    current_raster_box = raster_box
                    current_raster_source = raster_source
                    for pt in action.click_points or []:
                        points.append(image_to_canvas_point(pt, current_arr.shape, visible.y_offset, current_raster_box))
                    log(f"  点击 tile 点位: {points}")
                    for idx, pt in enumerate(action.click_points or []):
                        landed, next_arr, next_box, next_source, metrics = _click_image_point_with_feedback(
                            page,
                            ch,
                            arr=current_arr,
                            raster_box=current_raster_box,
                            img_point=pt,
                            prompt_hint=prompt,
                            submit_text_hint=submit_text,
                        )
                        click_feedback.append(metrics)
                        if landed:
                            current_arr = next_arr
                            current_raster_box = next_box
                            current_raster_source = next_source
                            log(
                                "  点击已落地 "
                                f"#{idx + 1}: changed_pixels={metrics.get('changed_pixels')} "
                                f"mean_diff={round(float(metrics.get('mean_diff') or 0.0), 4)} "
                                f"bbox={metrics.get('bbox')}"
                            )
                        else:
                            log(
                                "  点击未观察到明显状态变化 "
                                f"#{idx + 1}: changed_pixels={metrics.get('changed_pixels')} "
                                f"mean_diff={round(float(metrics.get('mean_diff') or 0.0), 4)} "
                                f"bbox={metrics.get('bbox')}"
                            )
                    if action.debug is None:
                        action.debug = {}
                    action.debug["click_feedback"] = click_feedback
                    if click_submit(ch, page=page):
                        log("  已点击 Next/Verify")
                        page.wait_for_timeout(1800)
                elif action.kind == "drag":
                    start = image_to_canvas_point(action.drag_from, arr.shape, visible.y_offset, raster_box)
                    end = image_to_canvas_point(action.drag_to, arr.shape, visible.y_offset, raster_box)
                    log(f"  拖拽 source -> target: {start} -> {end}")
                    landed, next_arr, next_box, next_source, drag_metrics = _drag_with_feedback(
                        page,
                        ch,
                        start=start,
                        end=end,
                        arr=arr,
                        raster_box=raster_box,
                    )
                    if action.debug is None:
                        action.debug = {}
                    action.debug["drag_feedback"] = drag_metrics
                    if landed:
                        log(
                            "  拖拽后观察到状态变化 "
                            f"changed_pixels={drag_metrics.get('changed_pixels')} "
                            f"mean_diff={round(float(drag_metrics.get('mean_diff') or 0.0), 4)} "
                            f"bbox={drag_metrics.get('bbox')}"
                        )
                    else:
                        log(
                            "  拖拽后未观察到明显状态变化 "
                            f"changed_pixels={drag_metrics.get('changed_pixels')} "
                            f"mean_diff={round(float(drag_metrics.get('mean_diff') or 0.0), 4)} "
                            f"bbox={drag_metrics.get('bbox')}"
                        )
                    page.wait_for_timeout(1200)
                    new_submit = get_submit_text(ch)
                    if new_submit and new_submit.lower() in {"next", "verify"}:
                        if click_submit(ch, page=page):
                            log(f"  拖拽后点击 {new_submit}")
                            page.wait_for_timeout(1500)
                else:
                    raise RuntimeError(f"不支持的 solver action: {action.kind}")

                save_debug_artifacts(
                    out_dir,
                    round_idx + 1,
                    prompt,
                    arr,
                    {
                        **(action.debug or {}),
                        "submit_text": submit_text,
                        "raster_source": raster_source,
                        "image_hash": image_hash,
                        "dom_prompt": prompt_dom,
                        "effective_prompt": prompt,
                        "network_prompt": observed_challenge_state.get("prompt"),
                        "network_request_type": observed_challenge_state.get("request_type"),
                        "network_task_count": observed_challenge_state.get("task_count"),
                        "network_ekey": observed_challenge_state.get("ekey"),
                    },
                )

                round_idx += 1

            raise TimeoutError(f"solver 超时 ({timeout_s}s)")
        finally:
            context.close()
            browser.close()


def main():
    parser = argparse.ArgumentParser(description="Auto-solve Stripe hCaptcha bridge")
    parser.add_argument("bridge_url", help="例如 http://127.0.0.1:42625/index.html")
    parser.add_argument("--timeout", type=int, default=180, help="求解超时秒数")
    parser.add_argument("--out-dir", default="/tmp/hcaptcha_auto_solver", help="调试截图输出目录")
    parser.add_argument("--headed", action="store_true", help="用有头浏览器运行")
    parser.add_argument("--proxy-url", default="", help="浏览器代理 URL，如 http://user:pass@host:port")
    parser.add_argument("--no-vlm", action="store_true", help="禁用 GPT-5.4 VLM 决策层")
    parser.add_argument(
        "--vlm-base-url",
        default=os.environ.get("CTF_VLM_BASE_URL", DEFAULT_VLM_BASE_URL),
        help="OpenAI-compatible VLM base_url",
    )
    parser.add_argument(
        "--vlm-api-key",
        default=os.environ.get("CTF_VLM_API_KEY", DEFAULT_VLM_API_KEY),
        help="OpenAI-compatible VLM api_key",
    )
    parser.add_argument(
        "--vlm-model",
        default=os.environ.get("CTF_VLM_MODEL", DEFAULT_VLM_MODEL),
        help="VLM model name",
    )
    parser.add_argument(
        "--vlm-timeout",
        type=int,
        default=int(os.environ.get("CTF_VLM_TIMEOUT", "45") or 45),
        help="单次 VLM 请求超时秒数",
    )
    parser.add_argument("--verify-url", default="", help="可选：在同一浏览器上下文内执行 Stripe verify_challenge")
    parser.add_argument("--verify-client-secret", default="", help="verify_challenge 所需 client_secret")
    parser.add_argument("--verify-key", default="", help="verify_challenge 所需 publishable key")
    parser.add_argument("--verify-stripe-version", default="", help="verify_challenge 所需 _stripe_version")
    parser.add_argument("--verify-captcha-vendor", default="hcaptcha", help="verify_challenge 所需 captcha_vendor_name")
    parser.add_argument("--browser-locale", default=DEFAULT_LOCALE, help="挑战浏览器 locale，如 en-US")
    parser.add_argument("--browser-timezone", default=DEFAULT_TIMEZONE, help="挑战浏览器 timezone，如 America/Chicago")
    parser.add_argument("--accept-language", default="", help="挑战浏览器 Accept-Language 头")
    args = parser.parse_args()

    result = solve_bridge(
        args.bridge_url,
        timeout_s=args.timeout,
        out_dir=Path(args.out_dir),
        headless=not args.headed,
        proxy_url=args.proxy_url,
        vlm_cfg={
            "enabled": not args.no_vlm,
            "base_url": args.vlm_base_url,
            "api_key": args.vlm_api_key,
            "model": args.vlm_model,
            "timeout_s": args.vlm_timeout,
        },
        verify_url=args.verify_url,
        verify_form_base={
            "client_secret": args.verify_client_secret,
            "captcha_vendor_name": args.verify_captcha_vendor,
            "key": args.verify_key,
            "_stripe_version": args.verify_stripe_version,
        },
        browser_locale=args.browser_locale,
        browser_timezone=args.browser_timezone,
        accept_language=args.accept_language or f"{args.browser_locale},{args.browser_locale.split('-', 1)[0]};q=0.9",
    )
    if result:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
