from flask import Flask, jsonify, request
from flask_cors import CORS
import sys

DEEP_TRANSLATOR_IMPORT_ERROR = None

# ✅ ADDED (for quiz generation)
import os
import json
import random
import re
import requests
from dotenv import load_dotenv

app = Flask(__name__)
CORS(app)

# ✅ ADDED: load backend/.env (do NOT commit .env)
load_dotenv()
# Also load frontend/.env as fallback for shared public config (Supabase URL/anon key).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "frontend", ".env"), override=False)

LANGUAGE_CODE_MAP = {
    "English": "en",
    "Tamil": "ta",
    "Malay": "ms",
    "Chinese": "zh-CN",
}

def get_google_translator_class():
    global DEEP_TRANSLATOR_IMPORT_ERROR

    try:
        from deep_translator import GoogleTranslator
        DEEP_TRANSLATOR_IMPORT_ERROR = None
        return GoogleTranslator
    except Exception as exc:
        DEEP_TRANSLATOR_IMPORT_ERROR = repr(exc)
        raise RuntimeError(f"deep_translator import failed: {exc}") from exc

def translate_with_deep_translator(text: str, target_language: str) -> str:
    GoogleTranslator = get_google_translator_class()

    try:
        return GoogleTranslator(source="auto", target=target_language).translate(text)
    except Exception as exc:
        raise RuntimeError(f"deep_translator error: {exc}") from exc

@app.get("/health")
def health():
    deep_translator_available = False
    try:
        get_google_translator_class()
        deep_translator_available = True
    except Exception:
        deep_translator_available = False

    return jsonify(
        status="hello",
        pythonExecutable=sys.executable,
        deepTranslatorAvailable=deep_translator_available,
        deepTranslatorImportError=DEEP_TRANSLATOR_IMPORT_ERROR,
    )


@app.post("/translate")
def translate_texts():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text")
    target_language = payload.get("targetLanguage", "English")

    if not isinstance(text, str):
        return jsonify(error="'text' must be a string."), 400

    normalized = text.strip()
    if not normalized:
        return jsonify(translatedText=text)

    language_code = LANGUAGE_CODE_MAP.get(target_language)
    if not language_code:
        return jsonify(error="Unsupported targetLanguage."), 400

    if language_code == "en":
        return jsonify(translatedText=text)

    try:
        translated = translate_with_deep_translator(text, language_code)
        return jsonify(
            translatedText=translated,
            translationStatus="ok",
            translationProvider="deep_translator",
        )
    except Exception as deep_exc:
        return jsonify(
            translatedText=text,
            translationStatus="down",
            userMessage="Translation is currently down. Your content is still available in English, and we will restore translation shortly.",
            technicalError=f"DeepTranslator={deep_exc}",
        )


# =========================
# ✅ ADDED: QUIZ GENERATION
# =========================
def _call_sealion_chat(messages, *, temperature=0.4):

    api_key = os.getenv("SEALION_API_KEY")
    base_url = os.getenv("SEALION_BASE_URL")
    model = os.getenv("SEALION_MODEL")

    if not api_key or not base_url or not model:
        raise RuntimeError(
            "Missing SEALION_API_KEY / SEALION_BASE_URL / SEALION_MODEL in backend/.env"
        )

    url = base_url.rstrip("/") + "/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)

    if resp.status_code >= 400:
        raise RuntimeError(f"SeaLion API error {resp.status_code}: {resp.text}")

    data = resp.json()

    # OpenAI-style parsing, with fallback for responses that place the payload in reasoning_content.
    message = data["choices"][0]["message"]
    content = message.get("content") or message.get("reasoning_content")
    if not content:
        raise RuntimeError(f"SeaLion response missing content: {data}")
    return content


def _call_checklist_sealion_chat(messages, *, temperature=0.4):
    api_key = os.getenv("SEALION_API_KEY")
    base_url = os.getenv("SEALION_BASE_URL")
    model = (os.getenv("CHECKLISTSEALION_MODEL") or os.getenv("SEALION_MODEL") or "").strip()

    if not api_key or not base_url or not model:
        raise RuntimeError(
            "Missing SEALION_API_KEY / SEALION_BASE_URL / CHECKLISTSEALION_MODEL (or SEALION_MODEL fallback) in backend/.env"
        )

    url = base_url.rstrip("/") + "/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)

    if resp.status_code >= 400:
        raise RuntimeError(f"SeaLion API error {resp.status_code}: {resp.text}")

    data = resp.json()
    message = data["choices"][0]["message"]
    content = message.get("content") or message.get("reasoning_content")
    if not content:
        raise RuntimeError(f"SeaLion response missing content: {data}")
    return content


def generate_quiz_from_steps(*, category: str, title: str, steps: list, num_questions: int):
    # Build compact SOP text for prompt
    sop_lines = []
    for s in steps:
        step_no = s.get("step_no", "")
        short = (s.get("step_short") or "").strip()
        desc = (s.get("step_description") or "").strip()
        sop_lines.append(f"Step {step_no}: {short} - {desc}".strip())
    sop_text = "\n".join(sop_lines)

    system_msg = (
        "You generate training quizzes for security SOP. "
        "Use ONLY the SOP steps provided. Do not invent policies. "
        "Return strictly valid JSON only (no markdown, no extra text)."
    )

    user_msg = f"""
Create a multiple-choice quiz.

Category: {category}
Title: {title}

SOP Steps:
{sop_text}

Rules:
- Make exactly {num_questions} questions.
- Each question must have exactly 4 choices.
- Only one correct answer.
- answerIndex must be 0, 1, 2, or 3.
- explanation must briefly justify the correct answer based ONLY on the SOP Steps.
- Output JSON in exactly this shape:
{{
  "questions": [
    {{
      "id": "q1",
      "question": "…",
      "choices": ["…", "…", "…", "…"],
      "answerIndex": 0,
      "explanation": "…"
    }}
  ]
}}
""".strip()

    raw = _call_sealion_chat(
        [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
    )

    quiz = json.loads(raw)

    questions = quiz.get("questions")
    if not isinstance(questions, list) or len(questions) == 0:
        raise RuntimeError("AI returned invalid quiz JSON (missing questions list).")

    # Validate + normalize
    normalized_questions = []
    for i, q in enumerate(questions):
        qid = q.get("id") or f"q{i+1}"
        question = q.get("question")
        choices = q.get("choices")
        answer_index = q.get("answerIndex")
        explanation = q.get("explanation", "")

        if not isinstance(question, str) or not question.strip():
            raise RuntimeError("Invalid question text from AI.")
        if not isinstance(choices, list) or len(choices) != 4 or not all(
            isinstance(c, str) for c in choices
        ):
            raise RuntimeError("Each question must have exactly 4 string choices.")
        if answer_index not in [0, 1, 2, 3]:
            raise RuntimeError("answerIndex must be 0..3.")
        if not isinstance(explanation, str):
            explanation = str(explanation)

        cleaned_choices = [c.strip() for c in choices]
        correct_choice = cleaned_choices[int(answer_index)]

        shuffled_choices = cleaned_choices[:]
        random.shuffle(shuffled_choices)
        shuffled_answer_index = shuffled_choices.index(correct_choice)

        normalized_questions.append(
            {
                "id": str(qid),
                "question": question.strip(),
                "choices": shuffled_choices,
                "answerIndex": shuffled_answer_index,
                "explanation": explanation.strip(),
            }
        )

    return {"questions": normalized_questions}

def _group_steps_by_doc(sop_rows: list[dict]) -> list[dict]:
    """
    Groups raw SOP rows (across all categories/titles) into 'documents'
    shaped like: {category, title, steps:[{step_no, step_short, step_description}, ...]}
    """
    docs_map = {}

    for row in sop_rows:
        category = str(row.get("category") or "").strip()
        title = str(row.get("title") or "").strip()
        step_no = row.get("step_no")
        step_short = row.get("step_short")
        step_description = row.get("step_description")

        if not category or not title:
            continue

        key = (category, title)
        if key not in docs_map:
            docs_map[key] = {
                "category": category,
                "title": title,
                "steps": [],
            }

        docs_map[key]["steps"].append(
            {
                "step_no": step_no,
                "step_short": step_short,
                "step_description": step_description,
            }
        )

    # sort steps in each doc
    docs = list(docs_map.values())
    for d in docs:
        d["steps"].sort(key=lambda s: _safe_step_no(s.get("step_no")))

    # stable order
    docs.sort(key=lambda d: (d["category"].lower(), d["title"].lower()))
    return docs


@app.post("/quiz/generate")
def quiz_generate():
    payload = request.get_json(silent=True) or {}

    category = payload.get("category", "")
    title = payload.get("title", "")
    steps = payload.get("steps", [])
    num_questions = payload.get("num_questions", 5)

    if not isinstance(category, str) or not category.strip():
        return jsonify(error="'category' must be a string."), 400

    if not isinstance(title, str) or not title.strip():
        return jsonify(error="'title' must be a string."), 400

    if not isinstance(steps, list) or len(steps) == 0:
        return jsonify(error="'steps' must be a non-empty array."), 400

    if not isinstance(num_questions, int) or num_questions < 1 or num_questions > 15:
        return jsonify(error="'num_questions' must be an int between 1 and 15."), 400

    try:
        quiz = generate_quiz_from_steps(
            category=category.strip(),
            title=title.strip(),
            steps=steps,
            num_questions=num_questions,
        )
    except json.JSONDecodeError:
        return jsonify(error="AI returned non-JSON text. Adjust prompt or retry."), 502
    except Exception as exc:
        return jsonify(error=f"Quiz generation failed: {exc}"), 500

    return jsonify(quiz)

@app.post("/quiz/generate-mixed")
def quiz_generate_mixed():
    payload = request.get_json(silent=True) or {}
    num_questions = payload.get("num_questions", 15)

    if not isinstance(num_questions, int) or num_questions < 1 or num_questions > 15:
        return jsonify(error="'num_questions' must be an int between 1 and 15."), 400

    # How many SOP documents to mix (category+title groups)
    docs_to_mix = payload.get("docs_to_mix", 6)
    if not isinstance(docs_to_mix, int) or docs_to_mix < 2 or docs_to_mix > 20:
        return jsonify(error="'docs_to_mix' must be an int between 2 and 20."), 400

    try:
        # ✅ Backend fetches SOP directly (recommended)
        sop_rows = _fetch_sop_rows()

        docs = _group_steps_by_doc(sop_rows)
        if not docs:
            return jsonify(error="No SOP documents found."), 500

        # Prefer docs that have some usable steps
        valid_docs = [d for d in docs if isinstance(d.get("steps"), list) and len(d["steps"]) > 0]
        if len(valid_docs) < 2:
            return jsonify(error="Not enough SOP documents to generate mixed test."), 500

        chosen_docs = random.sample(valid_docs, k=min(docs_to_mix, len(valid_docs)))

        mixed_steps = []
        for d in chosen_docs:
            mixed_steps.extend(d["steps"])

        if len(mixed_steps) == 0:
            return jsonify(error="Selected SOP documents had no steps."), 500

        # ✅ Reuse same generator so response format matches old quiz
        quiz = generate_quiz_from_steps(
            category="SOP Test",
            title="Mixed Categories",
            steps=mixed_steps,
            num_questions=num_questions,
        )
        return jsonify(quiz)

    except json.JSONDecodeError:
        return jsonify(error="AI returned non-JSON text. Adjust prompt or retry."), 502
    except Exception as exc:
        return jsonify(error=f"Mixed quiz generation failed: {exc}"), 500


# =========================
# ✅ ADDED: INCIDENT CHECKLIST RAG
# =========================
WORD_RE = re.compile(r"[a-z0-9][a-z0-9&/-]*")
STOP_WORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "with", "from", "by",
    "is", "are", "was", "were", "be", "this", "that", "it", "as", "you", "your", "into",
}
INCIDENT_CATEGORY_ALIASES = {
    "fire & evacuation": ["fire", "smoke", "alarm", "evacuation", "burning", "heat"],
    "suspicious person": ["suspicious", "loiter", "erratic", "impaired", "unwell", "threatening"],
    "suspicious item": ["bag", "package", "unattended", "unknown item", "suspicious item"],
    "medical": ["injury", "bleeding", "collapse", "medical", "faint", "unconscious"],
    "bomb threat": ["bomb", "threat", "explosive", "device", "evacuate"],
    "violence": ["fight", "assault", "violence", "weapon", "attack"],
    "robbery": ["robbery", "theft", "snatch", "steal", "cash"],
    "lift alarm": ["lift", "elevator", "trapped", "stuck"],
}
TARGET_EARLY_COUNT = 3
TARGET_SOP_COUNT = 12
MAX_RETRIEVED_ROWS = 40
RETRIEVAL_PREVIEW_ROWS = 16
MAX_LLM_DOC_ROWS = 28
MAX_DESC_CHARS = 140
ROLE_EXCLUSION_TERMS = (
    "debrief",
    "fitness for duty",
    "screening",
    "audit",
    "after-action",
    "post-incident",
    "command post",
    "facility manager",
    "soc",
    "blueprint",
    "paperwork",
    "complete report",
    "use of force report",
)
BACKUP_TERMS = ("request backup", "call backup", "dispatch backup", "backup units")
HIGH_RISK_TERMS = (
    "weapon",
    "gun",
    "knife",
    "bomb",
    "explosive",
    "active shooter",
    "multiple assailant",
    "hostage",
    "fire",
)
WEAPON_TERMS = ("gun", "gunfire", "firearm", "rifle", "pistol", "shotgun", "ammo", "code silver", "active shooter", "pop-pop")
EXPLOSIVE_TERMS = ("bomb", "explosive", "ied", "secondary device", "secondary devices", "detonation")
BIOHAZARD_TERMS = ("biological exposure", "pathogen protocol", "decontamination protocol", "hazmat", "bloodborne")
MASS_CASUALTY_TERMS = ("cold zone", "hot zone", "warm zone", "casualty extraction")
OFF_DOMAIN_TERMS = (
    "animal",
    "livestock",
    "farm",
    "safe-room key",
    "panic button",
    "protected employee",
    "vehicle relocation",
    "phone trace",
    "telecom",
    "rooftop watch",
    "rtf",
    "rescue task force",
)
ALLOWED_PHASES = {"en_route", "on_scene", "stabilize"}


def _normalize_text(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _tokenize(value: str) -> set[str]:
    tokens = set()
    for token in WORD_RE.findall(_normalize_text(value)):
        if token in STOP_WORDS or len(token) <= 1:
            continue
        tokens.add(token)
    return tokens


def _is_yes(value: str) -> bool:
    return _normalize_text(str(value)) in {"yes", "y", "true", "1"}


def _safe_step_no(value) -> int:
    try:
        return int(value)
    except Exception:
        return 999


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    text = _normalize_text(value)
    return any(term in text for term in terms)


def _is_high_risk_incident(incident_payload: dict) -> bool:
    context = " ".join(
        str(incident_payload.get(key) or "")
        for key in [
            "incident_category",
            "location_description",
            "ai_assessment",
            "officer_observation",
        ]
    )
    return _contains_any(context, HIGH_RISK_TERMS)


def _incident_context_text(incident_payload: dict) -> str:
    return " ".join(
        str(incident_payload.get(key) or "")
        for key in [
            "incident_category",
            "location_name",
            "location_unit_no",
            "location_description",
            "ai_assessment",
            "officer_observation",
        ]
    )


def _build_disallowed_threat_terms(incident_payload: dict) -> tuple[str, ...]:
    context = _normalize_text(_incident_context_text(incident_payload))
    disallowed = []
    if not _contains_any(context, WEAPON_TERMS):
        disallowed.extend(WEAPON_TERMS)
    if not _contains_any(context, EXPLOSIVE_TERMS):
        disallowed.extend(EXPLOSIVE_TERMS)
    if not _contains_any(context, BIOHAZARD_TERMS):
        disallowed.extend(BIOHAZARD_TERMS)
    if not _contains_any(context, MASS_CASUALTY_TERMS):
        disallowed.extend(MASS_CASUALTY_TERMS)
    return tuple(sorted(set(disallowed)))


def _normalize_phase(value: str, default_phase: str) -> str:
    candidate = _normalize_text(value)
    if candidate in ALLOWED_PHASES:
        return candidate
    return default_phase


def _to_action_item(raw, default_phase: str) -> dict | None:
    if isinstance(raw, str):
        text = " ".join(raw.split()).strip()
        if not text:
            return None
        return {"text": text, "phase": default_phase}
    if isinstance(raw, dict):
        text = " ".join(str(raw.get("text") or "").split()).strip()
        if not text:
            return None
        return {
            "text": text,
            "phase": _normalize_phase(str(raw.get("phase") or default_phase), default_phase),
        }
    return None


def _parse_action_items(raw_items, default_phase: str) -> list[dict]:
    if not isinstance(raw_items, list):
        return []
    parsed = []
    for item in raw_items:
        normalized = _to_action_item(item, default_phase)
        if normalized:
            parsed.append(normalized)
    return parsed


def _supabase_rest_config():
    base_url = (
        os.getenv("SUPABASE_URL")
        or os.getenv("EXPO_PUBLIC_SUPABASE_URL")
        or ""
    ).strip()
    api_key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
        or ""
    ).strip()

    if not base_url or not api_key:
        raise RuntimeError(
            "Missing Supabase env vars. Set SUPABASE_URL and SUPABASE_ANON_KEY (or SUPABASE_SERVICE_ROLE_KEY)."
        )

    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    return base_url.rstrip("/"), headers


def _fetch_sop_rows() -> list[dict]:
    base_url, headers = _supabase_rest_config()
    url = f"{base_url}/rest/v1/sop"
    params = {
        "select": "id,sop_id,category,title,step_no,step_title,step_description,step_short,requires_action",
        "order": "category.asc,title.asc,step_no.asc",
        "limit": "3000",
    }
    response = requests.get(url, headers=headers, params=params, timeout=45)
    if response.status_code >= 400:
        raise RuntimeError(f"SOP query failed ({response.status_code}): {response.text}")

    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("SOP query returned invalid response.")
    return data


def _score_sop_row(row: dict, incident_tokens: set[str], incident_text: str, incident_category: str) -> float:
    category = _normalize_text(str(row.get("category") or ""))
    title = _normalize_text(str(row.get("title") or ""))
    step_title = _normalize_text(str(row.get("step_title") or ""))
    step_short = _normalize_text(str(row.get("step_short") or ""))
    step_description = _normalize_text(str(row.get("step_description") or ""))

    score = 0.0
    if incident_category and (incident_category in category or category in incident_category):
        score += 12.0

    for alias in INCIDENT_CATEGORY_ALIASES.get(category, []):
        if alias in incident_text:
            score += 2.0

    row_tokens = _tokenize(f"{category} {title} {step_title} {step_short} {step_description}")
    score += len(incident_tokens.intersection(row_tokens)) * 1.15

    if _is_yes(str(row.get("requires_action") or "")):
        score += 1.4

    step_no = _safe_step_no(row.get("step_no"))
    if step_no <= 3:
        score += 1.2
    elif step_no <= 6:
        score += 0.6

    if any(kw in step_title for kw in ["verify", "scan", "acknowledge", "evacuat", "secure", "assess"]):
        score += 0.8

    return score


def _format_checklist_item(row: dict) -> str:
    step_short = (row.get("step_short") or "").strip()
    step_title = (row.get("step_title") or "").strip()
    desc = (row.get("step_description") or "").strip()

    lead = step_short or step_title or "Respond"
    if desc:
        sentence = f"{lead} - {desc}"
    else:
        sentence = lead
    return " ".join(sentence.split())


def _rank_and_retrieve_sop_rows(incident_payload: dict, sop_rows: list[dict]) -> tuple[list[dict], dict]:
    incident_category = _normalize_text(str(incident_payload.get("incident_category") or ""))
    incident_text = _normalize_text(
        " ".join(
            str(incident_payload.get(key) or "")
            for key in [
                "incident_category",
                "location_name",
                "location_unit_no",
                "location_description",
                "ai_assessment",
                "officer_observation",
            ]
        )
    )
    incident_tokens = _tokenize(incident_text)

    ranked = []
    for row in sop_rows:
        score = _score_sop_row(row, incident_tokens, incident_text, incident_category)
        ranked.append((score, row))

    ranked.sort(key=lambda item: item[0], reverse=True)
    top_ranked = [(score, row) for score, row in ranked if score > 0][:MAX_RETRIEVED_ROWS]

    if not top_ranked:
        category_rows = [
            row for row in sop_rows
            if incident_category and (
                incident_category in _normalize_text(str(row.get("category") or ""))
                or _normalize_text(str(row.get("category") or "")) in incident_category
            )
        ]
        fallback_rows = category_rows or sop_rows
        top_ranked = [(0.1, row) for row in fallback_rows[:MAX_RETRIEVED_ROWS]]

    doc_scores = {}
    for score, row in top_ranked:
        key = (
            _normalize_text(str(row.get("category") or "")),
            _normalize_text(str(row.get("title") or "")),
        )
        doc_scores[key] = doc_scores.get(key, 0.0) + score

    primary_doc = max(doc_scores.items(), key=lambda item: item[1])[0] if doc_scores else ("", "")
    primary_doc_rows = [
        row for _, row in top_ranked
        if (
            _normalize_text(str(row.get("category") or "")),
            _normalize_text(str(row.get("title") or "")),
        ) == primary_doc
    ]
    representative_row = primary_doc_rows[0] if primary_doc_rows else {}
    primary_category_display = str(representative_row.get("category") or primary_doc[0] or "").strip()
    primary_title_display = str(representative_row.get("title") or primary_doc[1] or "").strip()

    primary_rows = [
        row for _, row in top_ranked
        if (
            _normalize_text(str(row.get("category") or "")),
            _normalize_text(str(row.get("title") or "")),
        ) == primary_doc
    ]

    primary_category = primary_doc[0]
    same_category_rows = [
        row for _, row in top_ranked
        if _normalize_text(str(row.get("category") or "")) == primary_category
    ]
    remaining_rows = [
        row for _, row in top_ranked
        if _normalize_text(str(row.get("category") or "")) != primary_category
    ]

    merged_rows = []
    seen_ids = set()
    for row in primary_rows + same_category_rows + remaining_rows:
        row_id = str(row.get("id") or f"{row.get('sop_id')}:{row.get('step_no')}")
        if row_id in seen_ids:
            continue
        seen_ids.add(row_id)
        merged_rows.append(row)
        if len(merged_rows) >= MAX_RETRIEVED_ROWS:
            break

    metadata = {
        "primary_category": primary_category_display,
        "primary_title": primary_title_display,
        "primary_category_norm": primary_doc[0],
        "primary_title_norm": primary_doc[1],
        "retrieved_count": len(merged_rows),
    }
    return merged_rows, metadata


def _select_primary_doc_rows(sop_rows: list[dict], metadata: dict, fallback_rows: list[dict]) -> list[dict]:
    primary_category_norm = _normalize_text(str(metadata.get("primary_category_norm") or ""))
    primary_title_norm = _normalize_text(str(metadata.get("primary_title_norm") or ""))
    if not primary_category_norm or not primary_title_norm:
        return fallback_rows[:MAX_LLM_DOC_ROWS]

    doc_rows = [
        row for row in sop_rows
        if _normalize_text(str(row.get("category") or "")) == primary_category_norm
        and _normalize_text(str(row.get("title") or "")) == primary_title_norm
    ]
    if not doc_rows:
        return fallback_rows[:MAX_LLM_DOC_ROWS]

    doc_rows_sorted = sorted(
        doc_rows,
        key=lambda row: (_safe_step_no(row.get("step_no")), str(row.get("id") or "")),
    )
    return doc_rows_sorted[:MAX_LLM_DOC_ROWS]


def _compact_row_for_llm(row: dict) -> dict:
    desc = " ".join(str(row.get("step_description") or "").split()).strip()
    if len(desc) > MAX_DESC_CHARS:
        desc = f"{desc[:MAX_DESC_CHARS].rstrip()}..."
    return {
        "id": row.get("id"),
        "step_no": row.get("step_no"),
        "step_title": str(row.get("step_title") or "").strip(),
        "step_short": str(row.get("step_short") or "").strip(),
        "requires_action": str(row.get("requires_action") or "").strip(),
        "step_description": desc,
    }


def _fallback_checklists(retrieved_rows: list[dict]) -> tuple[list[str], list[str]]:
    if not retrieved_rows:
        return (
            [
                "Acknowledge response via radio and request nearby support.",
                "Perform a rapid visual risk scan before entry.",
                "Secure the immediate area and maintain safe access routes.",
            ],
            [
                "Preserve scene integrity and restrict unnecessary movement.",
                "Gather witness details and timeline updates.",
                "Escalate critical risks to supervisor immediately.",
                "Maintain clear radio updates with incident location and risk changes.",
                "Control crowd movement and prevent bystander interference.",
                "Request medical or specialist support when risk indicators escalate.",
                "Protect potential evidence and avoid unnecessary scene contamination.",
                "Separate key witnesses and capture immediate observations.",
                "Coordinate perimeter control and safe access routes.",
                "Reassess hazard level continuously and update supervisor.",
                "Prepare concise handover notes for incoming responders.",
                "Confirm stabilization status before transitioning to report stage.",
            ],
        )

    sorted_rows = sorted(
        retrieved_rows,
        key=lambda row: (_safe_step_no(row.get("step_no")), _normalize_text(str(row.get("title") or ""))),
    )
    actionable_rows = [row for row in sorted_rows if _is_yes(str(row.get("requires_action") or ""))]
    early_source = actionable_rows[:4] if actionable_rows else sorted_rows[:4]

    early = []
    seen = set()
    for row in early_source:
        item = _format_checklist_item(row)
        key = _normalize_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        early.append(item)
        if len(early) >= 3:
            break

    sop = []
    for row in sorted_rows:
        item = _format_checklist_item(row)
        key = _normalize_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        sop.append(item)
        if len(sop) >= 9:
            break

    return early, sop


def _log_pretty_json(tag: str, payload):
    try:
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        pretty = str(payload)
    app.logger.info("\n%s\n%s", tag, pretty)


def _dedupe_action_items(items: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in items:
        text = " ".join(str(item.get("text") or "").split()).strip()
        if not text:
            continue
        key = _normalize_text(text)
        if key in seen:
            continue
        seen.add(key)
        result.append({"text": text, "phase": _normalize_phase(str(item.get("phase") or "on_scene"), "on_scene")})
    return result


def _row_to_action_item(row: dict, phase: str) -> dict:
    return {
        "text": _format_checklist_item(row),
        "phase": _normalize_phase(phase, "on_scene"),
    }


def _candidate_actions_from_rows(rows: list[dict], *, early: bool, incident_payload: dict) -> list[dict]:
    incident_category = _normalize_text(str(incident_payload.get("incident_category") or ""))
    incident_text = _normalize_text(_incident_context_text(incident_payload))
    incident_tokens = _tokenize(incident_text)

    scored = []
    for row in rows:
        score = _score_sop_row(row, incident_tokens, incident_text, incident_category)
        requires_action = 0 if _is_yes(str(row.get("requires_action") or "")) else 1
        step_no = _safe_step_no(row.get("step_no"))
        if early:
            sort_key = (requires_action, step_no, -score)
            phase = "en_route"
        else:
            sort_key = (step_no, requires_action, -score)
            phase = "on_scene"
        scored.append((sort_key, _row_to_action_item(row, phase)))
    scored.sort(key=lambda item: item[0])
    return _dedupe_action_items([item for _, item in scored])


def _is_ground_action_allowed(item_text: str, *, is_early: bool, allow_early_backup: bool) -> bool:
    text = _normalize_text(item_text)
    if _contains_any(text, ROLE_EXCLUSION_TERMS):
        return False
    if is_early and not allow_early_backup and _contains_any(text, BACKUP_TERMS):
        return False
    return True


def _is_off_domain_item(item_text: str, *, disallowed_terms: tuple[str, ...]) -> bool:
    text = _normalize_text(item_text)
    if _contains_any(text, OFF_DOMAIN_TERMS):
        return True
    if _contains_any(text, disallowed_terms):
        return True
    return False


def _build_grounding_tokens(incident_payload: dict, doc_rows: list[dict]) -> set[str]:
    source_parts = [_incident_context_text(incident_payload)]
    for row in doc_rows:
        source_parts.append(
            " ".join(
                str(row.get(key) or "")
                for key in ["step_title", "step_short", "step_description"]
            )
        )
    return _tokenize(" ".join(source_parts))


def _is_grounded_item(item_text: str, source_tokens: set[str]) -> bool:
    tokens = _tokenize(item_text)
    if not tokens:
        return False
    return True


def _repair_action_list(
    *,
    generated: list[dict],
    doc_candidates: list[dict],
    target_count: int,
    is_early: bool,
    allow_early_backup: bool,
    disallowed_terms: tuple[str, ...],
    grounding_tokens: set[str],
    default_phase: str,
) -> list[dict]:
    repaired = []
    used = set()

    for item in _dedupe_action_items(generated):
        text = item["text"]
        if not _is_ground_action_allowed(text, is_early=is_early, allow_early_backup=allow_early_backup):
            continue
        if _is_off_domain_item(text, disallowed_terms=disallowed_terms):
            continue
        if not _is_grounded_item(text, grounding_tokens):
            continue
        normalized = _normalize_text(text)
        if normalized in used:
            continue
        used.add(normalized)
        repaired.append({"text": text, "phase": _normalize_phase(item.get("phase", default_phase), default_phase)})
        if len(repaired) >= target_count:
            return repaired

    for candidate in doc_candidates:
        text = candidate["text"]
        normalized = _normalize_text(text)
        if normalized in used:
            continue
        if not _is_ground_action_allowed(text, is_early=is_early, allow_early_backup=allow_early_backup):
            continue
        if _is_off_domain_item(text, disallowed_terms=disallowed_terms):
            continue
        if not _is_grounded_item(text, grounding_tokens):
            continue
        used.add(normalized)
        repaired.append({"text": text, "phase": default_phase})
        if len(repaired) >= target_count:
            return repaired

    return repaired[:target_count]


def _vet_checklists_with_sealion(
    *,
    incident_payload: dict,
    doc_rows_compact: list[dict],
    draft_early: list[dict],
    draft_sop: list[dict],
) -> tuple[list[dict], list[dict]] | None:
    system_msg = (
        "You are a mall security C2 checklist editor. "
        "Rewrite actions for clarity and practical execution by ground security officers. "
        "Do not invent threats, objects, or protocols not present in the provided incident context and SOP rows. "
        "Return strict JSON only."
    )
    user_msg = f"""
Incident context:
{json.dumps(incident_payload, ensure_ascii=False)}

Selected SOP document rows:
{json.dumps(doc_rows_compact, ensure_ascii=False)}

Draft checklist:
{json.dumps({"early_checklist": draft_early, "sop_checklist": draft_sop}, ensure_ascii=False)}

Output JSON shape:
{{
  "early_checklist": [{{"text":"...", "phase":"en_route"}}],
  "sop_checklist": [{{"text":"...", "phase":"on_scene"}}]
}}

Rules:
- Keep exactly {TARGET_EARLY_COUNT} early actions with phase "en_route".
- Keep exactly {TARGET_SOP_COUNT} SOP actions with phase "on_scene" or "stabilize".
- This is mall security response; not police special operations, not telecom, not farm/animal handling.
- Do not include: animals, protected employee programs, safe-room keys, phone trace, RTF, rooftop watch.
- No supervisor/admin/post-incident actions.
- Keep language simple, direct, and field-executable.
""".strip()

    try:
        raw = _call_checklist_sealion_chat(
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
        )
        parsed = json.loads(raw)
        early = _parse_action_items(parsed.get("early_checklist", []), "en_route")
        sop = _parse_action_items(parsed.get("sop_checklist", []), "on_scene")
        if len(early) < TARGET_EARLY_COUNT or len(sop) < max(6, TARGET_SOP_COUNT // 2):
            return None
        return early, sop
    except Exception:
        return None


def _polish_checklists_with_sealion(
    *,
    incident_payload: dict,
    early_actions: list[dict],
    sop_actions: list[dict],
) -> tuple[list[dict], list[dict]] | None:
    system_msg = (
        "You polish wording only. Keep meaning and order. "
        "Do not add or remove actions. Return strict JSON only."
    )
    user_msg = f"""
Incident context:
{json.dumps(incident_payload, ensure_ascii=False)}

Checklist to polish:
{json.dumps({"early_checklist": early_actions, "sop_checklist": sop_actions}, ensure_ascii=False)}

Return the same count and same phases:
{{
  "early_checklist": [{{"text":"...", "phase":"en_route"}}],
  "sop_checklist": [{{"text":"...", "phase":"on_scene"}}]
}}
""".strip()
    try:
        raw = _call_checklist_sealion_chat(
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
        )
        parsed = json.loads(raw)
        early = _parse_action_items(parsed.get("early_checklist", []), "en_route")
        sop = _parse_action_items(parsed.get("sop_checklist", []), "on_scene")
        if len(early) != len(early_actions) or len(sop) != len(sop_actions):
            return None
        return early, sop
    except Exception:
        return None


def generate_incident_checklists(incident_payload: dict):
    sop_rows = _fetch_sop_rows()

    # Phase 1: retrieve + rank to choose one best SOP document
    retrieved_rows, metadata = _rank_and_retrieve_sop_rows(incident_payload, sop_rows)
    doc_rows = _select_primary_doc_rows(sop_rows, metadata, retrieved_rows)
    fallback_early_raw, fallback_sop_raw = _fallback_checklists(doc_rows)

    doc_rows_compact = [_compact_row_for_llm(row) for row in doc_rows]
    retrieval_preview = doc_rows_compact[:RETRIEVAL_PREVIEW_ROWS]
    _log_pretty_json(
        "[incident/checklist] retrieval",
        {
            "retrieval": metadata,
            "selected_doc_rows_count": len(doc_rows),
            "retrieved_preview": retrieval_preview,
        },
    )

    # Phase 2: generate using only selected doc rows
    system_msg = (
        "You are a mall security C2 planner producing operational checklists for ground security officers. "
        "Use only the selected SOP document rows provided. Return strict JSON only."
    )
    user_msg = f"""
Incident context:
{json.dumps(incident_payload, ensure_ascii=False)}

Selected SOP document (single doc):
{{
  "category": {json.dumps(metadata.get("primary_category", ""), ensure_ascii=False)},
  "title": {json.dumps(metadata.get("primary_title", ""), ensure_ascii=False)}
}}

SOP rows (doc-only):
{json.dumps(doc_rows_compact, ensure_ascii=False)}

Output JSON shape:
{{
  "early_checklist": [{{"text":"...", "phase":"en_route"}}],
  "sop_checklist": [{{"text":"...", "phase":"on_scene"}}],
  "selected_sop": {{
    "category": "...",
    "title": "..."
  }}
}}

Rules:
- early_checklist is en-route mental checklist (2-3 minutes before arrival).
- sop_checklist is on-scene actionable sequence for ground officers.
- Keep exactly {TARGET_EARLY_COUNT} early items and {TARGET_SOP_COUNT} SOP items.
- This is mall security context only.
- Do not include: animals, protected employee programs, safe-room keys, phone trace, telecom, RTF, rooftop watch.
- No supervisor/admin/post-incident actions.
- Never introduce threat types or objects not in incident context or SOP rows.
- Keep actions simple, imperative, and practical.
""".strip()

    disallowed_terms = _build_disallowed_threat_terms(incident_payload)
    allow_early_backup = _is_high_risk_incident(incident_payload)
    grounding_tokens = _build_grounding_tokens(incident_payload, doc_rows)
    fallback_early = _parse_action_items(fallback_early_raw, "en_route")
    fallback_sop = _parse_action_items(fallback_sop_raw, "on_scene")
    doc_early_candidates = _candidate_actions_from_rows(doc_rows, early=True, incident_payload=incident_payload)
    doc_sop_candidates = _candidate_actions_from_rows(doc_rows, early=False, incident_payload=incident_payload)

    try:
        raw = _call_checklist_sealion_chat(
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
        parsed = json.loads(raw)
        early_generated = _parse_action_items(parsed.get("early_checklist", []), "en_route")
        sop_generated = _parse_action_items(parsed.get("sop_checklist", []), "on_scene")

        # Repair loop (replace bad lines, do not reject entire output)
        early_repaired = _repair_action_list(
            generated=early_generated + fallback_early,
            doc_candidates=doc_early_candidates,
            target_count=TARGET_EARLY_COUNT,
            is_early=True,
            allow_early_backup=allow_early_backup,
            disallowed_terms=disallowed_terms,
            grounding_tokens=grounding_tokens,
            default_phase="en_route",
        )
        sop_repaired = _repair_action_list(
            generated=sop_generated + fallback_sop,
            doc_candidates=doc_sop_candidates,
            target_count=TARGET_SOP_COUNT,
            is_early=False,
            allow_early_backup=True,
            disallowed_terms=disallowed_terms,
            grounding_tokens=grounding_tokens,
            default_phase="on_scene",
        )

        vetted = _vet_checklists_with_sealion(
            incident_payload=incident_payload,
            doc_rows_compact=doc_rows_compact,
            draft_early=early_repaired,
            draft_sop=sop_repaired,
        )
        if vetted:
            early_repaired, sop_repaired = vetted

        polished = _polish_checklists_with_sealion(
            incident_payload=incident_payload,
            early_actions=early_repaired,
            sop_actions=sop_repaired,
        )
        if polished:
            early_repaired, sop_repaired = polished

        # Final deterministic repair pass to enforce constraints/counts post-polish
        early_final = _repair_action_list(
            generated=early_repaired + fallback_early,
            doc_candidates=doc_early_candidates,
            target_count=TARGET_EARLY_COUNT,
            is_early=True,
            allow_early_backup=allow_early_backup,
            disallowed_terms=disallowed_terms,
            grounding_tokens=grounding_tokens,
            default_phase="en_route",
        )
        sop_final = _repair_action_list(
            generated=sop_repaired + fallback_sop,
            doc_candidates=doc_sop_candidates,
            target_count=TARGET_SOP_COUNT,
            is_early=False,
            allow_early_backup=True,
            disallowed_terms=disallowed_terms,
            grounding_tokens=grounding_tokens,
            default_phase="on_scene",
        )

        selected_sop = parsed.get("selected_sop") if isinstance(parsed.get("selected_sop"), dict) else {}
        result = {
            "early_checklist": early_final,
            "sop_checklist": sop_final,
            "selected_sop": {
                "category": str(selected_sop.get("category") or metadata.get("primary_category") or "").strip(),
                "title": str(selected_sop.get("title") or metadata.get("primary_title") or "").strip(),
            },
            "retrieval": {
                "primary_category": metadata.get("primary_category"),
                "primary_title": metadata.get("primary_title"),
                "retrieved_count": metadata.get("retrieved_count"),
                "doc_rows_count": len(doc_rows),
            },
            "generator": "sealion_doc_rag_repaired",
        }
        _log_pretty_json("[incident/checklist] generated", result)
        return result
    except Exception as exc:
        early_final = _repair_action_list(
            generated=fallback_early,
            doc_candidates=doc_early_candidates,
            target_count=TARGET_EARLY_COUNT,
            is_early=True,
            allow_early_backup=allow_early_backup,
            disallowed_terms=disallowed_terms,
            grounding_tokens=grounding_tokens,
            default_phase="en_route",
        )
        sop_final = _repair_action_list(
            generated=fallback_sop,
            doc_candidates=doc_sop_candidates,
            target_count=TARGET_SOP_COUNT,
            is_early=False,
            allow_early_backup=True,
            disallowed_terms=disallowed_terms,
            grounding_tokens=grounding_tokens,
            default_phase="on_scene",
        )
        fallback_result = {
            "early_checklist": early_final,
            "sop_checklist": sop_final,
            "selected_sop": {
                "category": metadata.get("primary_category", ""),
                "title": metadata.get("primary_title", ""),
            },
            "retrieval": {
                "primary_category": metadata.get("primary_category"),
                "primary_title": metadata.get("primary_title"),
                "retrieved_count": metadata.get("retrieved_count"),
                "doc_rows_count": len(doc_rows),
            },
            "generator": "deterministic_doc_repair_fallback",
            "fallback_reason": str(exc),
        }
        _log_pretty_json("[incident/checklist] generated_fallback", fallback_result)
        return fallback_result


def _normalize_report_type(value: str) -> str:
    normalized = _normalize_text(value)
    if normalized == "resolved":
        return "Resolved"
    return "Handover"


def _normalize_checked_actions(raw_items) -> list[str]:
    if not isinstance(raw_items, list):
        return []

    actions = []
    seen = set()
    for raw in raw_items:
        text = " ".join(str(raw or "").split()).strip()
        if not text:
            continue
        key = _normalize_text(text)
        if key in seen:
            continue
        seen.add(key)
        actions.append(text)
    return actions


def _fallback_incident_report_draft(
    *,
    incident_payload: dict,
    report_type: str,
    checked_actions: list[str],
) -> dict:
    category = str(incident_payload.get("incident_category") or "incident").strip()
    location_name = str(incident_payload.get("location_name") or "").strip()
    location_unit = str(incident_payload.get("location_unit_no") or "").strip()
    location_desc = str(incident_payload.get("location_description") or "").strip()
    location = " ".join(part for part in [location_name, location_unit, location_desc] if part).strip() or "assigned location"

    action_sentence = (
        "I carried out the following actions: " + "; ".join(checked_actions[:8]) + "."
        if checked_actions
        else "I responded according to SOP and completed scene checks based on the available indicators."
    )

    if report_type == "Resolved":
        incident_description = (
            f"I attended a {category} incident at {location}. "
            f"{action_sentence} "
            "After assessment and response, the immediate risk indicators were stabilized and the incident was resolved. "
            "Relevant updates were communicated to control and records were prepared for closure."
        )
        handover_instructions = (
            "Incident resolved. Continue routine monitoring of the area and escalate immediately if related indicators reappear."
        )
    else:
        incident_description = (
            f"I attended a {category} incident at {location}. "
            f"{action_sentence} "
            "The scene was managed to maintain safety and control while preserving key observations for continuity."
        )
        handover_instructions = (
            "Continue monitoring the location, maintain communication updates, and follow pending SOP actions until full stabilization."
        )

    return {
        "incident_description": " ".join(incident_description.split()),
        "handover_instructions": " ".join(handover_instructions.split()),
    }


def generate_incident_report_draft(
    *,
    incident_payload: dict,
    report_type: str,
    checked_actions: list[str],
    duty_officer_name: str,
    supervisor_name: str,
) -> dict:
    system_msg = (
        "You write formal incident reports in first-person perspective for mall security officers. "
        "Use only provided context. Do not fabricate facts, people, threats, or outcomes. "
        "Return strict JSON only."
    )
    user_msg = f"""
Report type:
{report_type}

Duty officer:
{duty_officer_name or "-"}

Supervisor:
{supervisor_name or "-"}

Incident context:
{json.dumps(incident_payload, ensure_ascii=False)}

Checklist actions the officer actually clicked/completed:
{json.dumps(checked_actions, ensure_ascii=False)}

Output JSON shape:
{{
  "incident_description": "...",
  "handover_instructions": "..."
}}

Rules:
- Write in first person ("I").
- Keep incident_description practical and professional for a security report.
- Explicitly incorporate relevant checklist actions as completed actions.
- If checklist actions are empty, state actions in conservative SOP terms without inventing specifics.
- For report_type "Resolved", describe closure status clearly.
- For report_type "Handover", provide actionable handover_instructions for the next officer.
- No markdown, no bullet points, no extra keys.
""".strip()

    raw = _call_checklist_sealion_chat(
        [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
    )
    parsed = json.loads(raw)

    incident_description = " ".join(str(parsed.get("incident_description") or "").split()).strip()
    handover_instructions = " ".join(str(parsed.get("handover_instructions") or "").split()).strip()

    if len(incident_description) < 40:
        raise RuntimeError("Generated incident_description is too short.")
    if report_type == "Handover" and len(handover_instructions) < 20:
        raise RuntimeError("Generated handover_instructions is too short.")

    return {
        "incident_description": incident_description,
        "handover_instructions": handover_instructions,
        "generator": "sealion_report_draft",
    }


@app.post("/incident/checklist/generate")
def incident_checklist_generate():
    payload = request.get_json(silent=True) or {}
    incident_payload = payload.get("incident")
    if not isinstance(incident_payload, dict):
        return jsonify(error="'incident' must be an object."), 400

    _log_pretty_json("[incident/checklist] request", {"incident": incident_payload})

    try:
        result = generate_incident_checklists(incident_payload)
    except Exception as exc:
        return jsonify(error=f"Checklist generation failed: {exc}"), 500

    _log_pretty_json("[incident/checklist] response", result)
    return jsonify(result)


@app.post("/incident/report/generate")
def incident_report_generate():
    payload = request.get_json(silent=True) or {}
    incident_payload = payload.get("incident")
    if not isinstance(incident_payload, dict):
        return jsonify(error="'incident' must be an object."), 400

    report_type = _normalize_report_type(str(payload.get("report_type") or "Handover"))
    checked_actions = _normalize_checked_actions(payload.get("checked_actions"))
    duty_officer_name = " ".join(str(payload.get("duty_officer_name") or "").split()).strip()
    supervisor_name = " ".join(str(payload.get("supervisor_name") or "").split()).strip()

    _log_pretty_json(
        "[incident/report] request",
        {
            "report_type": report_type,
            "incident": incident_payload,
            "checked_actions": checked_actions,
            "duty_officer_name": duty_officer_name,
            "supervisor_name": supervisor_name,
        },
    )

    try:
        result = generate_incident_report_draft(
            incident_payload=incident_payload,
            report_type=report_type,
            checked_actions=checked_actions,
            duty_officer_name=duty_officer_name,
            supervisor_name=supervisor_name,
        )
    except Exception as exc:
        result = _fallback_incident_report_draft(
            incident_payload=incident_payload,
            report_type=report_type,
            checked_actions=checked_actions,
        )
        result["generator"] = "deterministic_report_fallback"
        result["fallback_reason"] = str(exc)

    _log_pretty_json("[incident/report] response", result)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
