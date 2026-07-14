import asyncio
import csv
import html as html_lib
import io
import json
import math
import os
import random
import re
import sqlite3
import string
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from google import genai  # type: ignore
    from google.genai import types as genai_types  # type: ignore
except Exception:
    genai = None
    genai_types = None

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = APP_DIR / "data"
STATIC_DIR = APP_DIR / "static"
DB_PATH = APP_DIR / "sonp.sqlite3"
TASKS_PATH = DATA_DIR / "tasks.json"
REFERENCE_TRACK_PROFILES_PATH = DATA_DIR / "reference_track_profiles.json"

def _load_simple_env(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    try:
        if not path.exists():
            return data
        for raw_line in path.read_text("utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                data[key] = value
    except Exception:
        return {}
    return data


_DOTENV = _load_simple_env(APP_DIR / ".env")
AI_PROMPT_CONFIG_PATH = APP_DIR / "ai_prompt_config.json"
GEMINI_API_KEY_INLINE = "Your own *secret* Gemini/OpenAI/etc API key"
GEMINI_MODEL_INLINE = ""
GEMINI_API_KEY = (GEMINI_API_KEY_INLINE or os.getenv("GEMINI_API_KEY") or _DOTENV.get("GEMINI_API_KEY") or "").strip()
GEMINI_MODEL = (GEMINI_MODEL_INLINE or os.getenv("GEMINI_MODEL") or _DOTENV.get("GEMINI_MODEL") or "gemini-2.5-flash").strip() or "gemini-2.5-flash"

_DEFAULT_AI_PROMPT_CONFIG = {
    "version": "emopot_hr_v4_scientific_gemini",
    "temperature": 0.15,
    "max_output_tokens": 2600,
    "system_prompt": "Ты выступаешь в роли экспертной системы интерпретации результатов платформы EMOPOT — игровой мультимодальной диагностики эмоционально-мотивационного и когнитивно-поведенческого потенциала кандидатов на стажировки. Твоя задача — не выносить категоричный приговор, а формировать осторожное, аргументированное и проверяемое HR-заключение по данным игрока. Оценивай кандидата только на основе переданных данных. Не придумывай факты. Не делай медицинских, психиатрических, клинических или иных диагностических выводов. Не используй стигматизирующие формулировки. Не интерпретируй единичный признак как доказательство устойчивого личностного качества. Частично правильные ответы трактуй как значимый признак частичного понимания, а не как полный провал. Обязательно различай общую профессиональную пригодность, пригодность именно к IT, альтернативные треки и предварительное соответствие конкретным профессиям/ролям. Если в профиле уже есть rule-based рекомендация роли, используй её как опорный сигнал и либо подтверждай, либо осторожно уточняй, но не игнорируй без явных оснований в данных. Каждый вывод должен быть вероятностным, осторожным и сопровождаться ссылкой на наблюдаемые признаки и ограничениями интерпретации. Возвращай только JSON по заданной схеме.",
    "response_json_schema": {
        "type": "object",
        "properties": {
            "candidate_summary": {"type": "string"},
            "overall_assessment": {
                "type": "object",
                "properties": {
                    "general_fit_level": {"type": "string", "enum": ["high", "medium", "low", "unclear"]},
                    "it_fit_level": {"type": "string", "enum": ["high", "medium", "low", "unclear"]},
                    "confidence": {"type": "number"},
                    "follow_up_needed": {"type": "boolean"},
                    "follow_up_reason": {"type": "string"}
                },
                "required": ["general_fit_level", "it_fit_level", "confidence", "follow_up_needed", "follow_up_reason"]
            },
            "strengths": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "evidence": {"type": "string"},
                        "interpretation": {"type": "string"}
                    },
                    "required": ["name", "evidence", "interpretation"]
                }
            },
            "risk_flags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "evidence": {"type": "string"},
                        "alternative_explanation": {"type": "string"}
                    },
                    "required": ["name", "severity", "evidence", "alternative_explanation"]
                }
            },
            "behavioral_interpretation": {
                "type": "object",
                "properties": {
                    "decision_style": {"type": "object", "properties": {"value": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["value", "evidence"]},
                    "attention_stability": {"type": "object", "properties": {"value": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["value", "evidence"]},
                    "response_consistency": {"type": "object", "properties": {"value": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["value", "evidence"]},
                    "error_handling_style": {"type": "object", "properties": {"value": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["value", "evidence"]}
                },
                "required": ["decision_style", "attention_stability", "response_consistency", "error_handling_style"]
            },
            "track_recommendation": {
                "type": "object",
                "properties": {
                    "primary_track": {"type": "string"},
                    "secondary_tracks": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"}
                },
                "required": ["primary_track", "secondary_tracks", "why"]
            },
            "profession_recommendation": {
                "type": "object",
                "properties": {
                    "primary_role": {"type": "string"},
                    "role_cluster": {"type": "string"},
                    "secondary_roles": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                    "caution": {"type": "string"}
                },
                "required": ["primary_role", "role_cluster", "secondary_roles", "why", "caution"]
            },
            "it_specific_comment": {
                "type": "object",
                "properties": {
                    "recommended_for_it_internship": {"type": "boolean"},
                    "why": {"type": "string"},
                    "caution": {"type": "string"}
                },
                "required": ["recommended_for_it_internship", "why", "caution"]
            },
            "interview_focus": {"type": "array", "items": {"type": "string"}},
            "limitations": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["candidate_summary", "overall_assessment", "strengths", "risk_flags", "behavioral_interpretation", "track_recommendation", "profession_recommendation", "it_specific_comment", "interview_focus", "limitations"]
    }
}


def _load_ai_prompt_config() -> dict:
    config = dict(_DEFAULT_AI_PROMPT_CONFIG)
    try:
        if AI_PROMPT_CONFIG_PATH.exists():
            loaded = json.loads(AI_PROMPT_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for k, v in loaded.items():
                    if v is not None:
                        config[k] = v
    except Exception:
        traceback.print_exc()
    if not isinstance(config.get("response_json_schema"), dict):
        config["response_json_schema"] = _DEFAULT_AI_PROMPT_CONFIG["response_json_schema"]
    return config

AI_PROMPT_CONFIG = _load_ai_prompt_config()
AI_REPORT_PROMPT_VERSION = str(AI_PROMPT_CONFIG.get("version") or _DEFAULT_AI_PROMPT_CONFIG["version"])
AI_SYSTEM_PROMPT = str(AI_PROMPT_CONFIG.get("system_prompt") or _DEFAULT_AI_PROMPT_CONFIG["system_prompt"])
AI_TEMPERATURE = max(0.0, min(1.0, float(AI_PROMPT_CONFIG.get("temperature", _DEFAULT_AI_PROMPT_CONFIG["temperature"]))))
AI_MAX_OUTPUT_TOKENS = max(512, int(AI_PROMPT_CONFIG.get("max_output_tokens", _DEFAULT_AI_PROMPT_CONFIG["max_output_tokens"])))
AI_RESPONSE_JSON_SCHEMA = AI_PROMPT_CONFIG.get("response_json_schema") or _DEFAULT_AI_PROMPT_CONFIG["response_json_schema"]

ALL_BLOCK_KEYS = [
    "cognitive_cards",
    "emp_motivation",
    "emp_communication",
    "emp_self_regulation",
    "emp_career_orientation",
    "emp_values",
    "emp_control",
    "legacy_math",
    "legacy_logic",
    "legacy_data",
    "legacy_misc",
]

LEGACY_CATEGORY_TO_BLOCK = {
    "Математика": "legacy_math",
    "Логика": "legacy_logic",
    "Анализ данных": "legacy_data",
    "Когнитивные карточки": "cognitive_cards",
}

ASSESSMENT_PRESETS = {
    "emp_core_plus_cards": {
        "cognitive_cards": True,
        "emp_motivation": True,
        "emp_communication": True,
        "emp_self_regulation": True,
        "emp_career_orientation": False,
        "emp_values": False,
        "emp_control": True,
        "legacy_math": False,
        "legacy_logic": False,
        "legacy_data": False,
        "legacy_misc": False,
    },
    "cards_only": {
        "cognitive_cards": True,
        "emp_motivation": False,
        "emp_communication": False,
        "emp_self_regulation": False,
        "emp_career_orientation": False,
        "emp_values": False,
        "emp_control": False,
        "legacy_math": False,
        "legacy_logic": False,
        "legacy_data": False,
        "legacy_misc": False,
    },
    "emp_full": {
        "cognitive_cards": True,
        "emp_motivation": True,
        "emp_communication": True,
        "emp_self_regulation": True,
        "emp_career_orientation": True,
        "emp_values": True,
        "emp_control": True,
        "legacy_math": False,
        "legacy_logic": False,
        "legacy_data": False,
        "legacy_misc": False,
    },
    "emp_full_all_cards_controls": {
        "cognitive_cards": True,
        "emp_motivation": True,
        "emp_communication": True,
        "emp_self_regulation": True,
        "emp_career_orientation": True,
        "emp_values": True,
        "emp_control": True,
        "legacy_math": False,
        "legacy_logic": False,
        "legacy_data": False,
        "legacy_misc": False,
    },
    "legacy": {
        "cognitive_cards": True,
        "emp_motivation": False,
        "emp_communication": False,
        "emp_self_regulation": False,
        "emp_career_orientation": False,
        "emp_values": False,
        "emp_control": False,
        "legacy_math": True,
        "legacy_logic": True,
        "legacy_data": True,
        "legacy_misc": True,
    },
    "custom": {key: False for key in ALL_BLOCK_KEYS},
}

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)


# ====================== HELPERS ======================
def normalize_block_name(value: Optional[str]) -> str:
    s = str(value or "").strip()
    return s if s in ALL_BLOCK_KEYS else "legacy_misc"


def default_block_for(category: str, mode: str = "base") -> str:
    if mode == "card":
        return "cognitive_cards"
    if "контроль" in category.lower():
        return "emp_control"
    return LEGACY_CATEGORY_TO_BLOCK.get(category, "legacy_misc")


def sanitize_block_config(raw: Optional[dict], *, fallback_mode: str = "legacy") -> Dict[str, bool]:
    base = dict(ASSESSMENT_PRESETS.get(fallback_mode, ASSESSMENT_PRESETS["legacy"]))
    if isinstance(raw, dict):
        for key in ALL_BLOCK_KEYS:
            if key in raw:
                base[key] = bool(raw.get(key))
    return base


def _score_value(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def sort_players_by_score(rows: List[dict]) -> List[dict]:
    return sorted(
        list(rows or []),
        key=lambda x: (-_score_value(x.get("score", 0)), str(x.get("name", "")).casefold(), str(x.get("playerId", ""))),
    )



def resolve_assessment_mode(mode: Optional[str], legacy_filter_mode: Optional[str] = None) -> str:
    mode = str(mode or "").strip()
    if mode in ASSESSMENT_PRESETS:
        return mode
    legacy_filter_mode = str(legacy_filter_mode or "").strip()
    if legacy_filter_mode == "cards_only":
        return "cards_only"
    if legacy_filter_mode in ("all", "no_cards"):
        return "legacy"
    return "emp_full"


def infer_response_model(q: dict) -> str:
    raw = str(q.get("responseModel") or q.get("scoreMode") or "").strip().lower()
    if raw == "survey":
        return "survey"
    if str(q.get("mode", "base")) == "survey":
        return "survey"
    block = str(q.get("block", "")).strip()
    if block.startswith("emp_"):
        return "survey"
    return "quiz"


def task_block(q: dict) -> str:
    return normalize_block_name(q.get("block") or default_block_for(q.get("category", ""), q.get("mode", "base")))


def task_instrument(q: dict) -> str:
    return str(q.get("instrument") or q.get("subtype") or q.get("category") or "general")


def task_item_type(q: dict) -> str:
    if q.get("mode") == "card":
        return "cognitive_card"
    if infer_response_model(q) == "survey":
        return str(q.get("itemType") or "survey_item")
    return str(q.get("itemType") or "quiz_item")


def is_assessment_task(q: dict) -> bool:
    return infer_response_model(q) == "survey"


def is_card_task(q: dict) -> bool:
    return q.get("mode") == "card"


def is_control_task(q: dict) -> bool:
    block = task_block(q)
    if block == "emp_control":
        return True
    control_type = str(q.get("controlType") or "").strip().lower()
    if control_type and control_type not in {"none", "no", "false", "null", "regular"}:
        return True
    category = str(q.get("category") or "").lower()
    instrument = str(q.get("instrument") or "").lower()
    prompt = str(q.get("prompt") or q.get("title") or "").lower()
    markers = ["контроль", "внимательн", "social_desirability", "attention_check", "consistency_check"]
    text = " ".join([category, instrument, prompt])
    return any(marker in text for marker in markers)


def is_emp_content_task(q: dict) -> bool:
    return is_assessment_task(q) and not is_control_task(q)


def should_use_full_session_plan(mode: str) -> bool:
    return mode in {"cards_only", "emp_core_plus_cards", "emp_full", "emp_full_all_cards_controls"}


def compute_scored_value(q: dict, ans_text: str, ans_choice: Optional[int]) -> Optional[float]:
    if not is_assessment_task(q):
        return None
    if q.get("type") == "mcq":
        if ans_choice is None:
            return None
        option_scores = q.get("optionScores") or []
        if isinstance(option_scores, list) and 0 <= ans_choice < len(option_scores):
            try:
                value = float(option_scores[ans_choice])
                if q.get("reverse") and option_scores:
                    numeric = []
                    for item in option_scores:
                        try:
                            numeric.append(float(item))
                        except Exception:
                            pass
                    if numeric:
                        value = min(numeric) + max(numeric) - value
                return value
            except Exception:
                return None
        n = len(q.get("options") or [])
        if n <= 0:
            n = 5
        value = float(ans_choice + 1)
        if q.get("reverse"):
            value = float(n + 1) - value
        return value
    raw = (ans_text or "").strip().replace(",", ".")
    try:
        return float(raw)
    except Exception:
        return None


def _clone_shuffle(items: List[dict]) -> List[dict]:
    copied = list(items)
    random.shuffle(copied)
    return copied


def _round_robin_by_key(items: List[dict], key_fn) -> List[dict]:
    if not items:
        return []
    groups: Dict[str, List[dict]] = {}
    for item in items:
        groups.setdefault(str(key_fn(item)), []).append(item)
    for bucket in groups.values():
        random.shuffle(bucket)
    order = list(groups.keys())
    random.shuffle(order)
    result: List[dict] = []
    while order:
        next_order: List[str] = []
        for group_key in order:
            bucket = groups[group_key]
            if bucket:
                result.append(bucket.pop())
            if bucket:
                next_order.append(group_key)
        random.shuffle(next_order)
        order = next_order
    return result


def _interleave_even(primary: List[dict], inserts: List[dict]) -> List[dict]:
    if not primary:
        return list(inserts)
    if not inserts:
        return list(primary)

    gap_count = len(inserts) + 1
    base = len(primary) // gap_count
    extra = len(primary) % gap_count

    result: List[dict] = []
    idx = 0
    for gap in range(gap_count):
        take = base + (1 if gap < extra else 0)
        if take > 0:
            result.extend(primary[idx : idx + take])
            idx += take
        if gap < len(inserts):
            result.append(inserts[gap])
    return result


def _insert_evenly(base_items: List[dict], inserts: List[dict], *, avoid_edges: bool = True) -> List[dict]:
    if not inserts:
        return list(base_items)
    if not base_items:
        return list(inserts)

    result = list(base_items)
    original_len = len(base_items)
    offset = 0
    for idx, item in enumerate(inserts):
        raw_pos = round((idx + 1) * (original_len + 1) / (len(inserts) + 1))
        if avoid_edges:
            raw_pos = max(1, min(original_len - 1 if original_len > 1 else 1, raw_pos))
        insert_pos = max(0, min(len(result), raw_pos + offset))
        result.insert(insert_pos, item)
        offset += 1
    return result


def _collect_enabled_tasks(block_config: Dict[str, bool]) -> List[dict]:
    seen: set = set()
    items: List[dict] = []
    for q in _all_tasks():
        if q["id"] in seen:
            continue
        if not _allowed_by_block_config(q, block_config):
            continue
        seen.add(q["id"])
        items.append(q)
    return items


def build_session_plan(room: "Room") -> List[dict]:
    block_config = room.block_config or sanitize_block_config(None, fallback_mode=room.assessment_mode)
    enabled_tasks = _collect_enabled_tasks(block_config)

    cards = [q for q in enabled_tasks if is_card_task(q)]
    controls = [q for q in enabled_tasks if is_control_task(q)]
    emp_items = [q for q in enabled_tasks if is_emp_content_task(q)]
    others = [q for q in enabled_tasks if q not in cards and q not in controls and q not in emp_items]

    cards = _clone_shuffle(cards)
    controls = _clone_shuffle(controls)
    emp_items = _round_robin_by_key(emp_items, lambda q: task_block(q) or task_instrument(q))
    others = _round_robin_by_key(others, lambda q: q.get("category") or task_block(q))

    if room.assessment_mode == "cards_only":
        plan = cards
    else:
        primary: List[dict] = []
        if emp_items:
            primary.extend(emp_items)
        if others:
            if primary:
                primary = _insert_evenly(primary, others, avoid_edges=False)
            else:
                primary.extend(others)

        if primary and cards:
            plan = _interleave_even(primary, cards)
        elif primary:
            plan = primary
        elif cards:
            plan = cards
        else:
            plan = []

        if controls:
            plan = _insert_evenly(plan, controls, avoid_edges=True)

    if not plan:
        plan = _clone_shuffle(enabled_tasks)

    return plan


def limit_session_plan(plan: List[dict], requested_rounds: int) -> List[dict]:
    if not plan:
        return []
    requested_rounds = _safe_int(requested_rounds, 0) if "_safe_int" in globals() else int(requested_rounds or 0)
    if requested_rounds <= 0:
        return list(plan)
    return list(plan[: min(len(plan), requested_rounds)])


def normalize_expected_choice(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_expected_choice(v) for v in value]
    if isinstance(value, dict):
        return {str(k): normalize_expected_choice(v) for k, v in value.items()}
    return value


def evaluate_expected_choice(expected_choice: Any, actual_choice: Optional[int]) -> Optional[bool]:
    if expected_choice is None:
        return None
    if actual_choice is None:
        return False

    expected_choice = normalize_expected_choice(expected_choice)

    if isinstance(expected_choice, list):
        normalized: List[int] = []
        for item in expected_choice:
            try:
                normalized.append(int(item))
            except Exception:
                continue
        return actual_choice in normalized

    if isinstance(expected_choice, dict):
        if "anyOf" in expected_choice:
            return evaluate_expected_choice(expected_choice.get("anyOf"), actual_choice)
        if "allowed" in expected_choice:
            return evaluate_expected_choice(expected_choice.get("allowed"), actual_choice)
        try:
            low = int(expected_choice.get("min")) if expected_choice.get("min") is not None else None
            high = int(expected_choice.get("max")) if expected_choice.get("max") is not None else None
        except Exception:
            low = None
            high = None
        if low is not None and actual_choice < low:
            return False
        if high is not None and actual_choice > high:
            return False
        if low is not None or high is not None:
            return True
        return None

    try:
        return actual_choice == int(expected_choice)
    except Exception:
        return None


def compact_question_snapshot(q: dict) -> dict:
    snapshot = {
        "id": q.get("id"),
        "category": q.get("category"),
        "block": task_block(q),
        "instrument": task_instrument(q),
        "itemType": task_item_type(q),
        "responseModel": infer_response_model(q),
        "type": q.get("type"),
        "prompt": q.get("prompt"),
        "mode": q.get("mode", "base"),
        "subtype": q.get("subtype"),
        "difficulty": q.get("difficulty"),
        "timeRef": q.get("timeRef"),
        "tags": q.get("tags"),
        "controlType": q.get("controlType"),
        "expectedChoice": q.get("expectedChoice"),
        "scaleKey": q.get("scaleKey"),
        "reverse": q.get("reverse", False),
        "cardAxisWeights": q.get("cardAxisWeights"),
        "cardImage": q.get("cardImage"),
    }
    if q.get("type") == "mcq":
        snapshot["options"] = q.get("options", [])
        snapshot["correctIndex"] = q.get("correctIndex")
    else:
        snapshot["accept"] = q.get("accept", [])
    return snapshot

def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _empty_answer(text: str, choice: Optional[int]) -> bool:
    return choice is None and not bool((text or "").strip())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_interaction_state() -> dict:
    return {
        "first_response_ms": None,
        "last_change_ms": None,
        "change_count": 0,
        "choice_change_count": 0,
        "text_change_count": 0,
        "focus_count": 0,
        "manual_skip": False,
        "submit_clicked": False,
        "event_count": 0,
        "events": [],
    }


def _interaction_summary_from_state(state: Optional[dict], fallback_time_ms: int = 0) -> dict:
    state = state or init_interaction_state()
    first_response_ms = state.get("first_response_ms")
    last_change_ms = state.get("last_change_ms")
    if first_response_ms is None and fallback_time_ms:
        first_response_ms = fallback_time_ms
    if last_change_ms is None and fallback_time_ms:
        last_change_ms = fallback_time_ms
    change_count = _safe_int(state.get("change_count"), 0)
    hesitation_raw = min(1.0, (change_count * 0.18) + (_safe_int(last_change_ms or 0) / 1000.0) * 0.01)
    hesitation_index = round(hesitation_raw * 100.0, 1)
    compact_events = []
    for item in (state.get("events") or [])[-12:]:
        compact_events.append({
            "t": item.get("type"),
            "ms": item.get("event_ms"),
        })
    return {
        "firstResponseMs": first_response_ms,
        "lastChangeMs": last_change_ms,
        "changeCount": change_count,
        "choiceChangeCount": _safe_int(state.get("choice_change_count"), 0),
        "textChangeCount": _safe_int(state.get("text_change_count"), 0),
        "focusCount": _safe_int(state.get("focus_count"), 0),
        "manualSkip": bool(state.get("manual_skip")),
        "submitClicked": bool(state.get("submit_clicked")),
        "eventCount": _safe_int(state.get("event_count"), 0),
        "hesitationIndex": hesitation_index,
        "events": compact_events,
    }


def _record_interaction_event(state: dict, event_type: str, event_ms: Optional[int], payload: Optional[dict] = None):
    if state is None:
        return
    payload = payload or {}
    state["event_count"] = _safe_int(state.get("event_count"), 0) + 1
    if event_ms is not None:
        event_ms = max(0, _safe_int(event_ms, 0))
    state.setdefault("events", []).append({"type": event_type, "event_ms": event_ms, "payload": payload})
    if len(state["events"]) > 50:
        state["events"] = state["events"][-50:]
    if event_type in {"choice_selected", "choice_changed", "text_changed", "text_input_started", "answer_focus", "submit_clicked", "manual_skip"} and event_ms is not None:
        if state.get("first_response_ms") is None:
            state["first_response_ms"] = event_ms
        state["last_change_ms"] = event_ms
    if event_type == "answer_focus":
        state["focus_count"] = _safe_int(state.get("focus_count"), 0) + 1
    elif event_type in {"choice_selected", "choice_changed", "choice_unselected", "selection_changed"}:
        state["change_count"] = _safe_int(state.get("change_count"), 0) + 1
        state["choice_change_count"] = _safe_int(state.get("choice_change_count"), 0) + 1
    elif event_type in {"text_changed", "text_input_started", "text_cleared"}:
        state["change_count"] = _safe_int(state.get("change_count"), 0) + 1
        state["text_change_count"] = _safe_int(state.get("text_change_count"), 0) + 1
    elif event_type == "submit_clicked":
        state["submit_clicked"] = True
    elif event_type == "manual_skip":
        state["manual_skip"] = True


def _prefix_valid_ladder_words(words: List[str]) -> int:
    if not words:
        return 0
    valid = 0
    seen = set()
    prev = None
    for i, word in enumerate(words):
        if i == 0:
            if word != "ЛИСА":
                break
            valid = 1
            seen.add(word)
            prev = word
            continue
        if word in seen:
            break
        if word not in WORD_LADDER_LISA_NORA_WORDS:
            break
        if not _letters_diff_one(prev, word):
            break
        seen.add(word)
        prev = word
        valid += 1
    return valid


def _partial_credit_word_ladder(ans_text: str) -> Tuple[float, str]:
    words = _extract_russian_words_4(ans_text)
    if not words:
        return 0.0, "unfinished_solution"
    if words[0] != "ЛИСА":
        return 0.0, "rule_misunderstood"
    prefix = _prefix_valid_ladder_words(words)
    if words and words[-1] == "НОРА" and prefix >= max(1, len(words) - 1):
        return 0.75, "one_step_miss"
    if prefix >= 5:
        return 0.5, "partial_correct_chain"
    if prefix >= 2:
        return 0.25, "partial_correct_chain"
    return 0.0, "rule_misunderstood"


def _partial_credit_robot_pair(ans_text: str) -> Tuple[float, str]:
    nums = re.findall(r"-?\d+", ans_text or "")
    if len(nums) < 2:
        return 0.0, "unfinished_solution"
    try:
        a = int(nums[0])
        b = int(nums[1])
    except Exception:
        return 0.0, "execution_error"
    if a <= 0 or b <= 0:
        return 0.0, "rule_misunderstood"
    value = a * b - 5
    if abs(value - 72) <= 6:
        return 0.5, "near_miss"
    if abs(value - 72) <= 12:
        return 0.25, "partial_correct"
    return 0.0, "rule_misunderstood"


def _partial_credit_mcq(q: dict, choice: Optional[int]) -> float:
    if choice is None:
        return 0.0
    partial = q.get("partialScoring")
    if isinstance(partial, dict):
        value = partial.get(str(choice))
        if value is None:
            value = partial.get(choice)
        if value is not None:
            return _clamp(_safe_float(value, 0.0), 0.0, 1.0)
    return 0.0


def _parse_int_set(text: str) -> List[int]:
    values: List[int] = []
    seen = set()
    for token in re.findall(r"\d+", str(text or "")):
        try:
            num = int(token)
        except ValueError:
            continue
        if num not in seen:
            seen.add(num)
            values.append(num)
    return values


def _normalize_choice_letters(text: str) -> List[str]:
    src = str(text or "").upper()
    mapping = {"A": "А", "B": "В", "C": "С", "D": "Д", "E": "Е"}
    result: List[str] = []
    seen = set()
    for ch in src:
        ch = mapping.get(ch, ch)
        if ch in {"А", "Б", "В", "Г", "Д", "Е"} and ch not in seen:
            seen.add(ch)
            result.append(ch)
    return result


def _extract_correct_planet_set(q: dict) -> List[int]:
    accepts = q.get("accept") or []
    if isinstance(accepts, list) and accepts:
        vals = _parse_int_set(str(accepts[0]))
        if vals:
            return sorted(vals)
    return []


def _partial_credit_planets_select(ans_text: str, q: dict) -> Tuple[float, str]:
    selected = set(_parse_int_set(ans_text))
    correct = set(_extract_correct_planet_set(q))
    if not selected or not correct:
        return 0.0, "rule_misunderstood"
    common = len(selected & correct)
    false_pos = len(selected - correct)
    missed = len(correct - selected)
    partial = q.get("partialScoring") if isinstance(q.get("partialScoring"), dict) else {}
    near_full = _clamp(_safe_float(partial.get("near_full", 0.75), 0.75), 0.0, 1.0)
    partial_match = _clamp(_safe_float(partial.get("partial_match", 0.5), 0.5), 0.0, 1.0)
    if common == len(correct) - 1 and false_pos == 0 and missed == 1:
        return round(near_full, 2), "near_miss"
    if common >= 1 and false_pos <= 1:
        return round(partial_match, 2), "partial_correct"
    return 0.0, "rule_misunderstood"


def _extract_correct_letter_set(q: dict) -> List[str]:
    accepts = q.get("accept") or []
    if isinstance(accepts, list) and accepts:
        vals = _normalize_choice_letters(str(accepts[0]))
        if vals:
            return sorted(vals)
    return []


def _partial_credit_cube_nets_select(ans_text: str, q: dict) -> Tuple[float, str]:
    selected = set(_normalize_choice_letters(ans_text))
    correct = set(_extract_correct_letter_set(q))
    if not selected or not correct:
        return 0.0, "rule_misunderstood"
    common = len(selected & correct)
    false_pos = len(selected - correct)
    partial = q.get("partialScoring") if isinstance(q.get("partialScoring"), dict) else {}
    if common == max(0, len(correct) - 1) and false_pos == 0:
        return round(_clamp(_safe_float(partial.get("two_of_three", 0.75), 0.75), 0.0, 1.0), 2), "near_miss"
    if common == max(0, len(correct) - 1) and false_pos == 1:
        return round(_clamp(_safe_float(partial.get("one_false_positive_with_two_true", 0.5), 0.5), 0.0, 1.0), 2), "partial_correct"
    if common == 1 and false_pos == 0:
        return round(_clamp(_safe_float(partial.get("one_of_three", 0.25), 0.25), 0.0, 1.0), 2), "partial_attempt"
    return 0.0, "rule_misunderstood"


def _partial_credit_route_min_steps(ans_text: str, q: dict) -> Tuple[float, str]:
    """Даёт частичный балл за близкий расчёт минимального маршрута по сетке."""
    nums = re.findall(r"-?\d+", str(ans_text or ""))
    if not nums:
        return 0.0, "unfinished_solution"
    try:
        answer = int(nums[0])
    except Exception:
        return 0.0, "rule_misunderstood"

    accepted = q.get("accept") or []
    try:
        target = int(float(str(accepted[0]).replace(",", ".")))
    except Exception:
        return 0.0, "rule_misunderstood"

    diff = abs(answer - target)
    partial_cfg = q.get("partialScoring") if isinstance(q.get("partialScoring"), dict) else {}

    def _pc(key: str, default: float) -> float:
        return round(_clamp(_safe_float(partial_cfg.get(key, default), default), 0.0, 1.0), 2)

    if diff == 1:
        return _pc("plus_one_step", 0.75), "near_miss"
    if 2 <= diff <= 3:
        return _pc("plus_two_or_three_steps", 0.5), "partial_correct"
    if 4 <= diff <= 6:
        return _pc("clearly_suboptimal", 0.25), "suboptimal_route"
    return 0.0, "planning_gap"


def evaluate_player_response(q: dict, ans_text: str, ans_choice: Optional[int], interaction_summary: Optional[dict], *, answered: bool, timed_out: bool = False, disconnected: bool = False) -> dict:
    interaction_summary = interaction_summary or {}
    manual_skip = bool(interaction_summary.get("manualSkip"))
    if not answered or _empty_answer(ans_text, ans_choice):
        skip_reason = "disconnect" if disconnected else ("manual_skip" if manual_skip else ("timeout" if timed_out else "unknown"))
        return {
            "is_correct": None if is_assessment_task(q) and not is_control_task(q) else False,
            "partial_credit": 0.0,
            "response_quality": "intentional_skip" if manual_skip else ("timeout_incomplete" if timed_out else "no_answer"),
            "error_pattern": "unfinished_solution",
            "skip_reason": skip_reason,
            "final_answer_state": "skipped" if manual_skip else ("timed_out" if timed_out else "disconnect_before_submit" if disconnected else "auto_closed"),
            "awarded": 0,
            "scored_value": compute_scored_value(q, ans_text, ans_choice),
            "status_text": "пропуск" if manual_skip else ("время вышло" if timed_out else "нет ответа"),
        }

    if is_assessment_task(q):
        scored_value = compute_scored_value(q, ans_text, ans_choice)
        if is_control_task(q):
            expected_choice = q.get("expectedChoice")
            if expected_choice is None and q.get("correctIndex") is not None:
                expected_choice = q.get("correctIndex")
            ok = evaluate_expected_choice(expected_choice, ans_choice) if q.get("type") == "mcq" else bool((ans_text or "").strip())
            return {
                "is_correct": ok,
                "partial_credit": 1.0 if ok else 0.0,
                "response_quality": "full_correct" if ok else "control_failed",
                "error_pattern": "control_pass" if ok else "control_failed",
                "skip_reason": "",
                "final_answer_state": "submitted",
                "awarded": 0,
                "scored_value": scored_value,
                "status_text": "пройден" if ok else ("провален" if ok is False else "получен"),
            }
        return {
            "is_correct": None,
            "partial_credit": 1.0 if scored_value is not None else 0.0,
            "response_quality": "survey_answered",
            "error_pattern": "",
            "skip_reason": "",
            "final_answer_state": "submitted",
            "awarded": 0,
            "scored_value": scored_value,
            "status_text": "получен",
        }

    ok = False
    partial_credit = 0.0
    error_pattern = "rule_misunderstood"

    mode = q.get("mode", "base")
    subtype = q.get("subtype")
    status_text_override: Optional[str] = None
    if mode == "card" and subtype == "robot_pair_to_target":
        ok = _check_robot_pair_to_target(ans_text)
        if not ok:
            partial_credit, error_pattern = _partial_credit_robot_pair(ans_text)
    elif mode == "card" and subtype == "word_ladder_lisa_nora":
        ok = _check_word_ladder_lisa_nora(ans_text)
        if not ok:
            partial_credit, error_pattern = _partial_credit_word_ladder(ans_text)
    elif mode == "card" and subtype == "clock_equal_sums":
        ok, partial_credit, error_pattern, status_text_override = _evaluate_clock_equal_sums_partial(ans_text, q)
    elif mode == "card" and subtype == "route_min_steps":
        ok = _is_correct_text(ans_text, q.get("accept", []))
        if not ok:
            partial_credit, error_pattern = _partial_credit_route_min_steps(ans_text, q)
    elif mode == "card" and subtype == "planets_select":
        selected = set(_parse_int_set(ans_text))
        correct = set(_extract_correct_planet_set(q))
        ok = bool(correct) and selected == correct
        if not ok:
            partial_credit, error_pattern = _partial_credit_planets_select(ans_text, q)
    elif mode == "card" and subtype == "cube_nets_select":
        selected = set(_normalize_choice_letters(ans_text))
        correct = set(_extract_correct_letter_set(q))
        ok = bool(correct) and selected == correct
        if not ok:
            partial_credit, error_pattern = _partial_credit_cube_nets_select(ans_text, q)
    else:
        if q.get("type") == "mcq":
            ok = (ans_choice is not None) and (int(ans_choice) == int(q.get("correctIndex", -1)))
            if not ok:
                partial_credit = _partial_credit_mcq(q, ans_choice)
                error_pattern = "near_miss" if partial_credit > 0 else "confident_wrong"
        else:
            ok = _is_correct_text(ans_text, q.get("accept", []))
            if not ok and (ans_text or "").strip():
                accepted = q.get("accept", [])
                if accepted and _normalize_answer(ans_text) in _normalize_answer(str(accepted[0])):
                    partial_credit = 0.25
                    error_pattern = "partial_correct"

    partial_credit = round(_clamp(partial_credit, 0.0, 1.0), 2)
    awarded = 1.0 if ok else partial_credit
    change_count = _safe_int(interaction_summary.get("changeCount"), 0)
    hesitation = _safe_float(interaction_summary.get("hesitationIndex"), 0.0)
    if ok:
        response_quality = "full_correct" if change_count <= 1 else "hesitant_correct"
        status_text = status_text_override or "верно"
    elif partial_credit >= 0.75:
        response_quality = "near_miss"
        status_text = status_text_override or "почти верно"
    elif partial_credit >= 0.5:
        response_quality = "partial_correct"
        status_text = status_text_override or "частично верно"
    elif partial_credit > 0:
        response_quality = "partial_attempt"
        status_text = status_text_override or "слабое частичное решение"
    else:
        if hesitation >= 55:
            response_quality = "hesitant_wrong"
        elif change_count == 0 and _safe_int(interaction_summary.get("firstResponseMs"), 0) <= 1200:
            response_quality = "random_guess"
        else:
            response_quality = "confident_wrong"
        status_text = "неверно"

    return {
        "is_correct": ok,
        "partial_credit": partial_credit,
        "response_quality": response_quality,
        "error_pattern": error_pattern if not ok else "",
        "skip_reason": "",
        "final_answer_state": "submitted",
        "awarded": awarded,
        "scored_value": None,
        "status_text": status_text,
    }



# ====================== DB ======================
def _has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info('{table}')")
    cols = {row[1] for row in cur.fetchall()}
    return column in cols


def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS rooms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        created_at INTEGER,
        rounds INTEGER,
        status TEXT,
        assessment_mode TEXT,
        block_config TEXT,
        wait_for_all_players INTEGER DEFAULT 1
    )"""
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS players(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT,
        player_id TEXT,
        name TEXT,
        score REAL DEFAULT 0
    )"""
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS answers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT,
        round_no INTEGER,
        question_id TEXT,
        category TEXT,
        block TEXT,
        instrument TEXT,
        item_type TEXT,
        player_id TEXT,
        player_name TEXT,
        answer_text TEXT,
        is_correct INTEGER,
        awarded INTEGER,
        scored_value REAL,
        time_spent_ms INTEGER,
        answer_choice INTEGER,
        partial_credit REAL DEFAULT 0,
        response_quality TEXT DEFAULT '',
        error_pattern TEXT DEFAULT '',
        skip_reason TEXT DEFAULT '',
        final_answer_state TEXT DEFAULT '',
        first_response_ms INTEGER,
        last_change_ms INTEGER,
        change_count INTEGER DEFAULT 0,
        hesitation_index REAL DEFAULT 0,
        interaction_summary_json TEXT DEFAULT ''
    )"""
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS player_reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT,
        player_id TEXT,
        player_name TEXT,
        overall_potential REAL,
        summary_text TEXT,
        fit_tags_json TEXT,
        strengths_json TEXT,
        growth_zones_json TEXT,
        recommendations_json TEXT,
        emp_radar_json TEXT,
        cognitive_radar_json TEXT,
        quality_json TEXT,
        game_metrics_json TEXT,
        profile_json TEXT,
        created_at INTEGER
    )"""
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS room_reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT UNIQUE,
        players_count INTEGER,
        avg_score REAL,
        avg_accuracy REAL,
        avg_potential REAL,
        top_tags_json TEXT,
        emp_radar_json TEXT,
        cognitive_radar_json TEXT,
        summary_json TEXT,
        dashboard_json TEXT,
        created_at INTEGER
    )"""
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS event_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT NOT NULL,
        player_id TEXT NOT NULL,
        round_no INTEGER,
        question_id TEXT DEFAULT '',
        event_type TEXT NOT NULL,
        event_ts TEXT NOT NULL,
        event_ms INTEGER,
        payload_json TEXT DEFAULT ''
    )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_event_log_room_player ON event_log(room_code, player_id)")
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS player_sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_session_id TEXT NOT NULL,
        room_code TEXT DEFAULT '',
        player_id TEXT DEFAULT '',
        player_name TEXT DEFAULT '',
        status TEXT DEFAULT 'opened',
        first_event_at TEXT DEFAULT '',
        page_open_at TEXT DEFAULT '',
        join_clicked_at TEXT DEFAULT '',
        join_success_at TEXT DEFAULT '',
        last_seen_at TEXT DEFAULT '',
        exit_at TEXT DEFAULT '',
        exit_reason TEXT DEFAULT '',
        elapsed_to_join_ms INTEGER,
        last_event_type TEXT DEFAULT '',
        event_count INTEGER DEFAULT 0,
        prejoin_event_count INTEGER DEFAULT 0,
        ingame_event_count INTEGER DEFAULT 0,
        meta_json TEXT DEFAULT ''
    )"""
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS player_ai_reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT NOT NULL,
        player_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        model_name TEXT DEFAULT '',
        input_json TEXT DEFAULT '',
        analysis_text TEXT DEFAULT '',
        analysis_json TEXT DEFAULT '',
        status TEXT DEFAULT 'pending'
    )"""
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS round_questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT,
        round_no INTEGER,
        question_id TEXT,
        category TEXT,
        block TEXT,
        instrument TEXT,
        item_type TEXT,
        response_model TEXT,
        mode TEXT,
        subtype TEXT,
        question_meta TEXT
    )"""
    )
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_round_questions_room_round ON round_questions(room_code, round_no)")

    if not _has_column(cur, "players", "score"):
        cur.execute("ALTER TABLE players ADD COLUMN score REAL DEFAULT 0")

    if not _has_column(cur, "rooms", "status"):
        cur.execute("ALTER TABLE rooms ADD COLUMN status TEXT")
    if not _has_column(cur, "rooms", "assessment_mode"):
        cur.execute("ALTER TABLE rooms ADD COLUMN assessment_mode TEXT")
    if not _has_column(cur, "rooms", "block_config"):
        cur.execute("ALTER TABLE rooms ADD COLUMN block_config TEXT")
    if not _has_column(cur, "rooms", "wait_for_all_players"):
        cur.execute("ALTER TABLE rooms ADD COLUMN wait_for_all_players INTEGER DEFAULT 1")

    if not _has_column(cur, "answers", "answer_choice"):
        cur.execute("ALTER TABLE answers ADD COLUMN answer_choice INTEGER")
    if not _has_column(cur, "answers", "block"):
        cur.execute("ALTER TABLE answers ADD COLUMN block TEXT")
    if not _has_column(cur, "answers", "instrument"):
        cur.execute("ALTER TABLE answers ADD COLUMN instrument TEXT")
    if not _has_column(cur, "answers", "item_type"):
        cur.execute("ALTER TABLE answers ADD COLUMN item_type TEXT")
    if not _has_column(cur, "answers", "scored_value"):
        cur.execute("ALTER TABLE answers ADD COLUMN scored_value REAL")
    if not _has_column(cur, "answers", "time_spent_ms"):
        cur.execute("ALTER TABLE answers ADD COLUMN time_spent_ms INTEGER")
    if not _has_column(cur, "answers", "partial_credit"):
        cur.execute("ALTER TABLE answers ADD COLUMN partial_credit REAL DEFAULT 0")
    if not _has_column(cur, "answers", "response_quality"):
        cur.execute("ALTER TABLE answers ADD COLUMN response_quality TEXT DEFAULT ''")
    if not _has_column(cur, "answers", "error_pattern"):
        cur.execute("ALTER TABLE answers ADD COLUMN error_pattern TEXT DEFAULT ''")
    if not _has_column(cur, "answers", "skip_reason"):
        cur.execute("ALTER TABLE answers ADD COLUMN skip_reason TEXT DEFAULT ''")
    if not _has_column(cur, "answers", "final_answer_state"):
        cur.execute("ALTER TABLE answers ADD COLUMN final_answer_state TEXT DEFAULT ''")
    if not _has_column(cur, "answers", "first_response_ms"):
        cur.execute("ALTER TABLE answers ADD COLUMN first_response_ms INTEGER")
    if not _has_column(cur, "answers", "last_change_ms"):
        cur.execute("ALTER TABLE answers ADD COLUMN last_change_ms INTEGER")
    if not _has_column(cur, "answers", "change_count"):
        cur.execute("ALTER TABLE answers ADD COLUMN change_count INTEGER DEFAULT 0")
    if not _has_column(cur, "answers", "hesitation_index"):
        cur.execute("ALTER TABLE answers ADD COLUMN hesitation_index REAL DEFAULT 0")
    if not _has_column(cur, "answers", "interaction_summary_json"):
        cur.execute("ALTER TABLE answers ADD COLUMN interaction_summary_json TEXT DEFAULT ''")

    for column, ddl in [
        ("client_session_id", "TEXT"),
        ("room_code", "TEXT DEFAULT ''"),
        ("player_id", "TEXT DEFAULT ''"),
        ("player_name", "TEXT DEFAULT ''"),
        ("status", "TEXT DEFAULT 'opened'"),
        ("first_event_at", "TEXT DEFAULT ''"),
        ("page_open_at", "TEXT DEFAULT ''"),
        ("join_clicked_at", "TEXT DEFAULT ''"),
        ("join_success_at", "TEXT DEFAULT ''"),
        ("last_seen_at", "TEXT DEFAULT ''"),
        ("exit_at", "TEXT DEFAULT ''"),
        ("exit_reason", "TEXT DEFAULT ''"),
        ("elapsed_to_join_ms", "INTEGER"),
        ("last_event_type", "TEXT DEFAULT ''"),
        ("event_count", "INTEGER DEFAULT 0"),
        ("prejoin_event_count", "INTEGER DEFAULT 0"),
        ("ingame_event_count", "INTEGER DEFAULT 0"),
        ("meta_json", "TEXT DEFAULT ''"),
    ]:
        if not _has_column(cur, "player_sessions", column):
            cur.execute(f"ALTER TABLE player_sessions ADD COLUMN {column} {ddl}")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_player_sessions_client_session ON player_sessions(client_session_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_player_sessions_room_player ON player_sessions(room_code, player_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_player_sessions_status ON player_sessions(status)")

    for column, ddl in [
        ("overall_potential", "REAL"),
        ("summary_text", "TEXT"),
        ("fit_tags_json", "TEXT"),
        ("strengths_json", "TEXT"),
        ("growth_zones_json", "TEXT"),
        ("recommendations_json", "TEXT"),
        ("emp_radar_json", "TEXT"),
        ("cognitive_radar_json", "TEXT"),
        ("quality_json", "TEXT"),
        ("game_metrics_json", "TEXT"),
    ]:
        if not _has_column(cur, "player_reports", column):
            cur.execute(f"ALTER TABLE player_reports ADD COLUMN {column} {ddl}")

    for column, ddl in [
        ("players_count", "INTEGER"),
        ("avg_score", "REAL"),
        ("avg_accuracy", "REAL"),
        ("avg_potential", "REAL"),
        ("top_tags_json", "TEXT"),
        ("emp_radar_json", "TEXT"),
        ("cognitive_radar_json", "TEXT"),
        ("summary_json", "TEXT"),
        ("dashboard_json", "TEXT"),
    ]:
        if not _has_column(cur, "room_reports", column):
            cur.execute(f"ALTER TABLE room_reports ADD COLUMN {column} {ddl}")

    for column, ddl in [
        ("response_model", "TEXT"),
        ("mode", "TEXT"),
        ("subtype", "TEXT"),
        ("question_meta", "TEXT"),
    ]:
        if not _has_column(cur, "round_questions", column):
            cur.execute(f"ALTER TABLE round_questions ADD COLUMN {column} {ddl}")

    con.commit()
    con.close()


def db_room_upsert(
    code: str,
    rounds: int,
    status: str,
    assessment_mode: str = "emp_full",
    block_config: Optional[dict] = None,
    wait_for_all_players: bool = True,
):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    block_config_json = json.dumps(
        sanitize_block_config(block_config, fallback_mode=assessment_mode), ensure_ascii=False
    )
    wait_flag = 1 if wait_for_all_players else 0
    cur.execute(
        "INSERT OR IGNORE INTO rooms(code, created_at, rounds, status, assessment_mode, block_config, wait_for_all_players) VALUES(?,?,?,?,?,?,?)",
        (code, int(time.time()), rounds, status, assessment_mode, block_config_json, wait_flag),
    )
    cur.execute(
        "UPDATE rooms SET rounds=?, status=?, assessment_mode=?, block_config=?, wait_for_all_players=? WHERE code=?",
        (rounds, status, assessment_mode, block_config_json, wait_flag, code),
    )
    con.commit()
    con.close()


def db_player_upsert(room_code: str, player_id: str, name: str, score: float):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO players(id, room_code, player_id, name, score)
        VALUES(
            COALESCE((SELECT id FROM players WHERE room_code=? AND player_id=?), NULL),
            ?,?,?,?
        )
    """,
        (room_code, player_id, room_code, player_id, name, score),
    )
    con.commit()
    con.close()


def db_answer_add(
    room_code,
    round_no,
    qid,
    category,
    block,
    instrument,
    item_type,
    player_id,
    player_name,
    text,
    choice,
    is_correct,
    awarded,
    scored_value,
    time_spent_ms,
    partial_credit=0.0,
    response_quality="",
    error_pattern="",
    skip_reason="",
    final_answer_state="",
    first_response_ms=None,
    last_change_ms=None,
    change_count=0,
    hesitation_index=0.0,
    interaction_summary=None,
):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO answers(
            room_code, round_no, question_id, category, block, instrument, item_type, player_id, player_name,
            answer_text, answer_choice, is_correct, awarded, scored_value, time_spent_ms,
            partial_credit, response_quality, error_pattern, skip_reason, final_answer_state,
            first_response_ms, last_change_ms, change_count, hesitation_index, interaction_summary_json
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            room_code,
            round_no,
            qid,
            category,
            block,
            instrument,
            item_type,
            player_id,
            player_name,
            text,
            None if choice is None else int(choice),
            None if is_correct is None else int(bool(is_correct)),
            awarded,
            scored_value,
            time_spent_ms,
            _safe_float(partial_credit, 0.0),
            str(response_quality or ""),
            str(error_pattern or ""),
            str(skip_reason or ""),
            str(final_answer_state or ""),
            None if first_response_ms is None else _safe_int(first_response_ms),
            None if last_change_ms is None else _safe_int(last_change_ms),
            _safe_int(change_count, 0),
            _safe_float(hesitation_index, 0.0),
            _json_dumps(interaction_summary or {}),
        ),
    )
    con.commit()
    con.close()


def db_event_add(room_code: str, player_id: str, round_no: Optional[int], question_id: str, event_type: str, event_ms: Optional[int], payload: Optional[dict] = None):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO event_log(room_code, player_id, round_no, question_id, event_type, event_ts, event_ms, payload_json)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            room_code,
            player_id,
            round_no,
            question_id or "",
            event_type,
            _now_iso(),
            None if event_ms is None else _safe_int(event_ms),
            _json_dumps(payload or {}),
        ),
    )
    con.commit()
    con.close()


def db_round_question_upsert(room_code: str, round_no: int, q: dict):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    snapshot = compact_question_snapshot(q)
    cur.execute(
        """
        INSERT INTO round_questions(
            room_code, round_no, question_id, category, block, instrument, item_type, response_model, mode, subtype, question_meta
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(room_code, round_no) DO UPDATE SET
            question_id=excluded.question_id,
            category=excluded.category,
            block=excluded.block,
            instrument=excluded.instrument,
            item_type=excluded.item_type,
            response_model=excluded.response_model,
            mode=excluded.mode,
            subtype=excluded.subtype,
            question_meta=excluded.question_meta
    """,
        (
            room_code,
            round_no,
            q.get("id"),
            q.get("category"),
            task_block(q),
            task_instrument(q),
            task_item_type(q),
            infer_response_model(q),
            q.get("mode", "base"),
            q.get("subtype"),
            json.dumps(snapshot, ensure_ascii=False),
        ),
    )
    con.commit()
    con.close()



def db_player_session_touch(client_session_id: str, *, room_code: str = "", player_id: str = "", player_name: str = "", event_type: str = "", event_ms: Optional[int] = None, payload: Optional[dict] = None):
    client_session_id = str(client_session_id or "").strip()
    if not client_session_id:
        return
    payload = payload or {}
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM player_sessions WHERE client_session_id=? ORDER BY id DESC LIMIT 1", (client_session_id,))
    row = cur.fetchone()

    data = dict(row) if row else {
        "room_code": "",
        "player_id": "",
        "player_name": "",
        "status": "opened",
        "first_event_at": "",
        "page_open_at": "",
        "join_clicked_at": "",
        "join_success_at": "",
        "last_seen_at": "",
        "exit_at": "",
        "exit_reason": "",
        "elapsed_to_join_ms": None,
        "last_event_type": "",
        "event_count": 0,
        "prejoin_event_count": 0,
        "ingame_event_count": 0,
        "meta_json": "",
    }
    now_iso = _now_iso()

    effective_room_code = str(room_code or payload.get("roomCode") or data.get("room_code") or "").upper()
    effective_player_id = str(player_id or payload.get("playerId") or data.get("player_id") or "")
    effective_player_name = str(player_name or payload.get("playerName") or data.get("player_name") or "").strip()[:64]

    if not data.get("first_event_at"):
        data["first_event_at"] = now_iso
    data["room_code"] = effective_room_code
    data["player_id"] = effective_player_id
    data["player_name"] = effective_player_name
    data["last_seen_at"] = now_iso
    data["last_event_type"] = str(event_type or data.get("last_event_type") or "")
    data["event_count"] = _safe_int(data.get("event_count"), 0) + 1

    if effective_player_id:
        data["ingame_event_count"] = _safe_int(data.get("ingame_event_count"), 0) + 1
    else:
        data["prejoin_event_count"] = _safe_int(data.get("prejoin_event_count"), 0) + 1

    status = str(data.get("status") or "opened")
    if event_type == "page_open":
        if not data.get("page_open_at"):
            data["page_open_at"] = now_iso
        if not status:
            status = "opened"
    elif event_type == "join_clicked":
        if not data.get("join_clicked_at"):
            data["join_clicked_at"] = now_iso
        status = "join_clicked"
    elif event_type == "join_success":
        if not data.get("join_success_at"):
            data["join_success_at"] = now_iso
        elapsed = payload.get("elapsedToJoinMs")
        if elapsed is not None and data.get("elapsed_to_join_ms") in (None, ""):
            data["elapsed_to_join_ms"] = _safe_int(elapsed, 0)
        status = "joined"
    elif event_type == "join_failed":
        status = "join_failed"
    elif event_type in {"session_exit", "socket_disconnected"}:
        if not data.get("exit_at"):
            data["exit_at"] = now_iso
        reason = str(payload.get("reason") or event_type)
        data["exit_reason"] = reason[:64]
        status = "closed" if effective_player_id or status == "joined" else "abandoned"

    data["status"] = status
    data["meta_json"] = _json_dumps({
        "lastPayload": payload,
        "lastEventMs": None if event_ms is None else _safe_int(event_ms),
    })

    values = (
        client_session_id,
        data.get("room_code") or "",
        data.get("player_id") or "",
        data.get("player_name") or "",
        data.get("status") or "opened",
        data.get("first_event_at") or "",
        data.get("page_open_at") or "",
        data.get("join_clicked_at") or "",
        data.get("join_success_at") or "",
        data.get("last_seen_at") or now_iso,
        data.get("exit_at") or "",
        data.get("exit_reason") or "",
        None if data.get("elapsed_to_join_ms") in (None, "") else _safe_int(data.get("elapsed_to_join_ms"), 0),
        data.get("last_event_type") or "",
        _safe_int(data.get("event_count"), 0),
        _safe_int(data.get("prejoin_event_count"), 0),
        _safe_int(data.get("ingame_event_count"), 0),
        data.get("meta_json") or "",
    )

    if row:
        row_id = row["id"]
        cur.execute(
            """
            UPDATE player_sessions SET
                client_session_id=?,
                room_code=?,
                player_id=?,
                player_name=?,
                status=?,
                first_event_at=?,
                page_open_at=?,
                join_clicked_at=?,
                join_success_at=?,
                last_seen_at=?,
                exit_at=?,
                exit_reason=?,
                elapsed_to_join_ms=?,
                last_event_type=?,
                event_count=?,
                prejoin_event_count=?,
                ingame_event_count=?,
                meta_json=?
            WHERE id=?
            """,
            values + (row_id,),
        )
        cur.execute("DELETE FROM player_sessions WHERE client_session_id=? AND id<>?", (client_session_id, row_id))
    else:
        cur.execute(
            """
            INSERT INTO player_sessions(
                client_session_id, room_code, player_id, player_name, status, first_event_at, page_open_at,
                join_clicked_at, join_success_at, last_seen_at, exit_at, exit_reason, elapsed_to_join_ms,
                last_event_type, event_count, prejoin_event_count, ingame_event_count, meta_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )

    con.commit()
    con.close()


def process_telemetry_event(msg: dict):
    code = str(msg.get("roomCode") or "").upper()
    pid = str(msg.get("playerId") or "")
    client_session_id = str(msg.get("clientSessionId") or msg.get("client_session_id") or "").strip()
    event_type = str(msg.get("eventType") or msg.get("event_type") or "").strip()
    if not event_type:
        return

    room = ROOMS.get(code) if code else None
    pc = room.players.get(pid) if room and pid else None
    event_ms = msg.get("eventMs")
    if event_ms is None and room and room.round_started_at:
        event_ms = int((time.time() - room.round_started_at) * 1000)
    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
    round_no = msg.get("round")
    question_id = str(msg.get("questionId") or "")

    if pc:
        _record_interaction_event(pc.round_interaction, event_type, event_ms, payload)
        if round_no is None:
            round_no = room.current_round
        if not question_id and room.current_question:
            question_id = (room.current_question or {}).get("id", "")

    db_event_add(code or "", pid or "", round_no, question_id, event_type, event_ms, payload)
    db_player_session_touch(
        client_session_id,
        room_code=code or str(payload.get("roomCode") or "").upper(),
        player_id=pid,
        player_name=str(payload.get("playerName") or (pc.name if pc else "")),
        event_type=event_type,
        event_ms=event_ms,
        payload=payload,
    )


def db_player_report_upsert(room_code: str, player_id: str, player_name: str, profile: dict):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM player_reports WHERE room_code=? AND player_id=?", (room_code, player_id))
    cur.execute(
        """
        INSERT INTO player_reports(
            room_code, player_id, player_name, overall_potential, summary_text, fit_tags_json,
            strengths_json, growth_zones_json, recommendations_json, emp_radar_json, cognitive_radar_json,
            quality_json, game_metrics_json, profile_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            room_code,
            player_id,
            player_name,
            profile.get("overallPotential"),
            profile.get("summaryText"),
            json.dumps(profile.get("fitTags", []), ensure_ascii=False),
            json.dumps(profile.get("strengths", []), ensure_ascii=False),
            json.dumps(profile.get("growthZones", []), ensure_ascii=False),
            json.dumps(profile.get("recommendations", []), ensure_ascii=False),
            json.dumps(profile.get("empRadar", []), ensure_ascii=False),
            json.dumps(profile.get("cognitiveRadar", []), ensure_ascii=False),
            json.dumps(profile.get("quality", {}), ensure_ascii=False),
            json.dumps(profile.get("gameMetrics", {}), ensure_ascii=False),
            json.dumps(profile, ensure_ascii=False),
            int(time.time()),
        ),
    )
    con.commit()
    con.close()


def db_player_report_get(room_code: str, player_id: str) -> Optional[dict]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT profile_json FROM player_reports WHERE room_code=? AND player_id=? ORDER BY created_at DESC LIMIT 1",
        (room_code, player_id),
    )
    row = cur.fetchone()
    con.close()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def db_all_player_reports(room_code: str) -> List[dict]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT profile_json FROM player_reports WHERE room_code=? ORDER BY overall_potential DESC, player_name ASC",
        (room_code,),
    )
    rows = cur.fetchall()
    con.close()
    out = []
    for row in rows:
        try:
            out.append(json.loads(row[0]))
        except Exception:
            continue
    return out


def db_room_report_upsert(room_code: str, dashboard: dict):
    summary = dashboard.get("summary", {})
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO room_reports(
            room_code, players_count, avg_score, avg_accuracy, avg_potential, top_tags_json,
            emp_radar_json, cognitive_radar_json, summary_json, dashboard_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(room_code) DO UPDATE SET
            players_count=excluded.players_count,
            avg_score=excluded.avg_score,
            avg_accuracy=excluded.avg_accuracy,
            avg_potential=excluded.avg_potential,
            top_tags_json=excluded.top_tags_json,
            emp_radar_json=excluded.emp_radar_json,
            cognitive_radar_json=excluded.cognitive_radar_json,
            summary_json=excluded.summary_json,
            dashboard_json=excluded.dashboard_json,
            created_at=excluded.created_at
    """,
        (
            room_code,
            summary.get("playersCount"),
            summary.get("avgScore"),
            summary.get("avgAccuracy"),
            summary.get("avgPotential"),
            json.dumps(summary.get("topTags", []), ensure_ascii=False),
            json.dumps((dashboard.get("roomRadar") or {}).get("emp", []), ensure_ascii=False),
            json.dumps((dashboard.get("roomRadar") or {}).get("cognitive", []), ensure_ascii=False),
            json.dumps(summary, ensure_ascii=False),
            json.dumps(dashboard, ensure_ascii=False),
            int(time.time()),
        ),
    )
    con.commit()
    con.close()


def db_room_report_get(room_code: str) -> Optional[dict]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT dashboard_json FROM room_reports WHERE room_code=?", (room_code,))
    row = cur.fetchone()
    con.close()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def db_player_ai_report_save(room_code: str, player_id: str, *, model_name: str, input_json: dict, analysis_json: dict, analysis_text: str, status: str = "ready"):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM player_ai_reports WHERE room_code=? AND player_id=?", (room_code, player_id))
    cur.execute(
        """
        INSERT INTO player_ai_reports(
            room_code, player_id, created_at, model_name, input_json, analysis_text, analysis_json, status
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            room_code,
            player_id,
            _now_iso(),
            model_name,
            json.dumps(input_json, ensure_ascii=False),
            analysis_text or "",
            json.dumps(analysis_json, ensure_ascii=False),
            status,
        ),
    )
    con.commit()
    con.close()


def db_player_ai_report_get(room_code: str, player_id: str) -> Optional[dict]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT created_at, model_name, input_json, analysis_text, analysis_json, status
        FROM player_ai_reports
        WHERE room_code=? AND player_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (room_code, player_id),
    )
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return {
        "createdAt": row[0],
        "modelName": row[1],
        "inputJson": _safe_json_loads(row[2], {}),
        "analysisText": row[3] or "",
        "analysisJson": _safe_json_loads(row[4], {}),
        "status": row[5] or "pending",
    }


def _save_ai_debug(name: str, content: str):
    try:
        debug_dir = DATA_DIR / "ai_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        path = debug_dir / f"{ts}_{name}.txt"
        path.write_text(str(content or ""), encoding="utf-8")
    except Exception:
        traceback.print_exc()


def _find_first_complete_json_object(text: str) -> Optional[str]:
    if not text:
        return None

    start = -1
    depth = 0
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if start == -1:
            if ch == "{":
                start = i
                depth = 1
                in_string = False
                escape = False
            continue

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None


def _extract_json_object(raw_text: str) -> dict:
    if isinstance(raw_text, dict):
        return raw_text

    text = str(raw_text or "").strip()
    if not text:
        return {}

    # 1. Пробуем распарсить весь текст как есть
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 2. Если Gemini завернул JSON в ```json ... ```
    code_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    if code_match:
        candidate = code_match.group(1).strip()
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    # 3. Пытаемся найти первый завершённый JSON-объект по балансу скобок
    candidate = _find_first_complete_json_object(text)
    if candidate:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    # 4. Пытаемся декодировать JSON из любого места строки
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(text[i:])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue

    preview = text[:1200]
    raise ValueError(
        "Gemini вернул ответ, который не удалось распознать как JSON. "
        f"Фрагмент ответа: {preview}"
    )


def db_player_ai_report_save(room_code: str, player_id: str, *, model_name: str, input_json: dict, analysis_json: dict, analysis_text: str, status: str = "ready"):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM player_ai_reports WHERE room_code=? AND player_id=?", (room_code, player_id))
    cur.execute(
        """
        INSERT INTO player_ai_reports(
            room_code, player_id, created_at, model_name, input_json, analysis_text, analysis_json, status
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            room_code,
            player_id,
            _now_iso(),
            model_name,
            json.dumps(input_json, ensure_ascii=False),
            analysis_text or "",
            json.dumps(analysis_json, ensure_ascii=False),
            status,
        ),
    )
    con.commit()
    con.close()


def db_player_ai_report_get(room_code: str, player_id: str) -> Optional[dict]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT created_at, model_name, input_json, analysis_text, analysis_json, status
        FROM player_ai_reports
        WHERE room_code=? AND player_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (room_code, player_id),
    )
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return {
        "createdAt": row[0],
        "modelName": row[1],
        "inputJson": _safe_json_loads(row[2], {}),
        "analysisText": row[3] or "",
        "analysisJson": _safe_json_loads(row[4], {}),
        "status": row[5] or "pending",
    }


def _extract_events_for_player(room_code: str, player_id: str) -> List[dict]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT round_no, question_id, event_type, event_ts, event_ms, payload_json
        FROM event_log
        WHERE room_code=? AND player_id=?
        ORDER BY id ASC
        """,
        (room_code, player_id),
    )
    events = [
        {
            "round": r[0],
            "questionId": r[1],
            "eventType": r[2],
            "eventTs": r[3],
            "eventMs": r[4],
            "payload": _safe_json_loads(r[5], {}),
        }
        for r in cur.fetchall()
    ]
    con.close()
    return events


def _build_session_summary_from_events(events: List[dict]) -> dict:
    summary = {
        "pageOpenAt": "",
        "joinClickedAt": "",
        "joinSuccessAt": "",
        "exitAt": "",
        "exitReason": "",
        "elapsedToJoinMs": None,
        "eventCount": len(events),
        "prejoinEventCount": 0,
        "ingameEventCount": 0,
    }
    joined_seen = False
    for ev in events:
        et = str(ev.get("eventType") or "")
        ts = str(ev.get("eventTs") or "")
        payload = ev.get("payload") or {}
        if not joined_seen:
            summary["prejoinEventCount"] += 1
        else:
            summary["ingameEventCount"] += 1
        if et == "page_open" and not summary["pageOpenAt"]:
            summary["pageOpenAt"] = ts
        elif et == "join_clicked" and not summary["joinClickedAt"]:
            summary["joinClickedAt"] = ts
        elif et == "join_success" and not summary["joinSuccessAt"]:
            summary["joinSuccessAt"] = ts
            joined_seen = True
            elapsed = payload.get("elapsedToJoinMs")
            if isinstance(elapsed, (int, float)):
                summary["elapsedToJoinMs"] = int(elapsed)
        elif et in {"session_exit", "socket_disconnected"}:
            summary["exitAt"] = ts
            summary["exitReason"] = et
    return summary


def _normalize_ai_report(data: Any, profile: Optional[dict] = None) -> dict:
    if not isinstance(data, dict):
        data = {}
    profile = profile or {}
    overall = data.get("overall_assessment") if isinstance(data.get("overall_assessment"), dict) else {}
    track = data.get("track_recommendation") if isinstance(data.get("track_recommendation"), dict) else {}
    profession = data.get("profession_recommendation") if isinstance(data.get("profession_recommendation"), dict) else {}
    it_comment = data.get("it_specific_comment") if isinstance(data.get("it_specific_comment"), dict) else {}

    general_fit = str(overall.get("general_fit_level") or "unclear").lower()
    it_fit = str(overall.get("it_fit_level") or "unclear").lower()
    follow_up_needed = bool(overall.get("follow_up_needed"))
    follow_up_reason = str(overall.get("follow_up_reason") or "").strip()

    fit_map = {
        "high": "Высокий общий потенциал",
        "medium": "Умеренный подтверждённый потенциал",
        "low": "Низкий подтверждённый потенциал",
        "unclear": "Требуется дополнительная оценка",
    }
    fit = fit_map.get(general_fit, "Требуется дополнительная оценка")
    fit_sub_parts = []
    if follow_up_reason:
        fit_sub_parts.append(follow_up_reason)
    if it_fit in {"high", "medium", "low", "unclear"}:
        it_phrase = {
            "high": "Сигналы пригодности к IT выражены отчётливо.",
            "medium": "Есть умеренные признаки пригодности к IT.",
            "low": "Прямых сильных оснований для уверенной рекомендации в IT мало.",
            "unclear": "Пригодность именно к IT требует дополнительной проверки.",
        }[it_fit]
        fit_sub_parts.append(it_phrase)
    if follow_up_needed and "дополнитель" not in " ".join(fit_sub_parts).lower():
        fit_sub_parts.append("Нужна дополнительная проверка на интервью или дополнительным кейсом.")
    fit_sub = " ".join([x for x in fit_sub_parts if x]).strip() or "ИИ-вывод сформирован на основе игровых, когнитивных и поведенческих сигналов."

    confidence_val = overall.get("confidence")
    if isinstance(confidence_val, (int, float)):
        if float(confidence_val) <= 1.0:
            confidence = f"{round(max(0.0, min(1.0, float(confidence_val))) * 100)}%"
        else:
            confidence = f"{round(max(0.0, min(100.0, float(confidence_val))))}%"
    else:
        confidence = f"{round(float(profile.get('fitConfidence', 0) or 0))}%"

    primary_track = str(track.get("primary_track") or profile.get("recommendedTrackLabel") or profile.get("recommendedTrack") or "Нужна дополнительная оценка")
    primary_track_sub = str(track.get("why") or "Основной трек выбран на основе совокупности игровых, когнитивных и поведенческих признаков.")

    alt_tracks_raw = track.get("secondary_tracks") if isinstance(track.get("secondary_tracks"), list) else []
    alt_tracks = [str(x) for x in alt_tracks_raw if str(x).strip()]
    if not alt_tracks:
        fallback = profile.get("fitTags") or profile.get("generalFitTags") or []
        alt_tracks = [str(x) for x in fallback[:2] if str(x).strip()]

    primary_role = str(
        profession.get("primary_role")
        or profile.get("primaryRecommendedRole")
        or profile.get("recommendedProfession")
        or "Нужна дополнительная профдиагностика"
    )
    primary_role_cluster = str(
        profession.get("role_cluster")
        or profile.get("primaryRecommendedRoleCluster")
        or "Дополнительная оценка"
    )
    primary_role_sub = str(
        profession.get("why")
        or profile.get("roleRecommendationSummary")
        or "Роль определена как предварительная гипотеза на основе базового профиля EMOPOT."
    )
    alt_roles_raw = profession.get("secondary_roles") if isinstance(profession.get("secondary_roles"), list) else []
    alt_roles = [str(x) for x in alt_roles_raw if str(x).strip()]
    if not alt_roles:
        alt_roles = [str(x) for x in (profile.get("alternativeRecommendedRoles") or []) if str(x).strip()]
    profession_caution = str(profession.get("caution") or "").strip()

    strengths_out = []
    for item in data.get("strengths") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            interp = str(item.get("interpretation") or item.get("evidence") or "").strip()
            text = f"{name}: {interp}" if name and interp else (name or interp)
            if text:
                strengths_out.append(text)
    if not strengths_out:
        strengths_out = [str(x) for x in (profile.get("strengths") or []) if str(x).strip()]

    risks_out = []
    for item in data.get("risk_flags") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            sev = str(item.get("severity") or "").strip()
            alt = str(item.get("alternative_explanation") or "").strip()
            base = name
            if sev:
                base = f"{base} ({sev})" if base else sev
            if alt:
                base = f"{base}: {alt}" if base else alt
            if base:
                risks_out.append(base)
    if not risks_out:
        risks_out = [str(x) for x in (profile.get("riskFlags") or []) if str(x).strip()]

    interview = [str(x) for x in (data.get("interview_focus") or []) if str(x).strip()]
    if not interview:
        interview = [str(x) for x in (profile.get("recommendations") or []) if str(x).strip()]

    limitations = [str(x) for x in (data.get("limitations") or []) if str(x).strip()]
    caution = str(it_comment.get("caution") or "").strip()
    if caution:
        limitations.append(caution)
    if profession_caution:
        limitations.append(profession_caution)
    caveats = " ".join(limitations[:4]).strip() or "ИИ-оценка является вспомогательным слоем и должна использоваться вместе с базовым HR-дашбордом."

    summary = str(data.get("candidate_summary") or profile.get("summaryText") or "ИИ не вернул развёрнутое резюме.")

    return {
        "fit": fit,
        "fitSub": fit_sub,
        "confidence": confidence,
        "primaryTrack": primary_track,
        "primaryTrackSub": primary_track_sub,
        "altTracks": alt_tracks[:4],
        "primaryRole": primary_role,
        "primaryRoleCluster": primary_role_cluster,
        "primaryRoleSub": primary_role_sub,
        "altRoles": alt_roles[:4],
        "summary": summary,
        "strengths": strengths_out[:6],
        "risks": risks_out[:6],
        "interview": interview[:6],
        "caveats": caveats,
        "scientificReport": data,
    }


def _build_ai_payload(room_code: str, player_id: str) -> dict:
    profile = db_player_report_get(room_code, player_id)
    if profile is None:
        build_and_store_reports(room_code)
        profile = db_player_report_get(room_code, player_id)
    if profile is None:
        raise ValueError("player report not found")

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT round_no, question_id, category, block, instrument, item_type,
               answer_text, answer_choice, is_correct, awarded, scored_value, time_spent_ms,
               partial_credit, response_quality, error_pattern, skip_reason, final_answer_state,
               first_response_ms, last_change_ms, change_count, hesitation_index, interaction_summary_json
        FROM answers
        WHERE room_code=? AND player_id=?
        ORDER BY round_no ASC, id ASC
        """,
        (room_code, player_id),
    )
    answers = []
    for r in cur.fetchall():
        answers.append({
            "round": r[0],
            "questionId": r[1],
            "category": r[2],
            "block": r[3],
            "instrument": r[4],
            "itemType": r[5],
            "answerText": r[6],
            "answerChoice": r[7],
            "isCorrect": None if r[8] is None else bool(r[8]),
            "awarded": r[9],
            "scoredValue": r[10],
            "timeMs": r[11],
            "partialCredit": r[12],
            "responseQuality": r[13],
            "errorPattern": r[14],
            "skipReason": r[15],
            "finalAnswerState": r[16],
            "firstResponseMs": r[17],
            "lastChangeMs": r[18],
            "changeCount": r[19],
            "hesitationIndex": r[20],
            "interactionSummary": _safe_json_loads(r[21], {}),
        })

    cur.execute(
        """
        SELECT round_no, question_id, event_type, event_ts, event_ms, payload_json
        FROM event_log
        WHERE room_code=? AND player_id=?
        ORDER BY id ASC
        """,
        (room_code, player_id),
    )
    events = []
    for r in cur.fetchall():
        events.append({
            "round": r[0],
            "questionId": r[1],
            "eventType": r[2],
            "eventTs": r[3],
            "eventMs": r[4],
            "payload": _safe_json_loads(r[5], {}),
        })

    cur.execute(
        """
        SELECT round_no, question_id, category, block, instrument, item_type, response_model, mode, subtype, question_meta
        FROM round_questions
        WHERE room_code=?
        ORDER BY round_no ASC
        """,
        (room_code,),
    )
    questions = []
    for r in cur.fetchall():
        questions.append({
            "round": r[0],
            "questionId": r[1],
            "category": r[2],
            "block": r[3],
            "instrument": r[4],
            "itemType": r[5],
            "responseModel": r[6],
            "mode": r[7],
            "subtype": r[8],
            "meta": _safe_json_loads(r[9], {}),
        })
    con.close()

    return {
        "project": "EMOPOT",
        "task": "Сделай экспертную HR-интерпретацию результатов игрока строго по заданной JSON-схеме. Используй rule-based блок internshipRouting как источник по стажировочному треку, готовности, дефицитам и HR-решению, но обязательно сохрани полноценную рекомендацию: рекомендуемая профессия/роль, альтернативные роли, причины выбора, осторожность вывода и конкретные следующие действия для HR. Не заменяй рекомендацию простым перечислением треков.",
        "roomCode": room_code,
        "playerId": player_id,
        "profile": profile,
        "answers": answers,
        "session": _build_session_summary_from_events(events),
        "events": events[:120],
        "questions": questions,
    }


def _looks_like_truncated_json(text: str) -> bool:
    s = str(text or "").strip()
    if not s.startswith("{"):
        return False
    if s.count("{") > s.count("}"):
        return True
    if not s.endswith("}"):
        return True
    return False


def _call_gemini_ai_report(payload: dict) -> dict:
    api_key = GEMINI_API_KEY
    if not api_key or api_key == "YOUR_API_KEY":
        raise RuntimeError("GEMINI_API_KEY не задан")

    def _build_request_payload(compact_retry: bool = False) -> str:
        req_obj = {
            "project": "EMOPOT",
            "player_data": payload,
        }
        if compact_retry:
            req_obj["output_constraints"] = {
                "mode": "compact_json_retry",
                "candidate_summary_max_chars": 450,
                "short_text_field_max_chars": 180,
                "strengths_max_items": 3,
                "risk_flags_max_items": 3,
                "interview_focus_max_items": 4,
                "limitations_max_items": 4,
                "no_repetition": True
            }
        return json.dumps(req_obj, ensure_ascii=False)

    def _send_once(request_payload: str, max_tokens: int, debug_suffix: str) -> str:
        sdk_error = ""
        sdk_raw_text = ""

        if genai is not None and genai_types is not None:
            try:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=request_payload,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=AI_SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_json_schema=AI_RESPONSE_JSON_SCHEMA,
                        temperature=AI_TEMPERATURE,
                        max_output_tokens=max_tokens,
                    ),
                )

                sdk_raw_text = getattr(response, "text", None) or ""
                if not sdk_raw_text:
                    candidates = getattr(response, "candidates", None) or []
                    parts_text = []
                    for cand in candidates:
                        content = getattr(cand, "content", None)
                        parts = getattr(content, "parts", None) or []
                        for part in parts:
                            txt = getattr(part, "text", None)
                            if txt:
                                parts_text.append(str(txt))
                    sdk_raw_text = "".join(parts_text).strip()

                if not sdk_raw_text:
                    raise RuntimeError("Gemini SDK не вернул текст ответа")

                _save_ai_debug(f"gemini_sdk_raw_{debug_suffix}", sdk_raw_text)
                return sdk_raw_text

            except Exception as e:
                sdk_error = str(e)
                if sdk_raw_text:
                    _save_ai_debug(f"gemini_sdk_error_{debug_suffix}", sdk_raw_text)

        body = {
            "systemInstruction": {
                "parts": [
                    {
                        "text": AI_SYSTEM_PROMPT
                    }
                ]
            },
            "contents": [
                {
                    "parts": [
                        {
                            "text": request_payload
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": AI_TEMPERATURE,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": AI_RESPONSE_JSON_SCHEMA,
            },
        }

        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
            _save_ai_debug(f"gemini_http_error_{debug_suffix}", raw)
            detail = f"; SDK fallback error: {sdk_error}" if sdk_error else ""
            raise RuntimeError(f"Gemini HTTP {getattr(e, 'code', '?')}: {raw[:1500]}{detail}") from e
        except Exception as e:
            detail = f"; SDK fallback error: {sdk_error}" if sdk_error else ""
            raise RuntimeError(f"Ошибка вызова Gemini: {e}{detail}") from e

        _save_ai_debug(f"gemini_rest_raw_response_{debug_suffix}", raw)

        try:
            parsed = json.loads(raw)
        except Exception as e:
            raise RuntimeError(
                f"REST-ответ Gemini не является корректным JSON: {e}. "
                "Сырой ответ сохранён в data/ai_debug"
            ) from e

        candidates = parsed.get("candidates") or []
        if not candidates:
            prompt_feedback = parsed.get("promptFeedback")
            raise RuntimeError(
                "Gemini не вернул candidates. "
                f"promptFeedback={json.dumps(prompt_feedback, ensure_ascii=False)}"
            )

        finish_reason = str(candidates[0].get("finishReason") or "").strip()

        parts = (((candidates[0].get("content") or {}).get("parts")) or [])
        text_parts = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
        content = "".join(text_parts).strip()

        _save_ai_debug(f"gemini_rest_content_{debug_suffix}", content)

        if not content:
            prompt_feedback = parsed.get("promptFeedback")
            raise RuntimeError(
                "Gemini вернул пустой content. "
                f"finishReason={finish_reason}, "
                f"promptFeedback={json.dumps(prompt_feedback, ensure_ascii=False)}"
            )

        if finish_reason.upper().endswith("MAX_TOKENS") or _looks_like_truncated_json(content):
            raise RuntimeError(
                "Gemini оборвал JSON-ответ по длине. "
                f"finishReason={finish_reason or 'unknown'}"
            )

        return content

    first_payload = _build_request_payload(compact_retry=False)

    try:
        first_text = _send_once(first_payload, AI_MAX_OUTPUT_TOKENS, "first")
        return _extract_json_object(first_text)
    except Exception as first_error:
        first_msg = str(first_error)

        need_retry = (
            "MAX_TOKENS" in first_msg.upper()
            or "оборвал JSON-ответ по длине" in first_msg
            or "не удалось распознать как JSON" in first_msg
        )

        if not need_retry:
            raise RuntimeError(f"Не удалось получить корректный JSON от Gemini: {first_msg}") from first_error

        retry_tokens = max(AI_MAX_OUTPUT_TOKENS, 4096)
        retry_payload = _build_request_payload(compact_retry=True)

        try:
            retry_text = _send_once(retry_payload, retry_tokens, "retry_compact")
            return _extract_json_object(retry_text)
        except Exception as retry_error:
            raise RuntimeError(
                "Не удалось распарсить ответ Gemini даже после повторной компактной генерации. "
                f"Первая ошибка: {first_msg}; "
                f"Повторная ошибка: {retry_error}"
            ) from retry_error


def generate_and_store_ai_report(room_code: str, player_id: str) -> dict:
    room_code = room_code.upper()
    payload = _build_ai_payload(room_code, player_id)
    profile = payload.get("profile") or {}
    raw_report = _call_gemini_ai_report(payload)
    normalized = _normalize_ai_report(raw_report, profile)
    db_player_ai_report_save(
        room_code,
        player_id,
        model_name=GEMINI_MODEL,
        input_json={"promptVersion": AI_REPORT_PROMPT_VERSION, "payload": payload},
        analysis_json=normalized,
        analysis_text=normalized.get("summary", ""),
        status="ready",
    )
    return {
        "createdAt": _now_iso(),
        "modelName": GEMINI_MODEL,
        "analysisText": normalized.get("summary", ""),
        "analysisJson": normalized,
        "status": "ready",
    }


def db_room_results(room_code: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT rounds, status, assessment_mode, block_config, wait_for_all_players FROM rooms WHERE code=?", (room_code,))
    row = cur.fetchone()
    rounds = row[0] if row else 0
    status = row[1] if row else "unknown"
    assessment_mode = row[2] if row and row[2] else "emp_full"
    try:
        block_config = (
            json.loads(row[3])
            if row and row[3]
            else sanitize_block_config(None, fallback_mode=assessment_mode)
        )
    except Exception:
        block_config = sanitize_block_config(None, fallback_mode=assessment_mode)
    wait_for_all_players = bool(row[4]) if row and len(row) > 4 and row[4] is not None else True

    cur.execute(
        """
        SELECT player_id, name, score FROM players
        WHERE room_code=?
        ORDER BY score DESC, name ASC
    """,
        (room_code,),
    )
    players = [{"playerId": r[0], "name": r[1], "score": r[2]} for r in cur.fetchall()]

    cur.execute(
        """
        SELECT round_no, question_id, category, block, instrument, item_type, player_id, player_name,
               answer_text, answer_choice, is_correct, awarded, scored_value, time_spent_ms,
               partial_credit, response_quality, error_pattern, skip_reason, final_answer_state,
               first_response_ms, last_change_ms, change_count, hesitation_index, interaction_summary_json
        FROM answers
        WHERE room_code=?
        ORDER BY round_no, player_name
    """,
        (room_code,),
    )
    answers = [
        {
            "round": r[0],
            "questionId": r[1],
            "category": r[2],
            "block": r[3],
            "instrument": r[4],
            "itemType": r[5],
            "playerId": r[6],
            "playerName": r[7],
            "text": r[8],
            "choice": r[9],
            "isCorrect": None if r[10] is None else bool(r[10]),
            "awarded": r[11],
            "scoredValue": r[12],
            "timeMs": r[13],
            "partialCredit": r[14],
            "responseQuality": r[15],
            "errorPattern": r[16],
            "skipReason": r[17],
            "finalAnswerState": r[18],
            "firstResponseMs": r[19],
            "lastChangeMs": r[20],
            "changeCount": r[21],
            "hesitationIndex": r[22],
            "interactionSummary": _safe_json_loads(r[23], {}),
        }
        for r in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT round_no, question_id, category, block, instrument, item_type, response_model, mode, subtype, question_meta
        FROM round_questions
        WHERE room_code=?
        ORDER BY round_no
    """,
        (room_code,),
    )
    round_questions = []
    for r in cur.fetchall():
        round_questions.append(
            {
                "round": r[0],
                "questionId": r[1],
                "category": r[2],
                "block": r[3],
                "instrument": r[4],
                "itemType": r[5],
                "responseModel": r[6],
                "mode": r[7],
                "subtype": r[8],
                "meta": _safe_json_loads(r[9], {}),
            }
        )
    con.close()
    return {
        "roomCode": room_code,
        "rounds": rounds,
        "status": status,
        "assessmentMode": assessment_mode,
        "blockConfig": block_config,
        "waitForAllPlayers": wait_for_all_players,
        "players": players,
        "answers": answers,
        "roundQuestions": round_questions,
    }


# ====================== TASKS ======================
def _normalize_answer(s: str) -> str:
    s = (s or "").strip()
    s = s.replace(",", ".")
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _is_correct_text(user_text: str, accepted: List[str]) -> bool:
    if not accepted:
        return False
    u = _normalize_answer(user_text)
    for a in accepted or []:
        if _normalize_answer(a) == u:
            return True
    try:
        au = _normalize_answer(accepted[0])
        return float(u) == float(au)
    except Exception:
        return False


WORD_LADDER_LISA_NORA_WORDS = {
    "ЛИСА", "ЛИЗА", "ЛИДА", "ЛИГА", "ЛИКА", "ЛИНА", "ЛИПА", "ЛИРА", "ЛИМА",
    "ЛАВА", "ЛАПА", "ЛЕВА", "ЛЕРА", "ЛОЗА", "ЛОРА", "ЛУНА", "ЛУПА",
    "ВЕРА", "ВИЛА", "ВИНА", "ВИРА",
    "ГОРА",
    "ДИРА", "ДОРА",
    "ЖИЛА",
    "ЗОНА",
    "КИРА", "КИСА", "КОСА", "КОЖА", "КОЗА", "КОРА", "КОТА",
    "МАРА", "МЕРА", "МИЛА", "МИНА", "МИРА", "МОДА", "МОРА", "МОНА",
    "НИВА", "НИНА", "НОВА", "НОГА", "НОРА", "НОСА", "НОТА", "НОША", "НОНА",
    "ПАРА", "ПОЗА", "ПОРА",
    "РОЗА",
    "СОРА",
    "ТАРА", "ТОРА",
    "ФОРА",
    "ШОРА",
}


def _letters_diff_one(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    return sum(1 for x, y in zip(a, b) if x != y) == 1


def _extract_russian_words_4(ans_text: str) -> List[str]:
    if not ans_text:
        return []
    text = ans_text.upper().replace("Ё", "Е")
    return re.findall(r"[А-Я]{4}", text)


def _validate_word_ladder_lisa_nora(ans_text: str) -> Tuple[bool, str]:
    words = _extract_russian_words_4(ans_text)
    if not words:
        return False, "цепочка не распознана"
    if words[0] != "ЛИСА":
        return False, f"цепочка должна начинаться со слова «ЛИСА», а у вас «{words[0]}»"
    if words[-1] != "НОРА":
        return False, f"цепочка должна заканчиваться словом «НОРА», а у вас «{words[-1]}»"
    if len(words) < 5:
        return False, "цепочка слишком короткая"

    seen = set()
    for idx, word in enumerate(words, start=1):
        if word in seen:
            return False, f"слово «{word}» повторяется в шаге {idx}"
        seen.add(word)
        if word not in WORD_LADDER_LISA_NORA_WORDS:
            return False, f"слово «{word}» отсутствует в допустимом словаре"

    for prev, cur in zip(words, words[1:]):
        if not _letters_diff_one(prev, cur):
            return False, f"между словами «{prev}» и «{cur}» меняется не одна буква"

    return True, "цепочка принята"


def _check_word_ladder_lisa_nora(ans_text: str) -> bool:
    ok, _ = _validate_word_ladder_lisa_nora(ans_text)
    return ok


def _clock_numbers_layout() -> List[Tuple[int, float, float]]:
    nums: List[Tuple[int, float, float]] = []
    center_x = 115.0
    center_y = 115.0
    radius = 90.0
    for i in range(1, 13):
        angle = math.radians((i - 3) * 30)
        x = center_x + radius * math.cos(angle)
        y = center_y + radius * math.sin(angle)
        nums.append((i, x, y))
    return nums


CLOCK_NUMBERS_LAYOUT = _clock_numbers_layout()


def _compute_clock_sums_for_deg(deg: float) -> Tuple[int, int]:
    center_x = 115.0
    center_y = 115.0
    line_angle = math.radians(deg - 90.0)
    nx = -math.sin(line_angle)
    ny = math.cos(line_angle)

    s_a = 0
    s_b = 0
    for value, x, y in CLOCK_NUMBERS_LAYOUT:
        px = x - center_x
        py = y - center_y
        dot = px * nx + py * ny
        if dot > 0:
            s_a += value
        else:
            s_b += value
    return s_a, s_b


def _validate_clock_equal_sums(ans_text: str) -> Tuple[bool, str]:
    if not ans_text:
        return False, "ответ не получен"

    text = str(ans_text).strip()
    normalized = _normalize_answer(text)

    if normalized in {"39", "39.0", "39,0"}:
        return True, "верная общая сумма 39"

    sum_match = re.search(r"sum\s*=\s*(-?\d+(?:[.,]\d+)?)", text, flags=re.IGNORECASE)
    angle_match = re.search(r"angle\s*=\s*(-?\d+(?:[.,]\d+)?)", text, flags=re.IGNORECASE)
    sums_match = re.search(r"sums\s*=\s*(-?\d+)\s*,\s*(-?\d+)", text, flags=re.IGNORECASE)

    entered_sum: Optional[float] = None
    if sum_match:
        try:
            entered_sum = float(sum_match.group(1).replace(",", "."))
        except ValueError:
            return False, "не удалось распознать сумму"
    elif re.fullmatch(r"-?\d+(?:[.,]\d+)?", normalized):
        try:
            entered_sum = float(normalized.replace(",", "."))
        except ValueError:
            return False, "не удалось распознать сумму"

    if angle_match:
        try:
            deg = float(angle_match.group(1).replace(",", "."))
        except ValueError:
            return False, "не удалось распознать угол"

        s_a, s_b = _compute_clock_sums_for_deg(deg)
        if s_a != s_b:
            return False, f"при повороте {deg:g}° получаются суммы {s_a} и {s_b}, они не равны"

        if entered_sum is None:
            return False, f"линия стоит верно, но сумма не указана; правильная сумма {s_a}"

        if abs(entered_sum - s_a) > 1e-9:
            return False, f"линия стоит верно, но общая сумма должна быть {s_a:g}, а у вас {entered_sum:g}"

        return True, f"циферблат разделён верно, общая сумма {s_a:g}"

    if sums_match:
        try:
            s_a = int(sums_match.group(1))
            s_b = int(sums_match.group(2))
        except ValueError:
            return False, "не удалось распознать суммы по сторонам линии"

        if s_a != s_b:
            return False, f"по сторонам линии получаются суммы {s_a} и {s_b}, они не равны"

        if entered_sum is None:
            return True, f"линия делит циферблат на две равные части по {s_a}"

        if abs(entered_sum - s_a) > 1e-9:
            return False, f"линия делит циферблат верно, но нужно было указать сумму {s_a:g}, а у вас {entered_sum:g}"

        return True, f"циферблат разделён верно, общая сумма {s_a:g}"

    if entered_sum is not None:
        if abs(entered_sum - 39.0) <= 1e-9:
            return True, "верная общая сумма 39"
        return False, f"общая сумма должна быть 39, а у вас {entered_sum:g}"

    return False, "не удалось распознать ответ по циферблату"


def _check_clock_equal_sums(ans_text: str) -> bool:
    ok, _ = _validate_clock_equal_sums(ans_text)
    return ok


def _evaluate_clock_equal_sums_partial(ans_text: str, q: Optional[dict] = None) -> Tuple[bool, float, str, str]:
    q = q or {}
    text = str(ans_text or "").strip()
    if not text:
        return False, 0.0, "rule_misunderstood", "неверно"

    normalized = _normalize_answer(text)
    partial_cfg = q.get("partialScoring") if isinstance(q.get("partialScoring"), dict) else {}

    def _pc(key: str, default: float) -> float:
        value = partial_cfg.get(key, default)
        return round(_clamp(_safe_float(value, default), 0.0, 1.0), 2)

    sum_match = re.search(r"sum\s*=\s*(-?\d+(?:[.,]\d+)?)", text, flags=re.IGNORECASE)
    angle_match = re.search(r"angle\s*=\s*(-?\d+(?:[.,]\d+)?)", text, flags=re.IGNORECASE)
    sums_match = re.search(r"sums\s*=\s*(-?\d+)\s*,\s*(-?\d+)", text, flags=re.IGNORECASE)

    entered_sum: Optional[float] = None
    if sum_match:
        try:
            entered_sum = float(sum_match.group(1).replace(",", "."))
        except ValueError:
            entered_sum = None
    elif re.fullmatch(r"-?\d+(?:[.,]\d+)?", normalized):
        try:
            entered_sum = float(normalized.replace(",", "."))
        except ValueError:
            entered_sum = None

    angle_ok = False
    equal_parts_only = False
    target_sum: Optional[float] = None

    if angle_match:
        try:
            deg = float(angle_match.group(1).replace(",", "."))
            s_a, s_b = _compute_clock_sums_for_deg(deg)
            if s_a == s_b:
                angle_ok = True
                target_sum = float(s_a)
        except ValueError:
            angle_ok = False
    elif sums_match:
        try:
            s_a = int(sums_match.group(1))
            s_b = int(sums_match.group(2))
            if s_a == s_b:
                equal_parts_only = True
                target_sum = float(s_a)
        except ValueError:
            equal_parts_only = False

    sum_ok = False
    if entered_sum is not None:
        if target_sum is not None:
            sum_ok = abs(entered_sum - target_sum) <= 1e-9
        else:
            sum_ok = abs(entered_sum - 39.0) <= 1e-9

    if angle_ok and sum_ok:
        return True, 1.0, "", "верно"
    if angle_ok and entered_sum is None:
        pc = _pc("equal_parts_without_sum", 0.5)
        return False, pc, "partial_correct", "линия стоит верно, сумма не указана"
    if angle_ok and entered_sum is not None and not sum_ok:
        pc = _pc("correct_angle_wrong_sum", 0.5)
        return False, pc, "execution_error", "верный угол, неверная сумма"
    if equal_parts_only and entered_sum is None:
        pc = _pc("equal_parts_without_sum", 0.5)
        return False, pc, "partial_correct", "частично верно"
    if equal_parts_only and sum_ok:
        return True, 1.0, "", "верно"
    if entered_sum is not None and sum_ok and not angle_ok:
        pc = _pc("wrong_angle_correct_sum", 0.5)
        return False, pc, "partial_correct", "верная сумма, неверный угол"

    ok, _reason = _validate_clock_equal_sums(ans_text)
    if ok:
        return True, 1.0, "", "верно"
    return False, 0.0, "rule_misunderstood", "неверно"


def _check_robot_pair_to_target(ans_text: str) -> bool:
    if not ans_text:
        return False
    nums = re.findall(r"-?\d+", ans_text)
    if len(nums) < 2:
        return False
    try:
        a = int(nums[0])
        b = int(nums[1])
    except ValueError:
        return False
    if a <= 0 or b <= 0:
        return False
    return (a * b - 5) == 72


def load_tasks_raw() -> dict:
    if not TASKS_PATH.exists():
        TASKS_PATH.write_text("{}", encoding="utf-8")
    try:
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


OPTIONAL_TASK_KEYS = [
    "subtype",
    "difficulty",
    "timeRef",
    "tags",
    "reverse",
    "scaleKey",
    "optionScores",
    "cardImage",
    "cardAxisWeights",
    "controlType",
    "expectedChoice",
    "partialScoring",
]


def transform_tasks(raw: dict) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    id_counter = 1

    for category, arr in (raw or {}).items():
        if not isinstance(arr, list):
            continue
        out_list: List[dict] = []

        for q in arr:
            if not isinstance(q, dict):
                continue

            raw_mode = str(q.get("mode", "base")).strip() or "base"
            raw_block = normalize_block_name(q.get("block") or default_block_for(category, raw_mode))
            response_model = infer_response_model({**q, "block": raw_block})
            instrument = str(q.get("instrument") or q.get("subtype") or category).strip() or category
            item_type = str(
                q.get("itemType")
                or ("cognitive_card" if raw_mode == "card" else ("survey_item" if response_model == "survey" else "quiz_item"))
            ).strip()

            if "type" in q and "title" in q:
                qtype = q.get("type")
                if qtype not in ("mcq", "text"):
                    continue
                item = {
                    "id": q.get("id") or f"q{id_counter}",
                    "category": category,
                    "block": raw_block,
                    "instrument": instrument,
                    "itemType": item_type,
                    "responseModel": response_model,
                    "type": qtype,
                    "prompt": str(q.get("title", "")).strip(),
                    "mode": raw_mode,
                }
                id_counter += 1

                for key in OPTIONAL_TASK_KEYS:
                    if key in q:
                        item[key] = q[key]

                if qtype == "mcq":
                    opts = q.get("options") or []
                    if not opts:
                        continue
                    item["options"] = [str(x) for x in opts]
                    if q.get("correctIndex") is not None:
                        item["correctIndex"] = int(q.get("correctIndex"))
                        if "expectedChoice" not in item:
                            item["expectedChoice"] = int(q.get("correctIndex"))
                    elif response_model != "survey":
                        continue
                else:
                    acc = q.get("accept") or []
                    item["accept"] = [str(x) for x in acc]
                    if not item["accept"] and response_model != "survey":
                        continue
                out_list.append(item)
                continue

            if "prompt" in q and "answers" in q:
                item = {
                    "id": q.get("id") or f"q{id_counter}",
                    "category": category,
                    "block": raw_block,
                    "instrument": instrument,
                    "itemType": item_type,
                    "responseModel": response_model,
                    "type": "text",
                    "prompt": str(q.get("prompt", "")).strip(),
                    "accept": [str(x) for x in (q.get("answers") or [])],
                    "mode": raw_mode,
                }
                id_counter += 1
                for key in OPTIONAL_TASK_KEYS:
                    if key in q:
                        item[key] = q[key]
                if item["accept"] or response_model == "survey":
                    out_list.append(item)
                continue

        if out_list:
            out[category] = out_list

    return out


RAW_TASKS = load_tasks_raw()
TASK_BANK = transform_tasks(RAW_TASKS)
CATEGORIES = list(TASK_BANK.keys())
TASK_BY_ID = {q["id"]: q for q in TASK_BANK.values() for q in q}


def task_counts():
    total = 0
    per_cat: Dict[str, int] = {}
    per_block: Dict[str, int] = {}
    per_instrument: Dict[str, int] = {}
    for q in _all_tasks():
        total += 1
        per_cat[q["category"]] = per_cat.get(q["category"], 0) + 1
        b = task_block(q)
        per_block[b] = per_block.get(b, 0) + 1
        instr = task_instrument(q)
        per_instrument[instr] = per_instrument.get(instr, 0) + 1
    return {"total": total, "categories": per_cat, "blocks": per_block, "instruments": per_instrument}


def _all_tasks():
    for cat in CATEGORIES:
        for q in TASK_BANK.get(cat, []):
            yield q


def _allowed_by_block_config(q: dict, block_config: Optional[dict]) -> bool:
    if not block_config:
        return True
    return bool(block_config.get(task_block(q), False))


def pick_question(used_ids: set, desired_mode: Optional[str] = None, block_config: Optional[dict] = None) -> dict:
    candidates: List[dict] = []

    for cat in CATEGORIES:
        for q in TASK_BANK.get(cat, []):
            if q["id"] in used_ids:
                continue
            if not _allowed_by_block_config(q, block_config):
                continue
            mode = q.get("mode", "base")
            if desired_mode == "non_card" and mode == "card":
                continue
            if desired_mode not in (None, "non_card") and mode != desired_mode:
                continue
            candidates.append(q)

    if not candidates and desired_mode is not None:
        for cat in CATEGORIES:
            for q in TASK_BANK.get(cat, []):
                if q["id"] in used_ids:
                    continue
                if not _allowed_by_block_config(q, block_config):
                    continue
                candidates.append(q)

    if candidates:
        return random.choice(candidates)

    fallback_pool = [q for q in _all_tasks() if _allowed_by_block_config(q, block_config)]
    if fallback_pool:
        return random.choice(fallback_pool)

    if not CATEGORIES:
        return {
            "id": "none",
            "category": "N/A",
            "block": "legacy_misc",
            "instrument": "none",
            "itemType": "quiz_item",
            "responseModel": "quiz",
            "type": "text",
            "prompt": "(Нет задач)",
            "accept": [""],
            "mode": "base",
        }

    cat = random.choice(CATEGORIES)
    return random.choice(TASK_BANK[cat])


# ====================== ROOMS ======================
def gen_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(4))


@dataclass
class PlayerConn:
    ws: WebSocket
    id: str
    name: str
    score: float = 0.0
    answered: bool = False
    ans_text: str = ""
    ans_choice: Optional[int] = None
    ans_time_ms: int = 0
    connected_at: float = field(default_factory=time.time)
    client_session_id: str = ""
    round_interaction: dict = field(default_factory=init_interaction_state)


@dataclass
class Room:
    code: str
    rounds: int = 6
    admin: Optional[WebSocket] = None
    players: Dict[str, PlayerConn] = field(default_factory=dict)
    status: str = "lobby"
    current_round: int = 0
    current_question: Optional[dict] = None
    used_ids: set = field(default_factory=set)
    round_deadline: float = 0.0
    time_limit: int = 0
    timer_task: Optional[asyncio.Task] = None
    assessment_mode: str = "emp_full"
    block_config: Dict[str, bool] = field(default_factory=lambda: sanitize_block_config(None, fallback_mode="emp_full"))
    wait_for_all_players: bool = True
    session_plan: List[dict] = field(default_factory=list)
    round_started_at: float = 0.0

    def snapshot_players(self, include_answered: bool = False):
        items = []
        for p in self.players.values():
            row = {"playerId": p.id, "name": p.name, "score": p.score}
            if include_answered:
                row["answered"] = bool(p.answered)
            items.append(row)
        return items


ROOMS: Dict[str, Room] = {}
CLIENT_TO_ROOM: Dict[WebSocket, str] = {}


EMP_RADAR_GROUPS = {
    "motivation": ("Мотивация", ["achievement_drive", "growth_orientation", "persistence", "failure_avoidance"], {"failure_avoidance"}),
    "communication": ("Коммуникация", ["communication", "empathy", "teamwork", "assertive_communication"], set()),
    "self_regulation": ("Саморегуляция", ["planning", "self_regulation", "adaptability", "self_control", "persistence"], set()),
    "it_career": ("ИТ-вектор", ["professional_orientation", "innovation_orientation", "it_orientation", "learning_value"], set()),
    "values": ("Ценности", ["meaning_orientation", "ethical_orientation", "responsibility", "respect_orientation", "team_values", "user_value_orientation"], set()),
    "stability": ("Рабочая устойчивость", ["stability_orientation", "adaptability", "self_control"], set()),
}

COGNITIVE_LABELS = {
    "numerical_reasoning": "Числовое мышление",
    "abstract_reasoning": "Абстрактное мышление",
    "planning": "Планирование",
    "spatial_reasoning": "Пространственное мышление",
    "verbal_reasoning": "Вербальная логика",
    "data_interpretation": "Интерпретация данных",
}

CATEGORY_FALLBACK_CARD_AXES = {
    "Математика": {"numerical_reasoning": 0.75, "planning": 0.25},
    "Логика": {"abstract_reasoning": 0.7, "verbal_reasoning": 0.3},
    "Анализ данных": {"data_interpretation": 0.7, "numerical_reasoning": 0.3},
}

RECOMMENDATION_LIBRARY = {
    "communication": "Комфортно проявляется в задачах, где нужно договариваться, уточнять требования и удерживать общий ритм команды.",
    "motivation": "Лучше всего раскрывается в среде с понятными целями, заметным прогрессом и регулярной обратной связью по результату.",
    "self_regulation": "Можно уверенно давать самостоятельные участки работы с дедлайнами, чек-листами и необходимостью доводить задачу до конца.",
    "it_career": "Есть хороший задел для включения в ИТ-задачи, где важны интерес к продукту, данным, новым инструментам и поиску решений.",
    "values": "Сильнее проявляется в командах с прозрачными правилами, уважительной коммуникацией и культурой ответственного взаимодействия.",
    "planning": "Подойдут задачи с декомпозицией, маршрутами решения, несколькими шагами и необходимостью заранее продумывать ход работы.",
    "numerical_reasoning": "Есть смысл подключать к задачам с расчётами, ограничениями, таблицами и проверкой гипотез на числовых данных.",
    "abstract_reasoning": "Хорошо включается в поиск закономерностей, нетривиальные правила, структурное рассуждение и формализацию идей.",
    "spatial_reasoning": "Подходит для визуально-пространственных задач, схем, сеток, маршрутов и мысленной перестройки объектов.",
    "data_interpretation": "Можно давать работу с графиками, аналитикой, метриками и объяснением динамики данных на понятном языке.",
}

EMP_AXIS_LABELS = {
    "achievement_drive": "Достижение результата",
    "growth_orientation": "Ориентация на развитие",
    "persistence": "Настойчивость",
    "failure_avoidance": "Избегание неудач",
    "communication": "Коммуникация",
    "empathy": "Эмпатия",
    "teamwork": "Командное взаимодействие",
    "assertive_communication": "Уверенная коммуникация",
    "planning": "Планирование",
    "self_regulation": "Саморегуляция",
    "adaptability": "Адаптивность",
    "self_control": "Самоконтроль",
    "professional_orientation": "Профессиональная ориентация",
    "innovation_orientation": "Ориентация на новое",
    "it_orientation": "ИТ-ориентация",
    "learning_value": "Ценность обучения",
    "meaning_orientation": "Смысл работы",
    "ethical_orientation": "Этичность",
    "responsibility": "Ответственность",
    "respect_orientation": "Уважение",
    "team_values": "Командные ценности",
    "user_value_orientation": "Ориентация на пользователя",
    "stability_orientation": "Устойчивость",
}

DERIVED_SIGNAL_LABELS = {
    "overall_professional_fit": "Общий профессиональный fit",
    "it_fit": "IT-fit",
    "fit_confidence": "Надёжность вывода",
    "it_fit_confidence": "Надёжность IT-fit",
    "attention_pass_rate": "Контроль внимания",
    "social_desirability_index": "Социальная желательность",
    "consistency_index": "Согласованность ответов",
    "response_stability_index": "Стабильность ответов",
    "skip_tendency": "Доля пропусков",
    "persistence_index": "Настойчивость в решении",
    "speed_index": "Скорость решения",
    "cognitive_accuracy": "Когнитивная точность",
    "data_quality_index": "Качество данных",
    "communication_radar": "Коммуникационный контур",
    "motivation_radar": "Мотивационный контур",
    "self_regulation_radar": "Саморегуляция",
    "values_radar": "Ценностный контур",
    "stability_radar": "Рабочая устойчивость",
    "it_career_radar": "ИТ-вектор",
}

ROLE_MATCH_LIBRARY = [
    {
        "key": "data_analyst_intern",
        "label": "Аналитик данных / BI intern",
        "cluster": "Аналитика и данные",
        "track": "it_analytics",
        "signals": [
            {"key": "data_interpretation", "weight": 0.24, "critical": True},
            {"key": "numerical_reasoning", "weight": 0.18, "critical": True},
            {"key": "planning", "weight": 0.12, "critical": True},
            {"key": "abstract_reasoning", "weight": 0.10, "critical": True},
            {"key": "professional_orientation", "weight": 0.08},
            {"key": "learning_value", "weight": 0.06},
            {"key": "attention_pass_rate", "weight": 0.10, "critical": True},
            {"key": "consistency_index", "weight": 0.06, "critical": True},
            {"key": "overall_professional_fit", "weight": 0.06},
        ],
        "minimums": {"it_fit": 55.0, "fit_confidence": 50.0},
        "detail_sensitive": True,
        "summary": "Хорошо подходит для стажировок, где нужно читать метрики, находить закономерности и формулировать выводы по данным.",
    },
    {
        "key": "system_business_analyst_intern",
        "label": "Системный / бизнес-аналитик intern",
        "cluster": "Аналитика и постановка задач",
        "track": "it_analytics",
        "signals": [
            {"key": "data_interpretation", "weight": 0.18, "critical": True},
            {"key": "verbal_reasoning", "weight": 0.15, "critical": True},
            {"key": "communication", "weight": 0.14, "critical": True},
            {"key": "planning", "weight": 0.13, "critical": True},
            {"key": "abstract_reasoning", "weight": 0.10},
            {"key": "user_value_orientation", "weight": 0.10},
            {"key": "attention_pass_rate", "weight": 0.10},
            {"key": "overall_professional_fit", "weight": 0.10},
        ],
        "minimums": {"overall_professional_fit": 55.0, "fit_confidence": 50.0},
        "detail_sensitive": True,
        "summary": "Подходит для ролей, где важно разбирать требования, переводить бизнес-задачу в структуру и поддерживать ясную коммуникацию.",
    },
    {
        "key": "qa_intern",
        "label": "QA / тестировщик intern",
        "cluster": "Контроль качества",
        "track": "it_engineering",
        "signals": [
            {"key": "attention_pass_rate", "weight": 0.18, "critical": True},
            {"key": "consistency_index", "weight": 0.17, "critical": True},
            {"key": "planning", "weight": 0.14, "critical": True},
            {"key": "data_interpretation", "weight": 0.11},
            {"key": "self_control", "weight": 0.10},
            {"key": "responsibility", "weight": 0.10},
            {"key": "response_stability_index", "weight": 0.10},
            {"key": "overall_professional_fit", "weight": 0.10},
        ],
        "minimums": {"fit_confidence": 52.0, "attention_pass_rate": 70.0},
        "detail_sensitive": True,
        "summary": "Лучше всего проявится в задачах на проверку, воспроизведение сценариев, внимательность к деталям и дисциплину выполнения.",
    },
    {
        "key": "backend_intern",
        "label": "Backend-разработчик intern",
        "cluster": "Разработка",
        "track": "it_engineering",
        "signals": [
            {"key": "abstract_reasoning", "weight": 0.18, "critical": True},
            {"key": "planning", "weight": 0.16, "critical": True},
            {"key": "numerical_reasoning", "weight": 0.14, "critical": True},
            {"key": "self_regulation", "weight": 0.12},
            {"key": "learning_value", "weight": 0.09},
            {"key": "it_orientation", "weight": 0.09},
            {"key": "persistence_index", "weight": 0.08},
            {"key": "response_stability_index", "weight": 0.06},
            {"key": "overall_professional_fit", "weight": 0.08},
        ],
        "minimums": {"it_fit": 58.0, "fit_confidence": 50.0},
        "detail_sensitive": True,
        "summary": "Есть потенциал для задач, где важны логика, структура решения, декомпозиция и устойчивое доведение технической задачи до результата.",
    },
    {
        "key": "frontend_intern",
        "label": "Frontend-разработчик intern",
        "cluster": "Разработка",
        "track": "it_engineering",
        "signals": [
            {"key": "abstract_reasoning", "weight": 0.14, "critical": True},
            {"key": "verbal_reasoning", "weight": 0.12},
            {"key": "communication", "weight": 0.10},
            {"key": "user_value_orientation", "weight": 0.12},
            {"key": "planning", "weight": 0.10},
            {"key": "it_orientation", "weight": 0.09},
            {"key": "innovation_orientation", "weight": 0.09},
            {"key": "spatial_reasoning", "weight": 0.10},
            {"key": "overall_professional_fit", "weight": 0.14},
        ],
        "minimums": {"it_fit": 54.0, "fit_confidence": 48.0},
        "detail_sensitive": True,
        "summary": "Подходит для ролей, где нужно сочетать логику, работу с интерфейсом, внимание к пользователю и аккуратную коммуникацию с командой.",
    },
    {
        "key": "product_project_intern",
        "label": "Product / project intern",
        "cluster": "Координация и продукт",
        "track": "coordination",
        "signals": [
            {"key": "communication", "weight": 0.18, "critical": True},
            {"key": "teamwork", "weight": 0.14, "critical": True},
            {"key": "planning", "weight": 0.14, "critical": True},
            {"key": "self_regulation", "weight": 0.12},
            {"key": "responsibility", "weight": 0.10},
            {"key": "empathy", "weight": 0.10},
            {"key": "user_value_orientation", "weight": 0.08},
            {"key": "response_stability_index", "weight": 0.06},
            {"key": "overall_professional_fit", "weight": 0.08},
        ],
        "minimums": {"overall_professional_fit": 58.0, "fit_confidence": 48.0},
        "detail_sensitive": False,
        "summary": "Сильнее проявится в задачах на координацию, сбор требований, удержание сроков и взаимодействие между людьми и задачами.",
    },
    {
        "key": "ux_research_intern",
        "label": "UX research / user research intern",
        "cluster": "Исследование пользователя",
        "track": "coordination",
        "signals": [
            {"key": "empathy", "weight": 0.16, "critical": True},
            {"key": "communication", "weight": 0.15, "critical": True},
            {"key": "user_value_orientation", "weight": 0.15, "critical": True},
            {"key": "verbal_reasoning", "weight": 0.12},
            {"key": "data_interpretation", "weight": 0.10},
            {"key": "abstract_reasoning", "weight": 0.08},
            {"key": "innovation_orientation", "weight": 0.06},
            {"key": "attention_pass_rate", "weight": 0.08},
            {"key": "overall_professional_fit", "weight": 0.10},
        ],
        "minimums": {"overall_professional_fit": 55.0, "fit_confidence": 48.0},
        "detail_sensitive": False,
        "summary": "Подходит для ролей на стыке людей, продукта и наблюдения за пользовательским опытом.",
    },
    {
        "key": "support_implementation_intern",
        "label": "Техническая поддержка / внедрение intern",
        "cluster": "Поддержка и внедрение",
        "track": "general_professional",
        "signals": [
            {"key": "communication", "weight": 0.16, "critical": True},
            {"key": "self_control", "weight": 0.12},
            {"key": "adaptability", "weight": 0.12},
            {"key": "responsibility", "weight": 0.12},
            {"key": "verbal_reasoning", "weight": 0.12},
            {"key": "teamwork", "weight": 0.08},
            {"key": "attention_pass_rate", "weight": 0.10},
            {"key": "response_stability_index", "weight": 0.08},
            {"key": "overall_professional_fit", "weight": 0.10},
        ],
        "minimums": {"overall_professional_fit": 52.0, "fit_confidence": 45.0},
        "detail_sensitive": False,
        "summary": "Есть база для ролей, где важны спокойная коммуникация, адаптивность, дисциплина и разбор пользовательских запросов.",
    },
]


def _safe_json_loads(value: Any, default: Any):
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _mean(values: List[float], default: float = 0.0) -> float:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return float(default)
    return sum(nums) / len(nums)


def _normalize_likert_to_pct(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return _clamp(((float(value) - 1.0) / 4.0) * 100.0)


def _question_meta_map(room_results: dict) -> Dict[Tuple[int, str], dict]:
    meta_by_key: Dict[Tuple[int, str], dict] = {}
    for item in room_results.get("roundQuestions", []) or []:
        meta = item.get("meta") or {}
        snapshot = {**meta}
        snapshot.setdefault("id", item.get("questionId"))
        snapshot.setdefault("category", item.get("category"))
        snapshot.setdefault("block", item.get("block"))
        snapshot.setdefault("instrument", item.get("instrument"))
        snapshot.setdefault("itemType", item.get("itemType"))
        snapshot.setdefault("responseModel", item.get("responseModel"))
        snapshot.setdefault("mode", item.get("mode"))
        snapshot.setdefault("subtype", item.get("subtype"))
        meta_by_key[(item.get("round"), item.get("questionId"))] = snapshot
    for ans in room_results.get("answers", []) or []:
        key = (ans.get("round"), ans.get("questionId"))
        if key not in meta_by_key:
            q = TASK_BY_ID.get(ans.get("questionId"))
            if q:
                meta_by_key[key] = compact_question_snapshot(q)
            else:
                meta_by_key[key] = {
                    "id": ans.get("questionId"),
                    "category": ans.get("category"),
                    "block": ans.get("block"),
                    "instrument": ans.get("instrument"),
                    "itemType": ans.get("itemType"),
                    "responseModel": "survey" if str(ans.get("block") or "").startswith("emp_") else "quiz",
                    "mode": "base",
                }
    return meta_by_key


def _card_score(answer: dict, meta: dict) -> float:
    partial = _safe_float(answer.get("partialCredit"), 0.0)
    is_correct = bool(answer.get("isCorrect"))
    if answer.get("isCorrect") is None:
        is_correct = False
    base = 100.0 if is_correct else (_clamp(partial, 0.0, 1.0) * 100.0)
    time_ref = meta.get("timeRef") or 0
    time_ms = answer.get("timeMs") or 0
    if time_ref and time_ms:
        speed = _clamp((float(time_ref) / max(float(time_ms) / 1000.0, 1.0)) * 100.0)
    else:
        speed = 0.0
    return base * 0.8 + speed * 0.2


def _flatten_tags(items: List[str]) -> str:
    return " | ".join([str(x) for x in items if x])


def _question_short(meta: dict) -> str:
    prompt = str(meta.get("prompt") or "").strip()
    return prompt if len(prompt) <= 140 else prompt[:137] + "..."


def finalize_weighted_axes(weighted_sum: Dict[str, float], weight_total: Dict[str, float]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for key, total in weighted_sum.items():
        w = float(weight_total.get(key, 0.0) or 0.0)
        if w > 0:
            result[key] = _clamp(total / w)
    return result


def profile_level_text(overall_potential: float) -> str:
    if overall_potential >= 80:
        return "высокий"
    if overall_potential >= 65:
        return "хороший"
    if overall_potential >= 50:
        return "умеренный"
    return "начальный"


def top_labels(items: List[dict], limit: int = 2) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in sorted(items, key=lambda x: float(x.get("value", 0.0)), reverse=True):
        label = str(item.get("label") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        result.append(label)
        if len(result) >= limit:
            break
    return result


def build_player_badges(rank: Optional[int], overall_potential: float, cognitive_accuracy: float, speed_index: float, attention_pass_rate: float) -> List[str]:
    badges: List[str] = []
    if rank == 1:
        badges.append("🥇 1 место")
    elif rank == 2:
        badges.append("🥈 2 место")
    elif rank == 3:
        badges.append("🥉 3 место")
    elif rank is not None:
        badges.append(f"#{rank} место")

    if overall_potential >= 85:
        badges.append("🚀 Очень высокий потенциал")
    elif overall_potential >= 70:
        badges.append("🌟 Сильный потенциал")

    if cognitive_accuracy >= 95:
        badges.append("🎯 Почти без ошибок")
    elif cognitive_accuracy >= 80:
        badges.append("✅ Уверенная точность")

    if speed_index >= 85:
        badges.append("⚡ Молниеносный темп")
    elif speed_index >= 70:
        badges.append("🏃 Быстрый темп")

    if attention_pass_rate >= 100:
        badges.append("🧭 Идеальный контроль внимания")
    elif attention_pass_rate >= 80:
        badges.append("👀 Надёжное внимание")

    return badges


def _top_axis_keys(items: List[dict], threshold: float = 60.0) -> List[str]:
    keys: List[str] = []
    for item in sorted(items, key=lambda x: float(x.get("value", 0.0)), reverse=True):
        if float(item.get("value", 0.0)) < threshold:
            continue
        key = str(item.get("key") or "")
        if key and key not in keys:
            keys.append(key)
    return keys


def fit_level_text(score: float) -> str:
    if score >= 80:
        return "высокий"
    if score >= 65:
        return "хороший"
    if score >= 50:
        return "умеренный"
    return "сдержанный"


def confidence_level_text(score: float) -> str:
    if score >= 80:
        return "высокая"
    if score >= 65:
        return "хорошая"
    if score >= 50:
        return "средняя"
    return "ограниченная"


def _value_map(items: List[dict]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for item in items or []:
        key = str(item.get("key") or "").strip()
        if key:
            out[key] = float(item.get("value", 0.0) or 0.0)
    return out


def _mean_existing(values: List[Optional[float]], default: float = 0.0) -> float:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return float(default)
    return sum(nums) / len(nums)


def _unique_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items or []:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _track_label(key: str) -> str:
    mapping = {
        "it_engineering": "ИТ / инженерный трек",
        "it_analytics": "ИТ / аналитический трек",
        "general_professional": "общий профессиональный трек",
        "coordination": "координационно-проектный трек",
        "needs_additional_assessment": "нужна дополнительная оценка",
    }
    return mapping.get(str(key or ""), "общий профессиональный трек")



def _signal_label(signal_key: str) -> str:
    return (
        EMP_AXIS_LABELS.get(signal_key)
        or COGNITIVE_LABELS.get(signal_key)
        or DERIVED_SIGNAL_LABELS.get(signal_key)
        or str(signal_key or "").replace("_", " ").strip()
    )


def _role_signal_value(signal_key: str, emp_axes: Dict[str, float], cog_axes: Dict[str, float], emp_radar_map: Dict[str, float], derived: Dict[str, float]) -> Optional[float]:
    if signal_key in emp_axes:
        return float(emp_axes.get(signal_key, 0.0))
    if signal_key in cog_axes:
        return float(cog_axes.get(signal_key, 0.0))
    if signal_key in emp_radar_map:
        return float(emp_radar_map.get(signal_key, 0.0))
    if signal_key.endswith("_radar"):
        radar_key = signal_key[:-6]
        if radar_key in emp_radar_map:
            return float(emp_radar_map.get(radar_key, 0.0))
    if signal_key in derived:
        return float(derived.get(signal_key, 0.0))
    return None


def _role_match_level(score: float) -> str:
    if score >= 78:
        return "высокое соответствие"
    if score >= 66:
        return "хорошее соответствие"
    if score >= 56:
        return "умеренное соответствие"
    return "предварительное соответствие"


def _build_role_recommendations(
    *,
    recommended_track: str,
    overall_professional_fit: float,
    it_fit: float,
    fit_confidence: float,
    it_fit_confidence: float,
    attention_pass_rate: float,
    social_desirability_index: float,
    consistency_index: float,
    response_stability_index: float,
    skip_tendency: float,
    persistence_index: float,
    speed_index: float,
    cognitive_accuracy: float,
    data_quality_index: float,
    emp_axes: Dict[str, float],
    cog_axes: Dict[str, float],
    emp_radar_map: Dict[str, float],
) -> dict:
    derived = {
        "overall_professional_fit": float(overall_professional_fit),
        "it_fit": float(it_fit),
        "fit_confidence": float(fit_confidence),
        "it_fit_confidence": float(it_fit_confidence),
        "attention_pass_rate": float(attention_pass_rate),
        "social_desirability_index": float(social_desirability_index),
        "consistency_index": float(consistency_index),
        "response_stability_index": float(response_stability_index),
        "skip_tendency": float(skip_tendency),
        "persistence_index": float(persistence_index),
        "speed_index": float(speed_index),
        "cognitive_accuracy": float(cognitive_accuracy),
        "data_quality_index": float(data_quality_index),
        "communication_radar": float(emp_radar_map.get("communication", 0.0)),
        "motivation_radar": float(emp_radar_map.get("motivation", 0.0)),
        "self_regulation_radar": float(emp_radar_map.get("self_regulation", 0.0)),
        "values_radar": float(emp_radar_map.get("values", 0.0)),
        "stability_radar": float(emp_radar_map.get("stability", 0.0)),
        "it_career_radar": float(emp_radar_map.get("it_career", 0.0)),
    }

    results: List[dict] = []
    for rule in ROLE_MATCH_LIBRARY:
        weighted_sum = 0.0
        weight_total = 0.0
        matched_signals: List[dict] = []
        weak_signals: List[dict] = []

        for signal in rule.get("signals", []):
            key = str(signal.get("key") or "")
            value = _role_signal_value(key, emp_axes, cog_axes, emp_radar_map, derived)
            if value is None:
                continue
            weight = float(signal.get("weight", 0.0) or 0.0)
            weighted_sum += weight * float(value)
            weight_total += weight
            entry = {"key": key, "label": _signal_label(key), "value": round(float(value), 1)}
            if float(value) >= 62:
                matched_signals.append(entry)
            elif signal.get("critical") and float(value) < 48:
                weak_signals.append(entry)

        base_score = (weighted_sum / weight_total) if weight_total > 0 else 0.0
        role_score = base_score

        if recommended_track and str(rule.get("track") or "") == str(recommended_track):
            role_score += 4.0
        elif recommended_track == "needs_additional_assessment":
            role_score -= 6.0

        minimums = rule.get("minimums") or {}
        for metric_key, threshold in minimums.items():
            value = _role_signal_value(str(metric_key), emp_axes, cog_axes, emp_radar_map, derived)
            if value is not None and float(value) < float(threshold):
                role_score -= (float(threshold) - float(value)) * 0.28

        if fit_confidence < 45:
            role_score -= (45.0 - fit_confidence) * 0.35
        if attention_pass_rate < 70 and rule.get("detail_sensitive"):
            role_score -= (70.0 - attention_pass_rate) * 0.18
        if consistency_index < 60:
            role_score -= (60.0 - consistency_index) * 0.10
        if skip_tendency > 15:
            role_score -= (skip_tendency - 15.0) * 0.12
        if social_desirability_index > 70:
            role_score -= (social_desirability_index - 70.0) * 0.08
        if data_quality_index < 55:
            role_score -= (55.0 - data_quality_index) * 0.12

        role_score = round(_clamp(role_score), 1)
        matched_signals = sorted(matched_signals, key=lambda x: (-float(x.get("value", 0.0)), str(x.get("label", ""))))[:3]
        weak_signals = sorted(weak_signals, key=lambda x: (float(x.get("value", 0.0)), str(x.get("label", ""))))[:2]

        reasons = [
            f"сильный сигнал по «{item.get('label')}» ({item.get('value')}/100)"
            for item in matched_signals
        ]
        cautions: List[str] = []
        if weak_signals:
            cautions.extend([f"проседает показатель «{item.get('label')}» ({item.get('value')}/100)" for item in weak_signals])
        if fit_confidence < 55:
            cautions.append("итог нужно перепроверить на дополнительном интервью или практическом кейсе")
        if social_desirability_index > 70:
            cautions.append("есть риск завышения самоописания из-за социальной желательности")
        if skip_tendency > 15:
            cautions.append("доля пропусков выше комфортной для прямой профрекомендации")
        if attention_pass_rate < 70 and rule.get("detail_sensitive"):
            cautions.append("для этой роли желательно лучше подтверждённое внимание к деталям")
        cautions = _unique_keep_order(cautions)[:4]

        results.append({
            "key": rule.get("key"),
            "label": rule.get("label"),
            "cluster": rule.get("cluster"),
            "track": rule.get("track"),
            "score": role_score,
            "matchLevel": _role_match_level(role_score),
            "summary": rule.get("summary") or "",
            "reasons": reasons,
            "cautions": cautions,
            "matchedSignals": matched_signals,
            "weakSignals": weak_signals,
        })

    results = sorted(results, key=lambda x: (-float(x.get("score", 0.0)), str(x.get("label", ""))))
    top_results = results[:3]

    primary = top_results[0] if top_results else None
    primary_label = "Нужна дополнительная профдиагностика"
    primary_key = "needs_additional_assessment"
    primary_cluster = "Дополнительная оценка"
    primary_score = 0.0
    if primary and float(primary.get("score", 0.0)) >= 58 and fit_confidence >= 42:
        primary_label = str(primary.get("label") or primary_label)
        primary_key = str(primary.get("key") or primary_key)
        primary_cluster = str(primary.get("cluster") or primary_cluster)
        primary_score = float(primary.get("score", 0.0))
    elif primary:
        primary_score = float(primary.get("score", 0.0))

    alternative_labels = [str(item.get("label") or "") for item in top_results[1:] if str(item.get("label") or "").strip()]
    if primary_label == "Нужна дополнительная профдиагностика":
        summary = "Текущих сигналов недостаточно для прямой рекомендации одной конкретной IT-роли: лучше провести дополнительный кейс или интервью и затем пересчитать соответствие профессиям."
    else:
        summary = f"Предварительно наиболее подходящая роль — {primary_label}. Далее стоит рассматривать также: {', '.join(alternative_labels) if alternative_labels else 'смежные роли внутри того же трека'}."

    return {
        "primaryRoleKey": primary_key,
        "primaryRoleLabel": primary_label,
        "primaryRoleCluster": primary_cluster,
        "primaryRoleScore": round(primary_score, 1),
        "alternativeRoles": alternative_labels[:3],
        "roles": top_results,
        "summary": summary,
    }

# ====================== Internship routing layer ======================

def _load_reference_track_profiles() -> Dict[str, dict]:
    """Загружает эталонные профили профессиональных стажировок из JSON-справочника."""
    try:
        raw = json.loads(REFERENCE_TRACK_PROFILES_PATH.read_text(encoding="utf-8"))
        tracks = raw.get("tracks") if isinstance(raw, dict) else None
        if isinstance(tracks, dict) and tracks:
            return tracks
    except Exception:
        traceback.print_exc()
    return {}


REFERENCE_TRACK_PROFILES = _load_reference_track_profiles()


ROUTING_LEGACY_TRACK_MAP = {
    "it_analytics": "data_bi_analytics",
    "it_engineering": "software_foundation",
    "coordination": "product_project_support",
    "general_professional": "adjacent_digital_track",
    "needs_additional_assessment": "adjacent_digital_track",
}


ROUTING_AXIS_LABELS = {
    "attention": "контроль внимания",
    "response_stability": "стабильность ответов",
    "planning": "планирование",
    "persistence": "настойчивость",
    "self_regulation": "саморегуляция",
    "data_interpretation": "интерпретация данных",
    "numerical_reasoning": "числовое мышление",
    "abstract_reasoning": "абстрактное мышление",
    "learning_value": "ценность обучения",
    "communication": "коммуникация",
    "responsibility": "ответственность",
    "empathy": "эмпатия",
    "verbal_reasoning": "вербальная логика",
    "observation": "наблюдательность",
    "structured_explanation": "структурность объяснения",
    "process_orientation": "процессная ориентация",
    "general_emp_fit": "общий профессиональный fit",
    "digital_readiness": "цифровая готовность",
    "process_discipline": "процессная дисциплина",
    "data_quality": "качество данных",
    "skip_tendency": "доля пропусков",
    "social_desirability": "социальная желательность",
    "fit_confidence": "надёжность вывода",
    "cognitive_accuracy": "когнитивная точность",
    "speed_index": "скорость решения",
    "it_orientation": "ИТ-ориентация",
}


def _routing_axis_label(axis_key: str) -> str:
    return (
        ROUTING_AXIS_LABELS.get(axis_key)
        or EMP_AXIS_LABELS.get(axis_key)
        or COGNITIVE_LABELS.get(axis_key)
        or DERIVED_SIGNAL_LABELS.get(axis_key)
        or str(axis_key or "").replace("_", " ")
    )


def _profile_value(*values: Optional[float], default: float = 0.0) -> float:
    """Возвращает первое осмысленное значение профиля, ограниченное диапазоном 0-100."""
    for value in values:
        if value is None:
            continue
        try:
            return _clamp(float(value))
        except Exception:
            continue
    return _clamp(default)


def _build_employee_routing_profile(
    *,
    emp_axes: Dict[str, float],
    cog_axes: Dict[str, float],
    emp_radar_map: Dict[str, float],
    overall_professional_fit: float,
    it_fit: float,
    fit_confidence: float,
    it_fit_confidence: float,
    attention_pass_rate: float,
    social_desirability_index: float,
    consistency_index: float,
    data_quality_index: float,
    response_stability_index: float,
    skip_tendency: float,
    persistence_index: float,
    speed_index: float,
    cognitive_accuracy: float,
    partial_density: float,
) -> Dict[str, float]:
    """Формирует единый цифровой профиль сотрудника для сравнения с эталонными треками."""
    communication = _profile_value(emp_axes.get("communication"), emp_radar_map.get("communication"))
    self_regulation = _profile_value(emp_axes.get("self_regulation"), emp_radar_map.get("self_regulation"))
    planning = _profile_value(cog_axes.get("planning"), emp_axes.get("planning"), self_regulation)
    learning_value = _profile_value(emp_axes.get("learning_value"), emp_axes.get("growth_orientation"), emp_radar_map.get("it_career"))
    responsibility = _profile_value(emp_axes.get("responsibility"), emp_radar_map.get("values"), emp_axes.get("ethical_orientation"))
    empathy = _profile_value(emp_axes.get("empathy"), communication)
    data_interpretation = _profile_value(cog_axes.get("data_interpretation"), cognitive_accuracy)
    numerical_reasoning = _profile_value(cog_axes.get("numerical_reasoning"), cognitive_accuracy)
    abstract_reasoning = _profile_value(cog_axes.get("abstract_reasoning"), cognitive_accuracy)
    verbal_reasoning = _profile_value(cog_axes.get("verbal_reasoning"), communication)

    return {
        "motivation": _profile_value(emp_radar_map.get("motivation"), emp_axes.get("achievement_drive"), learning_value),
        "communication": communication,
        "self_regulation": self_regulation,
        "values": _profile_value(emp_radar_map.get("values"), responsibility),
        "stability": _profile_value(emp_radar_map.get("stability"), response_stability_index),
        "it_orientation": _profile_value(emp_axes.get("it_orientation"), emp_radar_map.get("it_career"), it_fit),
        "learning_value": learning_value,
        "responsibility": responsibility,
        "empathy": empathy,
        "planning": planning,
        "abstract_reasoning": abstract_reasoning,
        "numerical_reasoning": numerical_reasoning,
        "data_interpretation": data_interpretation,
        "verbal_reasoning": verbal_reasoning,
        "observation": _profile_value(cog_axes.get("spatial_reasoning"), attention_pass_rate),
        "structured_explanation": _profile_value(verbal_reasoning * 0.55 + communication * 0.45),
        "process_orientation": _profile_value(planning * 0.55 + self_regulation * 0.45),
        "process_discipline": _profile_value(self_regulation * 0.45 + planning * 0.35 + response_stability_index * 0.20),
        "general_emp_fit": _profile_value(overall_professional_fit),
        "digital_readiness": _profile_value(it_fit),
        "fit_confidence": _profile_value(fit_confidence),
        "it_fit_confidence": _profile_value(it_fit_confidence),
        "attention": _profile_value(attention_pass_rate),
        "social_desirability": _profile_value(social_desirability_index),
        "consistency": _profile_value(consistency_index),
        "data_quality": _profile_value(data_quality_index),
        "response_stability": _profile_value(response_stability_index),
        "skip_tendency": _profile_value(skip_tendency),
        "persistence": _profile_value(persistence_index),
        "speed_index": _profile_value(speed_index),
        "cognitive_accuracy": _profile_value(cognitive_accuracy),
        "partial_density": _profile_value(partial_density),
    }


def _check_blocker_rule(employee_profile: Dict[str, float], rule: dict) -> bool:
    axis = str(rule.get("axis") or "")
    op = str(rule.get("operator") or "").lower()
    threshold = float(rule.get("threshold", 0.0) or 0.0)
    value = float(employee_profile.get(axis, 0.0) or 0.0)
    if op == "lt":
        return value < threshold
    if op == "lte":
        return value <= threshold
    if op == "gt":
        return value > threshold
    if op == "gte":
        return value >= threshold
    if op == "eq":
        return abs(value - threshold) < 1e-9
    return False


def _build_required_gaps(employee_profile: Dict[str, float], track_profile: dict) -> List[dict]:
    """Сравнивает профиль сотрудника с обязательными минимумами стажировочного трека."""
    gaps: List[dict] = []
    for axis_key, required_value in (track_profile.get("requiredMinima") or {}).items():
        required = float(required_value or 0.0)
        actual = float(employee_profile.get(str(axis_key), 0.0) or 0.0)
        gap = max(0.0, required - actual)
        if gap > 0.0:
            severity = "critical" if gap >= 15.0 else "development"
            gaps.append({
                "axis": str(axis_key),
                "label": _routing_axis_label(str(axis_key)),
                "actual": round(actual, 1),
                "required": round(required, 1),
                "gap": round(gap, 1),
                "severity": severity,
            })
    return gaps


def _calculate_track_match(employee_profile: Dict[str, float], track_key: str, track_profile: dict) -> dict:
    """Рассчитывает процент совпадения сотрудника с одним эталонным треком."""
    weighted_sum = 0.0
    weight_total = 0.0
    matched_axes: List[dict] = []
    weak_axes: List[dict] = []

    for axis_key, weight in (track_profile.get("trackAxisWeights") or {}).items():
        key = str(axis_key)
        value = float(employee_profile.get(key, 0.0) or 0.0)
        w = max(0.0, float(weight or 0.0))
        weighted_sum += value * w
        weight_total += w
        axis_row = {"axis": key, "label": _routing_axis_label(key), "value": round(value, 1), "weight": round(w, 2)}
        if value >= 65.0:
            matched_axes.append(axis_row)
        elif value <= 50.0:
            weak_axes.append(axis_row)

    base_score = weighted_sum / weight_total if weight_total > 0 else 0.0
    required_gaps = _build_required_gaps(employee_profile, track_profile)
    gap_penalty = sum(float(gap.get("gap", 0.0)) * (0.35 if gap.get("severity") == "critical" else 0.22) for gap in required_gaps)

    blocker_penalty = 0.0
    blocker_hits: List[dict] = []
    for rule in track_profile.get("blockerRules") or []:
        if not isinstance(rule, dict):
            continue
        if _check_blocker_rule(employee_profile, rule):
            penalty = float(rule.get("penalty", 0.0) or 0.0)
            blocker_penalty += penalty
            axis = str(rule.get("axis") or "")
            blocker_hits.append({
                "axis": axis,
                "label": _routing_axis_label(axis),
                "actual": round(float(employee_profile.get(axis, 0.0) or 0.0), 1),
                "threshold": float(rule.get("threshold", 0.0) or 0.0),
                "penalty": round(penalty, 1),
                "severity": rule.get("severity") or "high",
                "message": rule.get("message") or "обнаружен стоп-фактор маршрутизации",
            })

    quality_penalty = 0.0
    if employee_profile.get("data_quality", 0.0) < 55.0:
        quality_penalty += (55.0 - employee_profile.get("data_quality", 0.0)) * 0.12
    if employee_profile.get("social_desirability", 0.0) > 75.0:
        quality_penalty += (employee_profile.get("social_desirability", 0.0) - 75.0) * 0.10
    if employee_profile.get("skip_tendency", 0.0) > 20.0:
        quality_penalty += (employee_profile.get("skip_tendency", 0.0) - 20.0) * 0.10

    final_score = _clamp(base_score - gap_penalty - blocker_penalty - quality_penalty)

    return {
        "trackKey": track_key,
        "trackLabel": track_profile.get("label") or track_key,
        "score": round(final_score, 1),
        "baseScore": round(_clamp(base_score), 1),
        "gapPenalty": round(gap_penalty, 1),
        "blockerPenalty": round(blocker_penalty, 1),
        "qualityPenalty": round(quality_penalty, 1),
        "requiredGaps": required_gaps,
        "blockerHits": blocker_hits,
        "matchedAxes": sorted(matched_axes, key=lambda item: -float(item.get("value", 0.0)))[:4],
        "weakAxes": sorted(weak_axes, key=lambda item: float(item.get("value", 0.0)))[:4],
    }


def _calculate_learning_agility(employee_profile: Dict[str, float]) -> float:
    """Оценивает способность сотрудника осваивать новое и удерживать усилие."""
    return round(_clamp(
        0.32 * employee_profile.get("learning_value", 0.0) +
        0.18 * employee_profile.get("self_regulation", 0.0) +
        0.15 * employee_profile.get("persistence", 0.0) +
        0.15 * employee_profile.get("response_stability", 0.0) +
        0.10 * employee_profile.get("communication", 0.0) +
        0.10 * (100.0 - employee_profile.get("skip_tendency", 0.0))
    ), 1)


def _calculate_it_transition_potential(employee_profile: Dict[str, float]) -> float:
    """Оценивает потенциал перехода сотрудника в ИТ-обучение и стажировочную среду."""
    learning_agility = _calculate_learning_agility(employee_profile)
    it_cognitive_fit = _mean_existing([
        employee_profile.get("abstract_reasoning"),
        employee_profile.get("numerical_reasoning"),
        employee_profile.get("planning"),
        employee_profile.get("data_interpretation"),
    ], default=employee_profile.get("cognitive_accuracy", 50.0))
    return round(_clamp(
        0.30 * employee_profile.get("digital_readiness", 0.0) +
        0.25 * it_cognitive_fit +
        0.20 * learning_agility +
        0.15 * employee_profile.get("general_emp_fit", 0.0) +
        0.10 * employee_profile.get("data_quality", 0.0)
    ), 1)


def _build_gap_analysis(best_match: dict, employee_profile: Dict[str, float]) -> dict:
    """Разделяет разрывы на критичные ограничения и развивающие дефициты."""
    critical_gaps: List[dict] = []
    development_gaps: List[dict] = []

    for gap in best_match.get("requiredGaps") or []:
        if str(gap.get("severity")) == "critical":
            critical_gaps.append(gap)
        else:
            development_gaps.append(gap)

    for blocker in best_match.get("blockerHits") or []:
        row = {
            "axis": blocker.get("axis"),
            "label": blocker.get("label"),
            "actual": blocker.get("actual"),
            "required": blocker.get("threshold"),
            "gap": None,
            "severity": blocker.get("severity") or "high",
            "message": blocker.get("message"),
        }
        if row["severity"] == "critical":
            critical_gaps.append(row)
        else:
            development_gaps.append(row)

    if employee_profile.get("data_quality", 0.0) < 45.0:
        critical_gaps.append({
            "axis": "data_quality",
            "label": _routing_axis_label("data_quality"),
            "actual": round(employee_profile.get("data_quality", 0.0), 1),
            "required": 45.0,
            "gap": round(45.0 - employee_profile.get("data_quality", 0.0), 1),
            "severity": "critical",
            "message": "низкое качество данных требует дооценки перед направлением",
        })

    # Удаляем дубли по оси и типу проблемы.
    def dedupe(items: List[dict]) -> List[dict]:
        seen = set()
        out = []
        for item in items:
            key = (str(item.get("axis")), str(item.get("message") or item.get("severity") or ""))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    return {
        "criticalGaps": dedupe(critical_gaps)[:6],
        "developmentGaps": dedupe(development_gaps)[:8],
    }


def _build_fast_growth_zones(employee_profile: Dict[str, float], best_match: dict) -> List[dict]:
    """Находит области, где дефицит можно закрыть краткой подготовкой."""
    zones: List[dict] = []
    learning_agility = _calculate_learning_agility(employee_profile)
    partial_density = employee_profile.get("partial_density", 0.0)

    for gap in best_match.get("requiredGaps") or []:
        gap_value = float(gap.get("gap", 0.0) or 0.0)
        axis = str(gap.get("axis") or "")
        actual = float(gap.get("actual", 0.0) or 0.0)
        if 0.0 < gap_value <= 18.0 and (learning_agility >= 60.0 or partial_density >= 35.0):
            zones.append({
                "axis": axis,
                "label": _routing_axis_label(axis),
                "actual": round(actual, 1),
                "gap": round(gap_value, 1),
                "reason": "разрыв умеренный, а профиль обучаемости или частичных успехов позволяет ожидать быстрый прирост",
            })

    if not zones and learning_agility >= 65.0:
        zones.append({
            "axis": "learning_agility",
            "label": "обучаемость и удержание усилия",
            "actual": learning_agility,
            "gap": 0.0,
            "reason": "высокая обучаемость может ускорить адаптацию на выбранной стажировке",
        })

    return zones[:5]


def _readiness_level(score: float) -> str:
    if score >= 75.0:
        return "high"
    if score >= 60.0:
        return "medium"
    return "low"


def _readiness_label(level: str) -> str:
    return {"high": "высокая", "medium": "средняя", "low": "низкая"}.get(level, "требует проверки")


def _decision_label(decision: str) -> str:
    return {
        "send_now": "Направить сейчас",
        "send_after_preparation": "Направить после краткой подготовки",
        "redirect_to_alternative_track": "Перенаправить в альтернативный трек",
        "defer": "Отложить решение / назначить дооценку",
    }.get(decision, "Требуется экспертная проверка")


def _resolve_internship_decision(
    *,
    internship_readiness: float,
    recommendation_confidence: float,
    critical_gaps: List[dict],
    best_track: dict,
    legacy_track_key: Optional[str],
    track_matches: Dict[str, dict],
    employee_profile: Dict[str, float],
) -> str:
    """Определяет HR-решение по готовности, качеству данных и разрывам профиля."""
    if employee_profile.get("data_quality", 0.0) < 45.0 or recommendation_confidence < 45.0:
        return "defer"

    if len(critical_gaps) >= 2 and internship_readiness < 72.0:
        return "defer"

    if legacy_track_key and legacy_track_key in track_matches and legacy_track_key != best_track.get("trackKey"):
        legacy_score = float(track_matches[legacy_track_key].get("score", 0.0) or 0.0)
        if float(best_track.get("score", 0.0) or 0.0) - legacy_score >= 5.0 and internship_readiness >= 55.0:
            return "redirect_to_alternative_track"

    if internship_readiness >= 75.0 and recommendation_confidence >= 60.0 and not critical_gaps:
        return "send_now"

    if internship_readiness >= 60.0 and len(critical_gaps) <= 1:
        return "send_after_preparation"

    return "defer"


def _build_internship_routing(
    *,
    emp_axes: Dict[str, float],
    cog_axes: Dict[str, float],
    emp_radar_map: Dict[str, float],
    overall_professional_fit: float,
    it_fit: float,
    fit_confidence: float,
    it_fit_confidence: float,
    attention_pass_rate: float,
    social_desirability_index: float,
    consistency_index: float,
    data_quality_index: float,
    response_stability_index: float,
    skip_tendency: float,
    persistence_index: float,
    speed_index: float,
    cognitive_accuracy: float,
    partial_density: float,
    recommended_track: str,
) -> dict:
    """Основная функция маршрутизации сотрудника на профессиональную ИТ-стажировку."""
    employee_profile = _build_employee_routing_profile(
        emp_axes=emp_axes,
        cog_axes=cog_axes,
        emp_radar_map=emp_radar_map,
        overall_professional_fit=overall_professional_fit,
        it_fit=it_fit,
        fit_confidence=fit_confidence,
        it_fit_confidence=it_fit_confidence,
        attention_pass_rate=attention_pass_rate,
        social_desirability_index=social_desirability_index,
        consistency_index=consistency_index,
        data_quality_index=data_quality_index,
        response_stability_index=response_stability_index,
        skip_tendency=skip_tendency,
        persistence_index=persistence_index,
        speed_index=speed_index,
        cognitive_accuracy=cognitive_accuracy,
        partial_density=partial_density,
    )

    track_matches: Dict[str, dict] = {}
    for track_key, track_profile in (REFERENCE_TRACK_PROFILES or {}).items():
        if isinstance(track_profile, dict):
            track_matches[track_key] = _calculate_track_match(employee_profile, track_key, track_profile)

    if not track_matches:
        return {
            "enabled": False,
            "error": "reference_track_profiles_not_loaded",
            "employeeProfile": employee_profile,
            "primaryInternshipTrack": None,
            "primaryInternshipTrackLabel": "Справочник треков не загружен",
            "alternativeInternshipTracks": [],
            "trackMatchScores": {},
            "criticalGaps": [],
            "developmentGaps": [],
            "fastGrowthZones": [],
            "internshipDecision": "defer",
            "internshipDecisionLabel": _decision_label("defer"),
            "internshipReadinessScore": 0.0,
            "internshipReadinessLevel": "low",
            "internshipReadinessLabel": _readiness_label("low"),
            "recommendationConfidence": 0.0,
        }

    sorted_tracks = sorted(track_matches.values(), key=lambda item: (-float(item.get("score", 0.0)), str(item.get("trackLabel", ""))))
    best_track = sorted_tracks[0]
    best_track_key = str(best_track.get("trackKey") or "")
    best_track_profile = REFERENCE_TRACK_PROFILES.get(best_track_key, {}) or {}

    gap_analysis = _build_gap_analysis(best_track, employee_profile)
    fast_growth_zones = _build_fast_growth_zones(employee_profile, best_track)
    learning_agility = _calculate_learning_agility(employee_profile)
    it_transition_potential = _calculate_it_transition_potential(employee_profile)

    internship_readiness = round(_clamp(
        0.35 * float(best_track.get("score", 0.0) or 0.0) +
        0.25 * it_transition_potential +
        0.15 * employee_profile.get("general_emp_fit", 0.0) +
        0.15 * employee_profile.get("data_quality", 0.0) +
        0.10 * employee_profile.get("persistence", 0.0)
    ), 1)

    track_separation = 0.0
    if len(sorted_tracks) >= 2:
        track_separation = max(0.0, float(sorted_tracks[0].get("score", 0.0)) - float(sorted_tracks[1].get("score", 0.0)))
    profile_coverage = 100.0
    recommendation_confidence = round(_clamp(
        0.45 * employee_profile.get("data_quality", 0.0) +
        0.20 * fit_confidence +
        0.15 * profile_coverage +
        0.10 * (100.0 - employee_profile.get("social_desirability", 0.0)) +
        0.10 * min(100.0, track_separation * 10.0)
    ), 1)

    legacy_track_key = ROUTING_LEGACY_TRACK_MAP.get(str(recommended_track or ""))
    decision = _resolve_internship_decision(
        internship_readiness=internship_readiness,
        recommendation_confidence=recommendation_confidence,
        critical_gaps=gap_analysis.get("criticalGaps", []),
        best_track=best_track,
        legacy_track_key=legacy_track_key,
        track_matches=track_matches,
        employee_profile=employee_profile,
    )
    readiness_level = _readiness_level(internship_readiness)

    alternative_tracks = [
        {
            "trackKey": item.get("trackKey"),
            "trackLabel": item.get("trackLabel"),
            "score": item.get("score"),
            "baseScore": item.get("baseScore"),
        }
        for item in sorted_tracks[1:3]
    ]

    return {
        "enabled": True,
        "referenceVersion": "emopot_internship_routing_v1",
        "employeeProfile": employee_profile,
        "primaryInternshipTrack": best_track_key,
        "primaryInternshipTrackLabel": best_track.get("trackLabel"),
        "initialTrackFromLegacyFit": legacy_track_key,
        "alternativeInternshipTracks": alternative_tracks,
        "trackMatchScores": {
            str(item.get("trackKey")): {
                "label": item.get("trackLabel"),
                "score": item.get("score"),
                "baseScore": item.get("baseScore"),
                "gapPenalty": item.get("gapPenalty"),
                "blockerPenalty": item.get("blockerPenalty"),
                "qualityPenalty": item.get("qualityPenalty"),
                "matchedAxes": item.get("matchedAxes", []),
                "weakAxes": item.get("weakAxes", []),
            }
            for item in sorted_tracks
        },
        "criticalGaps": gap_analysis.get("criticalGaps", []),
        "developmentGaps": gap_analysis.get("developmentGaps", []),
        "fastGrowthZones": fast_growth_zones,
        "learningAgilityIndex": learning_agility,
        "itTransitionPotential": it_transition_potential,
        "internshipDecision": decision,
        "internshipDecisionLabel": _decision_label(decision),
        "internshipReadinessScore": internship_readiness,
        "internshipReadinessLevel": readiness_level,
        "internshipReadinessLabel": _readiness_label(readiness_level),
        "recommendationConfidence": recommendation_confidence,
        "recommendedInternshipFormat": best_track_profile.get("format") or "Формат стажировки требует экспертного уточнения.",
        "mentorNeedLevel": best_track_profile.get("mentorNeedLevel") or "medium",
        "adaptationNeedLevel": best_track_profile.get("adaptationNeedLevel") or "medium",
        "routingSummary": (
            f"Основной трек: {best_track.get('trackLabel')}. "
            f"Готовность: {internship_readiness:.1f}/100, уверенность: {recommendation_confidence:.1f}/100. "
            f"HR-решение: {_decision_label(decision).lower()}."
        ),
    }



def _build_player_report(player_row: dict, answers: List[dict], meta_by_key: Dict[Tuple[int, str], dict], rank_map: Dict[str, int]) -> dict:
    player_id = player_row.get("playerId")
    player_name = player_row.get("name")

    emp_weighted_sum: Dict[str, float] = defaultdict(float)
    emp_weight_total: Dict[str, float] = defaultdict(float)
    cog_weighted_sum: Dict[str, float] = defaultdict(float)
    cog_weight_total: Dict[str, float] = defaultdict(float)

    quality_attention: List[int] = []
    quality_desirability: List[float] = []
    quality_consistency_pairs: Dict[str, List[float]] = defaultdict(list)
    hesitation_values: List[float] = []
    change_counts: List[int] = []
    skip_flags: List[int] = []
    partial_values: List[float] = []
    cognitive_correct = 0
    cognitive_total = 0
    cognitive_time: List[float] = []
    speed_components: List[float] = []
    history_rows: List[dict] = []
    evidence_blocks: set = set()
    evidence_items = 0
    it_signal_items = 0

    for ans in sorted(answers, key=lambda x: (x.get("round") or 0, x.get("playerName") or "")):
        meta = meta_by_key.get((ans.get("round"), ans.get("questionId")), {})
        response_model = meta.get("responseModel") or ("survey" if str(ans.get("block") or "").startswith("emp_") else "quiz")
        control = is_control_task(meta or ans)
        scored_value = ans.get("scoredValue")

        hesitation_values.append(_safe_float(ans.get("hesitationIndex"), 0.0))
        change_counts.append(_safe_int(ans.get("changeCount"), 0))
        skip_flags.append(1 if str(ans.get("finalAnswerState") or "") in {"skipped", "timed_out", "disconnect_before_submit", "auto_closed"} else 0)
        partial_values.append(_safe_float(ans.get("partialCredit"), 0.0))

        if response_model == "survey":
            scale_key = meta.get("scaleKey") or {}
            if control:
                ctype = str(meta.get("controlType") or meta.get("instrument") or ans.get("instrument") or "control").lower()
                if "attention" in ctype or "вним" in ctype:
                    quality_attention.append(1 if ans.get("isCorrect") else 0)
                if "social_desirability" in ctype or "desirability" in ctype:
                    desirability_pct = _normalize_likert_to_pct(scored_value)
                    if desirability_pct is not None:
                        quality_desirability.append(desirability_pct)
                if "consistency" in ctype or "соглас" in ctype:
                    consistency_pct = _normalize_likert_to_pct(scored_value)
                    if consistency_pct is not None:
                        pair_key = str(meta.get("consistencyPair") or meta.get("instrument") or "consistency")
                        quality_consistency_pairs[pair_key].append(consistency_pct)
            else:
                evidence_items += 1
                evidence_blocks.add(str(meta.get("block") or ans.get("block") or "survey"))
                for axis_key, weight in (scale_key or {}).items():
                    pct = _normalize_likert_to_pct(scored_value)
                    if pct is None:
                        continue
                    axis_key = str(axis_key)
                    w = float(weight)
                    emp_weighted_sum[axis_key] += w * pct
                    emp_weight_total[axis_key] += w
                    if axis_key in {"professional_orientation", "innovation_orientation", "it_orientation", "learning_value"}:
                        it_signal_items += 1
        else:
            evidence_items += 1
            evidence_blocks.add(str(meta.get("block") or ans.get("block") or meta.get("category") or "quiz"))
            cognitive_total += 1
            if ans.get("isCorrect"):
                cognitive_correct += 1
            if ans.get("timeMs") is not None:
                cognitive_time.append(float(ans.get("timeMs")))
            time_ref = meta.get("timeRef") or 0
            if time_ref and ans.get("timeMs"):
                speed_components.append(_clamp((float(time_ref) / max(float(ans.get("timeMs")) / 1000.0, 1.0)) * 100.0))
            axis_weights = meta.get("cardAxisWeights") or CATEGORY_FALLBACK_CARD_AXES.get(meta.get("category"), {})
            perf = _card_score(ans, meta)
            for axis_key, weight in (axis_weights or {}).items():
                axis_key = str(axis_key)
                w = float(weight)
                cog_weighted_sum[axis_key] += w * perf
                cog_weight_total[axis_key] += w
                if axis_key in {"numerical_reasoning", "abstract_reasoning", "planning", "data_interpretation"}:
                    it_signal_items += 1

        history_rows.append(
            {
                "round": ans.get("round"),
                "category": meta.get("category") or ans.get("category"),
                "block": meta.get("block") or ans.get("block"),
                "instrument": meta.get("instrument") or ans.get("instrument"),
                "prompt": meta.get("prompt") or "",
                "questionShort": _question_short(meta),
                "answer": ans.get("text") if ans.get("text") else ans.get("choice"),
                "isCorrect": ans.get("isCorrect"),
                "awarded": ans.get("awarded") or 0,
                "timeMs": ans.get("timeMs"),
                "responseModel": response_model,
                "partialCredit": ans.get("partialCredit") or 0,
                "statusText": (
                    "получен" if response_model == "survey" else (
                        "верно" if ans.get("isCorrect") else (
                            "почти верно" if _safe_float(ans.get("partialCredit"), 0.0) >= 0.75 else (
                                "частично верно" if _safe_float(ans.get("partialCredit"), 0.0) >= 0.5 else (
                                    "слабое частичное решение" if _safe_float(ans.get("partialCredit"), 0.0) > 0 else "неверно"
                                )
                            )
                        )
                    )
                ),
            }
        )

    emp_axes = finalize_weighted_axes(emp_weighted_sum, emp_weight_total)
    cog_axes = finalize_weighted_axes(cog_weighted_sum, cog_weight_total)

    emp_radar = []
    for radar_key, (label, axis_keys, invert_keys) in EMP_RADAR_GROUPS.items():
        vals = []
        for axis_key in axis_keys:
            if axis_key not in emp_axes:
                continue
            axis_val = emp_axes[axis_key]
            if axis_key in invert_keys:
                axis_val = 100.0 - axis_val
            vals.append(axis_val)
        emp_radar.append({"key": radar_key, "label": label, "value": round(_mean(vals, default=0.0), 1)})

    cognitive_radar = []
    for axis_key, label in COGNITIVE_LABELS.items():
        if axis_key in cog_axes:
            cognitive_radar.append({"key": axis_key, "label": label, "value": round(cog_axes[axis_key], 1)})

    emp_radar_map = _value_map(emp_radar)

    cognitive_accuracy = round((cognitive_correct / cognitive_total) * 100.0, 1) if cognitive_total else 0.0
    avg_cognitive_time = round(_mean(cognitive_time, default=0.0), 1) if cognitive_time else 0.0
    speed_index = round(_mean(speed_components, default=0.0), 1) if speed_components else 0.0
    attention_pass_rate = round((sum(quality_attention) / len(quality_attention)) * 100.0, 1) if quality_attention else 100.0
    social_desirability_index = round(_mean(quality_desirability, default=25.0), 1) if quality_desirability else 25.0
    consistency_scores: List[float] = []
    for pair_values in quality_consistency_pairs.values():
        if len(pair_values) >= 2:
            spread = max(pair_values) - min(pair_values)
            consistency_scores.append(max(0.0, 100.0 - spread))
        elif len(pair_values) == 1:
            consistency_scores.append(pair_values[0])
    consistency_index = round(_mean(consistency_scores, default=75.0), 1) if consistency_scores else 75.0
    behavioral_hesitation = round(_mean(hesitation_values, default=0.0), 1)
    response_stability_index = round(_clamp(100.0 - behavioral_hesitation), 1)
    skip_tendency = round(_mean(skip_flags, default=0.0) * 100.0, 1) if skip_flags else 0.0
    persistence_index = round(_clamp(35.0 + _mean(change_counts, default=0.0) * 8.0 + _mean(partial_values, default=0.0) * 25.0 - skip_tendency * 0.15), 1)
    partial_density = round(_clamp(_mean(partial_values, default=0.0) * 100.0), 1)
    data_quality_index = round(_clamp(0.35 * attention_pass_rate + 0.20 * (100.0 - social_desirability_index) + 0.15 * response_stability_index + 0.15 * (100.0 - skip_tendency) + 0.15 * consistency_index), 1)

    general_emp_fit = _mean_existing(
        [
            emp_radar_map.get("motivation"),
            emp_radar_map.get("communication"),
            emp_radar_map.get("self_regulation"),
            emp_radar_map.get("values"),
            emp_radar_map.get("stability"),
        ],
        default=55.0,
    )
    cognitive_core = _mean([item["value"] for item in cognitive_radar], default=cognitive_accuracy if cognitive_total else 50.0)
    it_orientation_fit = _mean_existing(
        [
            emp_axes.get("professional_orientation"),
            emp_axes.get("innovation_orientation"),
            emp_axes.get("it_orientation"),
            emp_axes.get("learning_value"),
        ],
        default=50.0,
    )
    it_cognitive_fit = _mean_existing(
        [
            cog_axes.get("abstract_reasoning"),
            cog_axes.get("numerical_reasoning"),
            cog_axes.get("planning"),
            cog_axes.get("data_interpretation"),
        ],
        default=cognitive_core,
    )
    motivation_fit = emp_radar_map.get("motivation", 55.0)

    overall_professional_fit = round(_clamp(0.50 * general_emp_fit + 0.22 * cognitive_core + 0.18 * data_quality_index + 0.10 * persistence_index), 1)
    it_fit = round(_clamp(0.35 * it_orientation_fit + 0.22 * it_cognitive_fit + 0.18 * overall_professional_fit + 0.15 * motivation_fit + 0.10 * data_quality_index), 1)
    overall_potential = overall_professional_fit

    evidence_ratio = min(1.0, evidence_items / 12.0) if evidence_items else 0.0
    block_coverage_ratio = min(1.0, len(evidence_blocks) / 6.0) if evidence_blocks else 0.0
    it_signal_ratio = min(1.0, it_signal_items / 6.0) if it_signal_items else 0.0
    fit_confidence = round(_clamp(0.50 * data_quality_index + 0.20 * (evidence_ratio * 100.0) + 0.15 * (block_coverage_ratio * 100.0) + 0.15 * (100.0 - social_desirability_index)), 1)
    it_fit_confidence = round(_clamp(0.70 * fit_confidence + 0.30 * (it_signal_ratio * 100.0)), 1)

    general_fit_level = fit_level_text(overall_professional_fit)
    it_fit_level = fit_level_text(it_fit)
    confidence_level = confidence_level_text(fit_confidence)

    general_fit_tags = []
    for item in sorted(emp_radar + cognitive_radar, key=lambda x: x.get("value", 0), reverse=True):
        if item.get("value", 0) >= 60 and item.get("key") != "it_career":
            general_fit_tags.append(item.get("label"))
    if speed_index >= 75:
        general_fit_tags.append("Хороший темп")
    if attention_pass_rate >= 80:
        general_fit_tags.append("Надёжные данные")
    general_fit_tags = _unique_keep_order(general_fit_tags)[:5] or ["Есть база для развития"]

    it_fit_tags = []
    if it_orientation_fit >= 70:
        it_fit_tags.append("Высокий интерес к цифровым и ИТ-задачам")
    elif it_orientation_fit >= 55:
        it_fit_tags.append("Умеренный интерес к цифровой среде")
    if it_cognitive_fit >= 70:
        it_fit_tags.append("Подходит для задач с логикой, данными и структурированием")
    elif it_cognitive_fit >= 55:
        it_fit_tags.append("Есть рабочая база для ИТ-обучения")
    if cog_axes.get("data_interpretation", 0.0) >= 65:
        it_fit_tags.append("Есть потенциал для аналитических задач")
    if cog_axes.get("planning", 0.0) >= 65:
        it_fit_tags.append("Умеет удерживать многошаговые решения")
    it_fit_tags = _unique_keep_order(it_fit_tags)[:4]

    fit_tags = general_fit_tags

    strength_candidates = [item["label"] for item in sorted(emp_radar + cognitive_radar, key=lambda x: x.get("value", 0), reverse=True) if item.get("value", 0) >= 65]
    strengths = strength_candidates[:4] or ["Профиль лучше всего раскрывается через реальные кейсы, задачи с дедлайном и понятный контекст работы"]

    growth_candidates = [item["label"] for item in sorted(emp_radar + cognitive_radar, key=lambda x: x.get("value", 0)) if item.get("value", 0) <= 45]
    growth_zones = growth_candidates[:4] or ["Критических провалов не выявлено; дальнейшее уточнение лучше проводить на практических заданиях"]

    risk_flags = []
    if attention_pass_rate < 70:
        risk_flags.append("Снижен контроль внимания: результаты лучше подтверждать дополнительным этапом оценки")
    if social_desirability_index > 70:
        risk_flags.append("Есть склонность к социально желательным ответам")
    if skip_tendency > 20:
        risk_flags.append("Повышенная доля пропусков или незавершённых ответов")
    if consistency_index < 55:
        risk_flags.append("Есть несогласованность между контрольными утверждениями")
    if fit_confidence < 55:
        risk_flags.append("Надёжность вывода ограничена объёмом или качеством данных")
    if overall_professional_fit >= 65 and it_fit < 50:
        risk_flags.append("Общий профессиональный fit выше, чем IT-fit: лучше не делать автоматический вывод о пригодности именно к ИТ")
    if overall_professional_fit < 50 and it_fit >= 60:
        risk_flags.append("Интерес к ИТ выше общего рабочего fit: нужен дополнительный кейс на дисциплину, устойчивость и командное взаимодействие")
    risk_flags = _unique_keep_order(risk_flags)

    top_emp = top_labels([item for item in emp_radar if item.get("key") != "it_career"], 2)
    top_cog = top_labels(cognitive_radar, 2)
    top_emp_keys = _top_axis_keys(emp_radar)
    top_cog_keys = _top_axis_keys(cognitive_radar)

    recommendations = []
    for axis_key in top_emp_keys + top_cog_keys:
        if axis_key in RECOMMENDATION_LIBRARY:
            text = RECOMMENDATION_LIBRARY[axis_key]
            if text not in recommendations:
                recommendations.append(text)
        if len(recommendations) >= 3:
            break
    if attention_pass_rate < 70:
        recommendations.append("Добавить короткий повторный этап с контролем внимания или второй кейс, чтобы повысить надёжность финального вывода.")
    if social_desirability_index > 70:
        recommendations.append("Полезно провести короткое интервью по поведенческим примерам, чтобы снизить эффект социально желательных ответов.")
    if consistency_index < 55:
        recommendations.append("Добавить короткий повторный этап или интервью с уточнением хода рассуждений: часть контрольных ответов выглядит несогласованной.")
    if overall_professional_fit >= 65 and it_fit < 55:
        recommendations.append("Рассматривать кандидата шире ИТ-контура: подойдут общие стажировочные, координационные или операционные роли, где цифровые навыки не являются единственным критерием.")
    if it_fit >= 70 and it_fit_confidence >= 60:
        recommendations.append("Есть основания выводить кандидата на следующий этап по ИТ-направлению: практический кейс, мини-задача или техническое интервью начального уровня.")
    if not recommendations:
        recommendations.append("Следующий шаг — практическая задача и короткий разбор хода решения: это даст более экспертный и надёжный итог для HR.")
    recommendations = _unique_keep_order(recommendations)[:4]

    recommended_track = "general_professional"
    if fit_confidence < 45:
        recommended_track = "needs_additional_assessment"
    elif it_fit >= 72 and it_fit_confidence >= 60:
        if cog_axes.get("data_interpretation", 0.0) >= 68 or cog_axes.get("numerical_reasoning", 0.0) >= 68:
            recommended_track = "it_analytics"
        else:
            recommended_track = "it_engineering"
    elif overall_professional_fit >= 68 and emp_radar_map.get("communication", 0.0) >= 65 and emp_radar_map.get("self_regulation", 0.0) >= 60:
        recommended_track = "coordination"
    elif overall_professional_fit >= 55:
        recommended_track = "general_professional"
    else:
        recommended_track = "needs_additional_assessment"

    expert_verdict = {
        "generalFitLevel": general_fit_level,
        "itFitLevel": it_fit_level,
        "fitConfidenceLevel": confidence_level,
        "recommendedTrack": recommended_track,
        "recommendedTrackLabel": _track_label(recommended_track),
    }

    role_recommendation = _build_role_recommendations(
        recommended_track=recommended_track,
        overall_professional_fit=overall_professional_fit,
        it_fit=it_fit,
        fit_confidence=fit_confidence,
        it_fit_confidence=it_fit_confidence,
        attention_pass_rate=attention_pass_rate,
        social_desirability_index=social_desirability_index,
        consistency_index=consistency_index,
        response_stability_index=response_stability_index,
        skip_tendency=skip_tendency,
        persistence_index=persistence_index,
        speed_index=speed_index,
        cognitive_accuracy=cognitive_accuracy,
        data_quality_index=data_quality_index,
        emp_axes=emp_axes,
        cog_axes=cog_axes,
        emp_radar_map=emp_radar_map,
    )

    internship_routing = _build_internship_routing(
        emp_axes=emp_axes,
        cog_axes=cog_axes,
        emp_radar_map=emp_radar_map,
        overall_professional_fit=overall_professional_fit,
        it_fit=it_fit,
        fit_confidence=fit_confidence,
        it_fit_confidence=it_fit_confidence,
        attention_pass_rate=attention_pass_rate,
        social_desirability_index=social_desirability_index,
        consistency_index=consistency_index,
        data_quality_index=data_quality_index,
        response_stability_index=response_stability_index,
        skip_tendency=skip_tendency,
        persistence_index=persistence_index,
        speed_index=speed_index,
        cognitive_accuracy=cognitive_accuracy,
        partial_density=partial_density,
        recommended_track=recommended_track,
    )

    summary_text = (
        f"{player_name} показал(а) {general_fit_level} общий профессиональный fit ({overall_professional_fit:.1f}/100) "
        f"и {it_fit_level} IT-fit ({it_fit:.1f}/100). "
        f"В общем профиле сильнее всего проявлены {', '.join(top_emp) if top_emp else 'рабочая мотивация и базовые поведенческие компетенции'}, "
        f"а среди когнитивных качеств — {', '.join(top_cog) if top_cog else 'базовые навыки решения задач'}. "
        f"Надёжность вывода — {confidence_level} ({fit_confidence:.1f}/100). "
        f"Рекомендуемый трек стажировки: {internship_routing.get('primaryInternshipTrackLabel') or _track_label(recommended_track)}. "
        f"HR-решение: {internship_routing.get('internshipDecisionLabel', 'требуется экспертная проверка')}. "
        f"Предварительно наиболее подходящая роль: {role_recommendation.get('primaryRoleLabel', 'Нужна дополнительная профдиагностика')}."
    )

    badges = build_player_badges(rank_map.get(player_id), overall_professional_fit, cognitive_accuracy, speed_index, attention_pass_rate)
    if overall_professional_fit >= 75:
        badges.append("🧩 Сильный общий fit")
    if it_fit >= 75:
        badges.append("💻 Сильный IT-fit")
    badges = _unique_keep_order(badges)

    return {
        "playerId": player_id,
        "playerName": player_name,
        "score": player_row.get("score", 0),
        "rank": rank_map.get(player_id),
        "overallPotential": overall_potential,
        "overallProfessionalFit": overall_professional_fit,
        "itFit": it_fit,
        "fitConfidence": fit_confidence,
        "itFitConfidence": it_fit_confidence,
        "summaryText": summary_text,
        "fitTags": fit_tags,
        "generalFitTags": general_fit_tags,
        "itFitTags": it_fit_tags,
        "badges": badges,
        "strengths": strengths,
        "growthZones": growth_zones,
        "recommendations": recommendations,
        "riskFlags": risk_flags,
        "recommendedTrack": recommended_track,
        "recommendedTrackLabel": _track_label(recommended_track),
        "internshipRouting": internship_routing,
        "primaryInternshipTrack": internship_routing.get("primaryInternshipTrack"),
        "primaryInternshipTrackLabel": internship_routing.get("primaryInternshipTrackLabel"),
        "alternativeInternshipTracks": internship_routing.get("alternativeInternshipTracks", []),
        "trackMatchScores": internship_routing.get("trackMatchScores", {}),
        "criticalGaps": internship_routing.get("criticalGaps", []),
        "developmentGaps": internship_routing.get("developmentGaps", []),
        "fastGrowthZones": internship_routing.get("fastGrowthZones", []),
        "internshipDecision": internship_routing.get("internshipDecision"),
        "internshipDecisionLabel": internship_routing.get("internshipDecisionLabel"),
        "internshipReadinessScore": internship_routing.get("internshipReadinessScore"),
        "internshipReadinessLevel": internship_routing.get("internshipReadinessLevel"),
        "internshipReadinessLabel": internship_routing.get("internshipReadinessLabel"),
        "recommendationConfidence": internship_routing.get("recommendationConfidence"),
        "recommendedInternshipFormat": internship_routing.get("recommendedInternshipFormat"),
        "mentorNeedLevel": internship_routing.get("mentorNeedLevel"),
        "adaptationNeedLevel": internship_routing.get("adaptationNeedLevel"),
        "primaryRecommendedRole": role_recommendation.get("primaryRoleLabel"),
        "primaryRecommendedRoleKey": role_recommendation.get("primaryRoleKey"),
        "primaryRecommendedRoleCluster": role_recommendation.get("primaryRoleCluster"),
        "primaryRecommendedRoleScore": role_recommendation.get("primaryRoleScore"),
        "alternativeRecommendedRoles": role_recommendation.get("alternativeRoles", []),
        "roleRecommendations": role_recommendation.get("roles", []),
        "roleRecommendationSummary": role_recommendation.get("summary", ""),
        "recommendedProfession": role_recommendation.get("primaryRoleLabel"),
        "recommendedProfessionKey": role_recommendation.get("primaryRoleKey"),
        "expertVerdict": {
            **expert_verdict,
            "recommendedRole": role_recommendation.get("primaryRoleLabel"),
            "recommendedRoleKey": role_recommendation.get("primaryRoleKey"),
            "primaryInternshipTrack": internship_routing.get("primaryInternshipTrack"),
            "primaryInternshipTrackLabel": internship_routing.get("primaryInternshipTrackLabel"),
            "internshipDecision": internship_routing.get("internshipDecision"),
            "internshipDecisionLabel": internship_routing.get("internshipDecisionLabel"),
            "internshipReadinessScore": internship_routing.get("internshipReadinessScore"),
            "recommendationConfidence": internship_routing.get("recommendationConfidence"),
        },
        "empRadar": emp_radar,
        "cognitiveRadar": cognitive_radar,
        "quality": {
            "attentionPassRate": attention_pass_rate,
            "socialDesirabilityIndex": social_desirability_index,
            "consistencyIndex": consistency_index,
            "dataQualityIndex": data_quality_index,
        },
        "behavioralStyle": {
            "hesitationIndex": behavioral_hesitation,
            "responseStabilityIndex": response_stability_index,
            "skipTendency": skip_tendency,
            "persistenceIndex": persistence_index,
        },
        "gameMetrics": {
            "rank": rank_map.get(player_id),
            "cognitiveAccuracy": cognitive_accuracy,
            "avgCognitiveTimeMs": avg_cognitive_time,
            "speedIndex": speed_index,
            "cognitiveTasks": cognitive_total,
            "partialDensity": partial_density,
        },
        "history": history_rows,
    }


def compute_reports_for_room(room_code: str) -> dict:
    room_results = db_room_results(room_code)
    players = sort_players_by_score(room_results.get("players", []))
    rank_map = {player.get("playerId"): idx + 1 for idx, player in enumerate(players)}
    answers_by_player: Dict[str, List[dict]] = defaultdict(list)
    for ans in room_results.get("answers", []) or []:
        answers_by_player[ans.get("playerId")].append(ans)
    meta_by_key = _question_meta_map(room_results)

    profiles = []
    for player in players:
        profile = _build_player_report(player, answers_by_player.get(player.get("playerId"), []), meta_by_key, rank_map)
        profiles.append(profile)
        db_player_report_upsert(room_code, profile["playerId"], profile["playerName"], profile)

    avg_score = round(_mean([float(p.get("score", 0)) for p in profiles], default=0.0), 1) if profiles else 0.0
    avg_accuracy = round(_mean([float((p.get("gameMetrics") or {}).get("cognitiveAccuracy", 0.0)) for p in profiles], default=0.0), 1) if profiles else 0.0
    avg_potential = round(_mean([float(p.get("overallPotential", 0.0)) for p in profiles], default=0.0), 1) if profiles else 0.0
    avg_general_fit = round(_mean([float(p.get("overallProfessionalFit", p.get("overallPotential", 0.0))) for p in profiles], default=0.0), 1) if profiles else 0.0
    avg_it_fit = round(_mean([float(p.get("itFit", 0.0)) for p in profiles], default=0.0), 1) if profiles else 0.0
    avg_fit_confidence = round(_mean([float(p.get("fitConfidence", 0.0)) for p in profiles], default=0.0), 1) if profiles else 0.0
    avg_social_desirability = round(_mean([float((p.get("quality") or {}).get("socialDesirabilityIndex", 25.0)) for p in profiles], default=25.0), 1) if profiles else 25.0
    avg_internship_readiness = round(_mean([float(p.get("internshipReadinessScore", 0.0) or 0.0) for p in profiles], default=0.0), 1) if profiles else 0.0
    avg_recommendation_confidence = round(_mean([float(p.get("recommendationConfidence", 0.0) or 0.0) for p in profiles], default=0.0), 1) if profiles else 0.0

    tag_counter = Counter()
    risk_counter = Counter()
    role_counter = Counter()
    internship_track_counter = Counter()
    internship_decision_counter = Counter()
    for profile in profiles:
        for tag in (profile.get("fitTags", []) or []) + (profile.get("itFitTags", []) or []):
            tag_counter[str(tag)] += 1
        for flag in profile.get("riskFlags", []) or []:
            risk_counter[str(flag)] += 1
        primary_role = str(profile.get("primaryRecommendedRole") or "").strip()
        if primary_role and primary_role != "Нужна дополнительная профдиагностика":
            role_counter[primary_role] += 1
        primary_track = str(profile.get("primaryInternshipTrackLabel") or "").strip()
        if primary_track:
            internship_track_counter[primary_track] += 1
        decision_label = str(profile.get("internshipDecisionLabel") or "").strip()
        if decision_label:
            internship_decision_counter[decision_label] += 1
    top_tags = [{"tag": tag, "count": count} for tag, count in tag_counter.most_common(8)]
    top_risks = [{"risk": risk, "count": count} for risk, count in risk_counter.most_common(6)]
    top_roles = [{"role": role, "count": count} for role, count in role_counter.most_common(6)]
    top_internship_tracks = [{"track": track, "count": count} for track, count in internship_track_counter.most_common(8)]
    internship_decisions = [{"decision": decision, "count": count} for decision, count in internship_decision_counter.most_common(6)]

    def avg_radar(key_name: str) -> List[dict]:
        values: Dict[str, List[float]] = defaultdict(list)
        labels: Dict[str, str] = {}
        for profile in profiles:
            for item in profile.get(key_name, []) or []:
                values[item.get("key")].append(float(item.get("value", 0.0)))
                labels[item.get("key")] = item.get("label")
        out = []
        for key, vals in values.items():
            out.append({"key": key, "label": labels.get(key, key), "value": round(_mean(vals, default=0.0), 1)})
        return out

    dashboard = {
        "roomCode": room_code,
        "summary": {
            "playersCount": len(profiles),
            "avgScore": avg_score,
            "avgAccuracy": avg_accuracy,
            "avgPotential": avg_potential,
            "avgGeneralFit": avg_general_fit,
            "avgItFit": avg_it_fit,
            "avgFitConfidence": avg_fit_confidence,
            "avgSocialDesirability": avg_social_desirability,
            "avgInternshipReadiness": avg_internship_readiness,
            "avgRecommendationConfidence": avg_recommendation_confidence,
            "topTags": top_tags,
            "topRisks": top_risks,
            "topRoles": top_roles,
            "topInternshipTracks": top_internship_tracks,
            "internshipDecisions": internship_decisions,
        },
        "roomRadar": {
            "emp": avg_radar("empRadar"),
            "cognitive": avg_radar("cognitiveRadar"),
        },
        "players": profiles,
    }
    db_room_report_upsert(room_code, dashboard)
    return dashboard


def build_and_store_reports(room_code: str) -> dict:
    return compute_reports_for_room(room_code)


def _csv_response(filename: str, rows: List[Dict[str, Any]], fieldnames: List[str]) -> Response:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe_row = {}
        for field in fieldnames:
            value = row.get(field, "")
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            safe_row[field] = value
        writer.writerow(safe_row)
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def build_hr_export_rows(dashboard: dict) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for profile in dashboard.get("players", []) or []:
        quality = profile.get("quality") or {}
        game = profile.get("gameMetrics") or {}
        rows.append(
            {
                "roomCode": dashboard.get("roomCode"),
                "playerId": profile.get("playerId"),
                "playerName": profile.get("playerName"),
                "rank": game.get("rank"),
                "score": profile.get("score"),
                "overallPotential": profile.get("overallPotential"),
                "overallProfessionalFit": profile.get("overallProfessionalFit"),
                "itFit": profile.get("itFit"),
                "fitConfidence": profile.get("fitConfidence"),
                "itFitConfidence": profile.get("itFitConfidence"),
                "recommendedTrack": profile.get("recommendedTrackLabel") or profile.get("recommendedTrack"),
                "primaryInternshipTrack": profile.get("primaryInternshipTrackLabel") or profile.get("primaryInternshipTrack"),
                "internshipDecision": profile.get("internshipDecisionLabel") or profile.get("internshipDecision"),
                "internshipReadinessScore": profile.get("internshipReadinessScore"),
                "internshipReadinessLevel": profile.get("internshipReadinessLabel") or profile.get("internshipReadinessLevel"),
                "recommendationConfidence": profile.get("recommendationConfidence"),
                "recommendedInternshipFormat": profile.get("recommendedInternshipFormat"),
                "mentorNeedLevel": profile.get("mentorNeedLevel"),
                "adaptationNeedLevel": profile.get("adaptationNeedLevel"),
                "criticalGaps": _flatten_tags([(item.get("label") or item.get("axis") or "") for item in (profile.get("criticalGaps") or [])]),
                "developmentGaps": _flatten_tags([(item.get("label") or item.get("axis") or "") for item in (profile.get("developmentGaps") or [])]),
                "fastGrowthZones": _flatten_tags([(item.get("label") or item.get("axis") or "") for item in (profile.get("fastGrowthZones") or [])]),
                "primaryRecommendedRole": profile.get("primaryRecommendedRole"),
                "primaryRecommendedRoleCluster": profile.get("primaryRecommendedRoleCluster"),
                "primaryRecommendedRoleScore": profile.get("primaryRecommendedRoleScore"),
                "alternativeRecommendedRoles": _flatten_tags(profile.get("alternativeRecommendedRoles", [])),
                "roleRecommendationSummary": profile.get("roleRecommendationSummary"),
                "cognitiveAccuracyPct": game.get("cognitiveAccuracy"),
                "avgCognitiveTimeMs": game.get("avgCognitiveTimeMs"),
                "speedIndex": game.get("speedIndex"),
                "attentionPassRate": quality.get("attentionPassRate"),
                "socialDesirabilityIndex": quality.get("socialDesirabilityIndex"),
                "consistencyIndex": quality.get("consistencyIndex"),
                "dataQualityIndex": quality.get("dataQualityIndex"),
                "badges": _flatten_tags(profile.get("badges", [])),
                "fitTags": _flatten_tags(profile.get("fitTags", [])),
                "generalFitTags": _flatten_tags(profile.get("generalFitTags", [])),
                "itFitTags": _flatten_tags(profile.get("itFitTags", [])),
                "riskFlags": _flatten_tags(profile.get("riskFlags", [])),
                "strengths": _flatten_tags(profile.get("strengths", [])),
                "growthZones": _flatten_tags(profile.get("growthZones", [])),
                "recommendations": _flatten_tags(profile.get("recommendations", [])),
                "summaryText": profile.get("summaryText"),
            }
        )
    return rows


def build_hr_html_report(dashboard: dict) -> str:
    summary = dashboard.get("summary") or {}
    players_html = []
    room_code = str(dashboard.get("roomCode", "") or "")
    for profile in dashboard.get("players", []) or []:
        game = profile.get("gameMetrics") or {}
        ai_report_row = db_player_ai_report_get(room_code, str(profile.get("playerId", "") or "")) if room_code else None
        ai_report = _normalize_ai_report((ai_report_row or {}).get("analysisJson") or {}, profile) if ai_report_row else None
        ai_html = ""
        if ai_report:
            ai_html = f"""
          <div class="ai-box">
            <h3>ИИ-рекомендации</h3>
            <div class="ai-grid">
              <div><b>AI fit:</b> {html_lib.escape(str(ai_report.get('fit', '—')))}</div>
              <div><b>Уверенность:</b> {html_lib.escape(str(ai_report.get('confidence', '—')))}</div>
              <div><b>Основной трек:</b> {html_lib.escape(str(ai_report.get('primaryTrack', '—')))}</div>
              <div><b>Альтернативы:</b> {html_lib.escape(' · '.join(ai_report.get('altTracks') or []) or '—')}</div>
            </div>
            <p class="summary">{html_lib.escape(str(ai_report.get('summary', '')))}</p>
            <div class="cols">
              <div><h3>Сильные стороны по ИИ</h3><ul>{''.join(f'<li>{html_lib.escape(str(x))}</li>' for x in (ai_report.get('strengths') or []))}</ul></div>
              <div><h3>Риски и наблюдения</h3><ul>{''.join(f'<li>{html_lib.escape(str(x))}</li>' for x in (ai_report.get('risks') or []))}</ul></div>
              <div><h3>Что уточнить на интервью</h3><ul>{''.join(f'<li>{html_lib.escape(str(x))}</li>' for x in (ai_report.get('interview') or []))}</ul></div>
            </div>
            <p class="footnote">{html_lib.escape(str(ai_report.get('caveats', '')))}</p>
          </div>
            """
        players_html.append(
            f"""
        <section class="player">
          <h2>{html_lib.escape(str(profile.get('playerName', 'Игрок')))} <span class="rank">#{game.get('rank', '—')}</span></h2>
          <p class="summary">{html_lib.escape(str(profile.get('summaryText', '')))}</p>
          <div class="metrics">
            <div><b>Общий fit:</b> {profile.get('overallProfessionalFit', profile.get('overallPotential', '—'))}/100</div>
            <div><b>IT-fit:</b> {profile.get('itFit', '—')}/100</div>
            <div><b>Надёжность вывода:</b> {profile.get('fitConfidence', '—')}/100</div>
            <div><b>Рекомендованный трек:</b> {html_lib.escape(str(profile.get('recommendedTrackLabel', profile.get('recommendedTrack', '—'))))}</div>
            <div><b>Рекомендуемая роль:</b> {html_lib.escape(str(profile.get('primaryRecommendedRole', '—')))}</div>
            <div><b>Сила совпадения роли:</b> {profile.get('primaryRecommendedRoleScore', '—')}/100</div>
            <div><b>Альтернативные роли:</b> {html_lib.escape(' · '.join(profile.get('alternativeRecommendedRoles') or []) or '—')}</div>
            <div><b>Точность:</b> {game.get('cognitiveAccuracy', '—')}%</div>
            <div><b>Скорость:</b> {game.get('speedIndex', '—')}/100</div>
            <div><b>Контроль внимания:</b> {(profile.get('quality') or {}).get('attentionPassRate', '—')}%</div>
            <div><b>Соц. желательность:</b> {(profile.get('quality') or {}).get('socialDesirabilityIndex', '—')}/100</div>
            <div><b>Согласованность:</b> {(profile.get('quality') or {}).get('consistencyIndex', '—')}/100</div>
            <div><b>Пропуски:</b> {(profile.get('behavioralStyle') or {}).get('skipTendency', '—')}%</div>
          </div>
          <div class="chips">{''.join(f'<span>{html_lib.escape(str(tag))}</span>' for tag in (profile.get('generalFitTags') or []))}</div>
          <div class="chips">{''.join(f'<span>{html_lib.escape(str(tag))}</span>' for tag in (profile.get('itFitTags') or []))}</div>
          <div class="chips">{''.join(f'<span>{html_lib.escape(str(tag))}</span>' for tag in (profile.get('riskFlags') or []))}</div>
          <div class="chips">{''.join(f'<span>{html_lib.escape(str(tag))}</span>' for tag in (profile.get('badges') or []))}</div>
          <p class="summary"><b>Профрекомендация:</b> {html_lib.escape(str(profile.get('roleRecommendationSummary', '—')))}</p>
          <div class="cols">
            <div><h3>Сильные стороны</h3><ul>{''.join(f'<li>{html_lib.escape(str(x))}</li>' for x in (profile.get('strengths') or []))}</ul></div>
            <div><h3>Зоны роста</h3><ul>{''.join(f'<li>{html_lib.escape(str(x))}</li>' for x in (profile.get('growthZones') or []))}</ul></div>
            <div><h3>Рекомендации</h3><ul>{''.join(f'<li>{html_lib.escape(str(x))}</li>' for x in (profile.get('recommendations') or []))}</ul></div>
          </div>
          {ai_html}
        </section>
        """
        )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>EMOPOT HR report {html_lib.escape(str(dashboard.get('roomCode', '')))}</title>
<style>body{{font-family:Inter,Arial,sans-serif;background:#f8fafc;color:#0f172a;margin:0;padding:24px}}h1{{margin:0 0 8px}}.card{{background:#fff;border:1px solid #e2e8f0;border-radius:18px;padding:18px;margin:0 0 16px;box-shadow:0 10px 30px rgba(15,23,42,.06)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.stat{{background:#f8fafc;border-radius:14px;padding:12px;border:1px solid #e5e7eb}}.player{{background:#fff;border:1px solid #e5e7eb;border-radius:18px;padding:18px;margin-top:16px}}.rank{{color:#2563eb}}.summary{{line-height:1.6}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:12px 0}}.chips{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}}.chips span{{background:#eef2ff;border:1px solid #c7d2fe;border-radius:999px;padding:6px 10px;font-weight:700;color:#312e81}}.cols{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}}.ai-box{{margin-top:16px;padding:16px;border-radius:16px;border:1px solid #dbeafe;background:linear-gradient(135deg,#eff6ff,#ffffff)}}.ai-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:12px 0}}.footnote{{margin-top:10px;color:#475569;font-size:.94rem;line-height:1.5}}ul{{margin:8px 0 0;padding-left:18px}}@media(max-width:900px){{.cols{{grid-template-columns:1fr}}}}</style></head><body>
<div class="card">
<h1>EMOPOT — HR-отчёт по комнате {html_lib.escape(str(dashboard.get('roomCode', '')))}</h1>
<p>Игроков: <b>{summary.get('playersCount', 0)}</b>. Средний балл: <b>{summary.get('avgScore', 0)}</b>. Средняя точность: <b>{summary.get('avgAccuracy', 0)}%</b>. Средний общий fit: <b>{summary.get('avgGeneralFit', summary.get('avgPotential', 0))}/100</b>. Средний IT-fit: <b>{summary.get('avgItFit', 0)}/100</b>. Средняя надёжность вывода: <b>{summary.get('avgFitConfidence', 0)}/100</b>. Средняя соц. желательность: <b>{summary.get('avgSocialDesirability', 25.0)}/100</b>.</p>
<div class="grid">
<div class="stat"><div>Средняя соц. желательность</div><div>{summary.get('avgSocialDesirability', 25.0)}/100</div></div>
<div class="stat"><div>Топ-теги</div><div>{html_lib.escape(', '.join([str(item.get('tag')) for item in (summary.get('topTags') or [])])) or '—'}</div></div>
<div class="stat"><div>Топ-роли</div><div>{html_lib.escape(', '.join([str(item.get('role')) for item in (summary.get('topRoles') or [])])) or '—'}</div></div>
<div class="stat"><div>Топ-риски</div><div>{html_lib.escape(', '.join([str(item.get('risk')) for item in (summary.get('topRisks') or [])])) or '—'}</div></div>
</div>
</div>
{''.join(players_html)}
</body></html>"""


# ====================== FastAPI ======================
app = FastAPI(title="EMOPOT — локальный сервер")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def on_startup():
    db_init()


@app.get("/", response_class=HTMLResponse)
def root():
    return FileResponse(APP_DIR / "index.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_landing():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/api/tasks")
def api_tasks():
    return JSONResponse(task_counts())


@app.post("/api/tasks/reload")
def api_tasks_reload():
    global RAW_TASKS, TASK_BANK, CATEGORIES, TASK_BY_ID
    RAW_TASKS = load_tasks_raw()
    TASK_BANK = transform_tasks(RAW_TASKS)
    CATEGORIES = list(TASK_BANK.keys())
    TASK_BY_ID = {q["id"]: q for q in TASK_BANK.values() for q in q}
    return JSONResponse({"ok": True, **task_counts()})


@app.get("/api/room/{code}/results")
def api_room_results(code: str):
    code = code.upper()
    if code not in ROOMS and not exists_in_db(code=code):
        return JSONResponse({"error": "room not found"}, status_code=404)
    return JSONResponse(db_room_results(code))


def exists_in_db(code: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM rooms WHERE code=?", (code,))
    row = cur.fetchone()
    con.close()
    return bool(row)


@app.get("/api/export/{code}/player/{player_id}.csv")
def export_player_csv(code: str, player_id: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT round_no, question_id, category, block, instrument, item_type,
               answer_text, answer_choice, is_correct, awarded, scored_value, time_spent_ms,
               partial_credit, response_quality, error_pattern, skip_reason, final_answer_state,
               first_response_ms, last_change_ms, change_count, hesitation_index
        FROM answers WHERE room_code=? AND player_id=? ORDER BY round_no
    """,
        (code.upper(), player_id),
    )
    rows = cur.fetchall()
    con.close()
    if not rows:
        return PlainTextResponse("Нет данных", status_code=404)
    header = "round,questionId,category,block,instrument,itemType,answerText,answerChoice,isCorrect,awarded,scoredValue,timeMs,partialCredit,responseQuality,errorPattern,skipReason,finalAnswerState,firstResponseMs,lastChangeMs,changeCount,hesitationIndex\n"

    def fmt(x: Any) -> str:
        s = "" if x is None else str(x)
        return s.replace(",", " ")

    body = "\n".join(",".join(map(fmt, r)) for r in rows)
    content = header + body + "\n"
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{code.upper()}_{player_id}.csv"'},
    )


@app.get("/api/export/{code}/room.csv")
def export_room_csv(code: str):
    data = db_room_results(code.upper())
    header = "playerId,name,score\n"
    body = "\n".join(f'{p["playerId"]},{p["name"]},{p["score"]}' for p in data["players"])
    content = header + body + "\n"
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{code.upper()}_summary.csv"'},
    )


@app.get("/api/room/{code}/admin-report")
def api_room_admin_report(code: str):
    code = code.upper()
    dashboard = db_room_report_get(code)
    if dashboard is None:
        if not exists_in_db(code):
            return JSONResponse({"error": "room not found"}, status_code=404)
        dashboard = build_and_store_reports(code)
    return JSONResponse(dashboard)


@app.get("/api/room/{code}/player/{player_id}/report")
def api_player_report(code: str, player_id: str):
    code = code.upper()
    profile = db_player_report_get(code, player_id)
    if profile is None:
        if not exists_in_db(code):
            return JSONResponse({"error": "room not found"}, status_code=404)
        build_and_store_reports(code)
        profile = db_player_report_get(code, player_id)
    if profile is None:
        return JSONResponse({"error": "player report not found"}, status_code=404)
    return JSONResponse(profile)


@app.get("/api/export/{code}/hr-report.csv")
def export_hr_report_csv(code: str):
    code = code.upper()
    dashboard = build_and_store_reports(code)
    rows = build_hr_export_rows(dashboard)
    fieldnames = [
        "roomCode", "playerId", "playerName", "rank", "score", "overallPotential",
        "overallProfessionalFit", "itFit", "fitConfidence", "itFitConfidence",
        "recommendedTrack", "primaryInternshipTrack", "internshipDecision", "internshipReadinessScore",
        "internshipReadinessLevel", "recommendationConfidence", "recommendedInternshipFormat",
        "mentorNeedLevel", "adaptationNeedLevel", "criticalGaps", "developmentGaps", "fastGrowthZones",
        "primaryRecommendedRole", "primaryRecommendedRoleCluster", "primaryRecommendedRoleScore",
        "alternativeRecommendedRoles", "roleRecommendationSummary",
        "cognitiveAccuracyPct", "avgCognitiveTimeMs", "speedIndex", "attentionPassRate",
        "socialDesirabilityIndex", "consistencyIndex", "dataQualityIndex", "badges", "fitTags", "generalFitTags", "itFitTags", "riskFlags", "strengths",
        "growthZones", "recommendations", "summaryText",
    ]
    return _csv_response(f"{code}_hr_report.csv", rows, fieldnames)


@app.get("/api/export/{code}/hr-report.json")
def export_hr_report_json(code: str):
    code = code.upper()
    dashboard = build_and_store_reports(code)
    return Response(
        json.dumps(dashboard, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{code}_hr_report.json"'},
    )



@app.get("/api/export/{code}/hr-report.html")
def export_hr_report_html(code: str):
    code = code.upper()
    dashboard = build_and_store_reports(code)
    html_text = build_hr_html_report(dashboard)
    return Response(
        html_text,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{code}_hr_report.html"'},
    )


@app.get("/api/export/{code}/player/{player_id}/profile.json")
def export_player_profile_json(code: str, player_id: str):
    code = code.upper()
    profile = db_player_report_get(code, player_id)
    if profile is None:
        build_and_store_reports(code)
        profile = db_player_report_get(code, player_id)
    if profile is None:
        return PlainTextResponse("Нет данных", status_code=404)
    return Response(
        json.dumps(profile, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{code}_{player_id}_profile.json"'},
    )


@app.get("/api/room/{code}/player/{player_id}/ai-report")
def api_player_ai_report(code: str, player_id: str):
    code = code.upper()
    report = db_player_ai_report_get(code, player_id)
    if not report:
        return JSONResponse({"ok": True, "status": "empty", "data": None})
    return JSONResponse({"ok": True, "status": report.get("status", "ready"), "data": report})


@app.post("/api/room/{code}/player/{player_id}/ai-report/generate")
def api_generate_player_ai_report(code: str, player_id: str):
    code = code.upper()
    if not exists_in_db(code):
        return JSONResponse({"ok": False, "error": "room not found"}, status_code=404)
    try:
        report = generate_and_store_ai_report(code, player_id)
        return JSONResponse({"ok": True, "status": report.get("status", "ready"), "data": report})
    except Exception as e:
        traceback.print_exc()
        error_report = _normalize_ai_report({
            "fit": "Ошибка генерации",
            "fitSub": "Не удалось сформировать AI-экспертизу по выбранному участнику.",
            "confidence": "0%",
            "primaryTrack": "—",
            "primaryTrackSub": "—",
            "altTracks": [],
            "summary": f"Ошибка генерации AI-экспертизы: {e}",
            "strengths": [],
            "risks": [],
            "interview": [],
            "caveats": "Проверьте GEMINI_API_KEY_INLINE, установку пакета google-genai и доступность Gemini API.",
        })
        try:
            db_player_ai_report_save(code, player_id, model_name=GEMINI_MODEL, input_json={}, analysis_json=error_report, analysis_text=error_report.get("summary", ""), status="error")
        except Exception:
            traceback.print_exc()
        return JSONResponse({"ok": False, "status": "error", "error": str(e), "data": {"createdAt": _now_iso(), "modelName": GEMINI_MODEL, "analysisText": error_report.get("summary", ""), "analysisJson": error_report, "status": "error"}}, status_code=500)


# ====================== WebSocket ======================
@app.post("/api/telemetry/beacon")
async def api_telemetry_beacon(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    if isinstance(body, list):
        events = [item for item in body if isinstance(item, dict)]
    elif isinstance(body, dict):
        raw = body.get("events")
        if isinstance(raw, list):
            events = [item for item in raw if isinstance(item, dict)]
        else:
            events = [body]
    else:
        events = []

    accepted = 0
    for item in events:
        try:
            process_telemetry_event(item)
            accepted += 1
        except Exception:
            traceback.print_exc()

    return JSONResponse({"ok": True, "accepted": accepted})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "admin_create_room":
                code = (msg.get("preferredCode") or gen_code()).upper()
                if code in ROOMS:
                    code = gen_code()

                assessment_mode = resolve_assessment_mode(msg.get("assessmentMode"), msg.get("taskFilterMode"))
                block_config = sanitize_block_config(msg.get("blockConfig"), fallback_mode=assessment_mode)

                raw_rounds = msg.get("rounds", None)
                if raw_rounds is None or raw_rounds == "":
                    rounds = 0
                else:
                    rounds = max(1, min(40, _safe_int(raw_rounds, 0)))

                wait_for_all_players = bool(msg.get("waitForAllPlayers", True))
                room = Room(
                    code=code,
                    rounds=rounds,
                    assessment_mode=assessment_mode,
                    block_config=block_config,
                    wait_for_all_players=wait_for_all_players,
                )
                ROOMS[code] = room
                room.admin = ws
                CLIENT_TO_ROOM[ws] = code
                db_room_upsert(code, room.rounds, "lobby", room.assessment_mode, room.block_config, room.wait_for_all_players)
                await ws.send_json(
                    {
                        "type": "room_created",
                        "roomCode": code,
                        "assessmentMode": room.assessment_mode,
                        "blockConfig": room.block_config,
                        "waitForAllPlayers": room.wait_for_all_players,
                    }
                )
                continue

            if t == "admin_attach":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if not room:
                    try:
                        db_player_session_touch(str(msg.get("clientSessionId") or "").strip(), room_code=code, player_name=name, event_type="join_failed", payload={"reason": "room_not_found", "playerName": name, "roomCode": code})
                    except Exception:
                        traceback.print_exc()
                    await ws.send_json({"type": "error", "message": "Комната не найдена"})
                    continue
                room.admin = ws
                CLIENT_TO_ROOM[ws] = code
                await ws.send_json(
                    {
                        "type": "room_attached",
                        "roomCode": code,
                        "players": room.snapshot_players(),
                        "status": room.status,
                        "assessmentMode": room.assessment_mode,
                        "blockConfig": room.block_config,
                        "waitForAllPlayers": room.wait_for_all_players,
                    }
                )
                continue

            if t == "admin_set_wait_mode":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if not room or room.admin is not ws:
                    await ws.send_json({"type": "error", "message": "Нет прав/комната не найдена"})
                    continue
                room.wait_for_all_players = bool(msg.get("waitForAllPlayers", True))
                db_room_upsert(code, room.rounds, room.status, room.assessment_mode, room.block_config, room.wait_for_all_players)
                await broadcast(code, {"type": "room_settings", "waitForAllPlayers": room.wait_for_all_players})
                continue

            if t == "join":
                code = msg["roomCode"].upper()
                name = str(msg.get("playerName", "Игрок")).strip()[:32]
                room = ROOMS.get(code)
                if not room:
                    try:
                        db_player_session_touch(str(msg.get("clientSessionId") or "").strip(), room_code=code, player_name=name, event_type="join_failed", payload={"reason": "room_not_found", "playerName": name, "roomCode": code})
                    except Exception:
                        traceback.print_exc()
                    await ws.send_json({"type": "error", "message": "Комната не найдена"})
                    continue
                if room.status != "lobby":
                    try:
                        db_player_session_touch(str(msg.get("clientSessionId") or "").strip(), room_code=code, player_name=name, event_type="join_failed", payload={"reason": "game_already_running", "playerName": name, "roomCode": code})
                    except Exception:
                        traceback.print_exc()
                    await ws.send_json({"type": "error", "message": "Игра уже идёт"})
                    continue
                if len(room.players) >= 10:
                    try:
                        db_player_session_touch(str(msg.get("clientSessionId") or "").strip(), room_code=code, player_name=name, event_type="join_failed", payload={"reason": "room_full", "playerName": name, "roomCode": code})
                    except Exception:
                        traceback.print_exc()
                    await ws.send_json({"type": "error", "message": "Комната заполнена"})
                    continue
                client_session_id = str(msg.get("clientSessionId") or "").strip()
                pid = msg.get("playerId") or ("p_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8)))
                pc = PlayerConn(ws=ws, id=pid, name=name, client_session_id=client_session_id)
                room.players[pid] = pc
                CLIENT_TO_ROOM[ws] = code
                db_player_upsert(room.code, pid, name, 0)
                try:
                    db_event_add(room.code, pid, None, "", "join_success", None, {"playerName": name, "clientSessionId": client_session_id})
                    db_player_session_touch(client_session_id, room_code=room.code, player_id=pid, player_name=name, event_type="join_success", payload={"playerName": name, "roomCode": room.code})
                except Exception:
                    traceback.print_exc()
                await ws.send_json({"type": "joined", "roomCode": code, "playerId": pid, "players": room.snapshot_players(), "waitForAllPlayers": room.wait_for_all_players})
                await broadcast(code, {"type": "players", "players": room.snapshot_players(), "waitForAllPlayers": room.wait_for_all_players})
                continue

            if t == "admin_start":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if not room or room.admin is not ws:
                    await ws.send_json({"type": "error", "message": "Нет прав/комната не найдена"})
                    continue
                if len(room.players) == 0:
                    await ws.send_json({"type": "error", "message": "Нет игроков"})
                    continue

                requested_rounds = max(0, _safe_int(room.rounds, 0))
                if should_use_full_session_plan(room.assessment_mode):
                    full_plan = build_session_plan(room)
                    if not full_plan:
                        await ws.send_json({"type": "error", "message": "Не удалось собрать сценарий из доступных задач"})
                        continue
                    room.session_plan = limit_session_plan(full_plan, requested_rounds)
                    room.rounds = len(room.session_plan)
                else:
                    room.session_plan = []
                    if requested_rounds > 0:
                        room.rounds = requested_rounds
                    else:
                        enabled_count = len(_collect_enabled_tasks(room.block_config or sanitize_block_config(None, fallback_mode=room.assessment_mode)))
                        if enabled_count <= 0:
                            await ws.send_json({"type": "error", "message": "Нет доступных задач в выбранных блоках"})
                            continue
                        room.rounds = enabled_count

                room.status = "running"
                db_room_upsert(code, room.rounds, "running", room.assessment_mode, room.block_config, room.wait_for_all_players)
                await broadcast(code, {"type": "game_started", "rounds": room.rounds, "waitForAllPlayers": room.wait_for_all_players})
                asyncio.create_task(run_rounds(room))
                continue

            if t == "answer":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if not room:
                    continue
                pid = msg.get("playerId")
                pc = room.players.get(pid)
                if not pc or room.status != "running" or room.current_question is None:
                    continue
                if pc.answered:
                    continue
                now = time.time()
                if now > room.round_deadline:
                    continue

                q = room.current_question
                if q["type"] == "mcq":
                    ch = msg.get("choice")
                    try:
                        pc.ans_choice = int(ch) if ch is not None else None
                    except Exception:
                        pc.ans_choice = None
                    pc.ans_text = ""
                else:
                    pc.ans_text = str(msg.get("text", ""))[:300]
                    pc.ans_choice = None

                spent_ms = int((now - (room.round_deadline - room.time_limit)) * 1000)
                pc.ans_time_ms = max(0, spent_ms)
                _record_interaction_event(pc.round_interaction, "submit_clicked", pc.ans_time_ms, {"qtype": q.get("type")})
                pc.answered = True

                answered_players = [{"playerId": p.id, "name": p.name} for p in room.players.values() if p.answered]
                pending_players = [{"playerId": p.id, "name": p.name} for p in room.players.values() if not p.answered]
                await broadcast(code, {
                    "type": "answer_progress",
                    "round": room.current_round,
                    "questionId": q.get("id"),
                    "answeredPlayers": answered_players,
                    "pendingPlayers": pending_players,
                    "players": room.snapshot_players(include_answered=True),
                    "waitForAllPlayers": room.wait_for_all_players,
                })

                if room.wait_for_all_players and all(p.answered for p in room.players.values()):
                    if room.timer_task and not room.timer_task.done():
                        room.timer_task.cancel()
                    await finish_round(room)
                continue

            if t == "telemetry_event":
                try:
                    process_telemetry_event(msg)
                except Exception:
                    traceback.print_exc()
                continue

            if t == "admin_end":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if room and room.admin is ws:
                    room.status = "finished"
                    room.current_question = None

                    if room.timer_task and not room.timer_task.done():
                        room.timer_task.cancel()

                    db_room_upsert(code, room.rounds, "finished", room.assessment_mode, room.block_config, room.wait_for_all_players)

                    dashboard = None
                    try:
                        dashboard = build_and_store_reports(code)
                    except Exception:
                        traceback.print_exc()

                    await broadcast(
                        code,
                        {
                            "type": "final",
                            "scores": room.snapshot_players(),
                            "reportsReady": bool(dashboard),
                            "playersCount": len((dashboard or {}).get("players", [])),
                            "waitForAllPlayers": room.wait_for_all_players,
                        },
                    )
                continue

    except WebSocketDisconnect:
        pass
    finally:
        code = CLIENT_TO_ROOM.get(ws)
        if code:
            room = ROOMS.get(code)
            if room:
                if room.admin is ws:
                    room.admin = None
                drop_pid = None
                for pid, pc in list(room.players.items()):
                    if pc.ws is ws:
                        drop_pid = pid
                        break
                if drop_pid:
                    try:
                        db_event_add(code, drop_pid, room.current_round if room else None, (room.current_question or {}).get("id") if room and room.current_question else "", "socket_disconnected", None, {"clientSessionId": room.players[drop_pid].client_session_id if drop_pid in room.players else ""})
                        db_player_session_touch(room.players[drop_pid].client_session_id if drop_pid in room.players else "", room_code=code, player_id=drop_pid, player_name=room.players[drop_pid].name if drop_pid in room.players else "", event_type="socket_disconnected", payload={"reason": "socket_disconnected", "roomCode": code})
                    except Exception:
                        traceback.print_exc()
                    del room.players[drop_pid]
                    await safe_broadcast(code, {"type": "players", "players": room.snapshot_players(), "waitForAllPlayers": room.wait_for_all_players})
            CLIENT_TO_ROOM.pop(ws, None)


# ====================== game loop ======================
async def run_rounds(room: Room):
    room.used_ids = set()

    block_config = room.block_config or sanitize_block_config(None, fallback_mode=room.assessment_mode)

    has_card = any(q.get("mode", "base") == "card" and _allowed_by_block_config(q, block_config) for q in _all_tasks())
    has_non_card = any(q.get("mode", "base") != "card" and _allowed_by_block_config(q, block_config) for q in _all_tasks())

    plan = room.session_plan if room.session_plan else None
    total_rounds = len(plan) if plan else room.rounds
    room.rounds = total_rounds

    try:
        for r in range(1, total_rounds + 1):
            if room.status != "running":
                break

            room.current_round = r

            if plan:
                q = plan[r - 1]
            else:
                if room.assessment_mode == "cards_only":
                    desired_mode = "card"
                elif has_card and has_non_card:
                    desired_mode = "card" if r % 2 == 0 else "non_card"
                elif has_card:
                    desired_mode = "card"
                else:
                    desired_mode = "non_card"

                q = pick_question(room.used_ids, desired_mode=desired_mode, block_config=block_config)
                room.used_ids.add(q["id"])

            room.current_question = q
            db_round_question_upsert(room.code, r, q)

            for p in room.players.values():
                p.answered = False
                p.ans_text = ""
                p.ans_choice = None
                p.ans_time_ms = 0
                p.round_interaction = init_interaction_state()

            tl = int(q.get("timeRef") or random.randint(40, 60))
            room.time_limit = tl
            room.round_started_at = time.time()
            room.round_deadline = room.round_started_at + tl

            payload = {
                "type": "question",
                "round": r,
                "totalRounds": room.rounds,
                "category": q["category"],
                "block": task_block(q),
                "instrument": task_instrument(q),
                "itemType": task_item_type(q),
                "responseModel": infer_response_model(q),
                "questionId": q["id"],
                "timeLimit": tl,
                "qtype": q["type"],
                "prompt": q["prompt"],
                "title": q.get("prompt", ""),
                "mode": q.get("mode", "base"),
                "subtype": q.get("subtype"),
                "waitForAllPlayers": room.wait_for_all_players,
            }
            if q["type"] == "mcq":
                payload["options"] = q.get("options", [])

            await broadcast(room.code, payload)
            for p in room.players.values():
                try:
                    db_event_add(room.code, p.id, room.current_round, q["id"], "question_rendered", 0, {"mode": q.get("mode", "base"), "subtype": q.get("subtype")})
                except Exception:
                    traceback.print_exc()

            async def timer():
                try:
                    await asyncio.sleep(tl)
                    await finish_round(room)
                except asyncio.CancelledError:
                    pass

            room.timer_task = asyncio.create_task(timer())

            while room.current_question is not None and room.status == "running":
                await asyncio.sleep(0.1)

    except Exception:
        traceback.print_exc()

    finally:
        room.current_question = None

        if room.timer_task and not room.timer_task.done():
            room.timer_task.cancel()

        if room.status == "running":
            room.status = "finished"
            db_room_upsert(room.code, room.rounds, "finished", room.assessment_mode, room.block_config, room.wait_for_all_players)

        dashboard = None
        try:
            dashboard = build_and_store_reports(room.code)
        except Exception:
            traceback.print_exc()

        await broadcast(
            room.code,
            {
                "type": "final",
                "scores": room.snapshot_players(),
                "reportsReady": bool(dashboard),
                "playersCount": len((dashboard or {}).get("players", [])),
                "waitForAllPlayers": room.wait_for_all_players,
            },
        )


async def finish_round(room: Room):
    if room.current_question is None:
        return
    q = room.current_question
    mode = q.get("mode", "base")
    subtype = q.get("subtype")
    response_model = infer_response_model(q)
    block = task_block(q)
    instrument = task_instrument(q)
    item_type = task_item_type(q)
    results = []
    round_end_time = time.time()
    timed_out_globally = round_end_time >= room.round_deadline

    for p in room.players.values():
        if not p.answered and timed_out_globally:
            p.ans_time_ms = max(0, int((room.round_deadline - (room.round_deadline - room.time_limit)) * 1000))
        interaction_summary = _interaction_summary_from_state(p.round_interaction, p.ans_time_ms)
        evaluation = evaluate_player_response(
            q,
            p.ans_text,
            p.ans_choice,
            interaction_summary,
            answered=p.answered,
            timed_out=(not p.answered and timed_out_globally),
            disconnected=False,
        )
        ok = evaluation["is_correct"]
        awarded = evaluation["awarded"]
        scored_value = evaluation["scored_value"]
        partial_credit = evaluation["partial_credit"]
        response_quality = evaluation["response_quality"]
        error_pattern = evaluation["error_pattern"]
        skip_reason = evaluation["skip_reason"]
        final_answer_state = evaluation["final_answer_state"]
        status_text = evaluation["status_text"]

        p.score += awarded
        db_player_upsert(room.code, p.id, p.name, p.score)
        db_answer_add(
            room.code,
            room.current_round,
            q["id"],
            q["category"],
            block,
            instrument,
            item_type,
            p.id,
            p.name,
            p.ans_text,
            p.ans_choice,
            ok,
            awarded,
            scored_value,
            p.ans_time_ms,
            partial_credit=partial_credit,
            response_quality=response_quality,
            error_pattern=error_pattern,
            skip_reason=skip_reason,
            final_answer_state=final_answer_state,
            first_response_ms=interaction_summary.get("firstResponseMs"),
            last_change_ms=interaction_summary.get("lastChangeMs"),
            change_count=interaction_summary.get("changeCount", 0),
            hesitation_index=interaction_summary.get("hesitationIndex", 0.0),
            interaction_summary=interaction_summary,
        )
        results.append(
            {
                "playerId": p.id,
                "name": p.name,
                "choice": p.ans_choice,
                "text": p.ans_text,
                "isCorrect": ok,
                "statusText": status_text,
                "awarded": awarded,
                "scoredValue": scored_value,
                "partialCredit": partial_credit,
                "responseQuality": response_quality,
                "errorPattern": error_pattern,
                "skipReason": skip_reason,
                "finalAnswerState": final_answer_state,
                "timeMs": p.ans_time_ms,
                "interactionSummary": interaction_summary,
                "score": p.score,
            }
        )

    reveal = {
        "type": "reveal",
        "round": room.current_round,
        "questionId": q["id"],
        "category": q["category"],
        "block": block,
        "instrument": instrument,
        "itemType": item_type,
        "responseModel": response_model,
        "qtype": q["type"],
        "prompt": q["prompt"],
        "mode": mode,
        "subtype": subtype,
        "results": results,
        "scores": room.snapshot_players(),
        "waitForAllPlayers": room.wait_for_all_players,
    }
    if q["type"] == "mcq":
        reveal["options"] = q.get("options", [])
        reveal["correctIndex"] = q.get("correctIndex")
        if q.get("options") and q.get("correctIndex") is not None:
            ci = q.get("correctIndex", -1)
            if 0 <= ci < len(q["options"]):
                reveal["correctText"] = q["options"][ci]
            else:
                reveal["correctText"] = None
        else:
            reveal["correctText"] = None
    else:
        acc = q.get("accept", [])
        reveal["accepted"] = acc
        reveal["correctText"] = acc[0] if acc else ""

    await broadcast(room.code, reveal)
    room.current_question = None


async def broadcast(room_code: str, payload: dict):
    await safe_broadcast(room_code, payload)


async def safe_broadcast(room_code: str, payload: dict):
    room = ROOMS.get(room_code)
    if not room:
        return
    dead = []
    if room.admin:
        try:
            await room.admin.send_json(payload)
        except Exception:
            dead.append(room.admin)
    for pc in list(room.players.values()):
        try:
            await pc.ws.send_json(payload)
        except Exception:
            dead.append(pc.ws)
    for ws in dead:
        CLIENT_TO_ROOM.pop(ws, None)


@app.get("/healthz")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
