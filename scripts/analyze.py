"""
Trading Bot Analyzer — GitHub Actions script
Runs automatically when data.json changes.
Processes groups with status 'pending' or 'pendiente_final'.
"""

import os
import json
import sys
from pathlib import Path
from datetime import date
import requests
from anthropic import Anthropic

WORKER_URL = os.environ.get("WORKER_URL", "https://trading-upload.nestragues.workers.dev")
client = Anthropic()

# ── Data access ───────────────────────────────────────────────────────────────

def get_data():
    r = requests.get(f"{WORKER_URL}/data", timeout=30)
    r.raise_for_status()
    return r.json()

def put_data(data):
    payload = json.dumps(data, ensure_ascii=True).encode("utf-8")
    r = requests.put(
        f"{WORKER_URL}/data",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=60,
    )
    r.raise_for_status()

def read_file(folder, filename):
    path = Path("Archivos") / folder / filename
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    print(f"  [warn] File not found: {path}")
    return None

# ── Prompts ───────────────────────────────────────────────────────────────────

SCHEMA = """
{
  "category": "Type | N instruments | Platform | Strategy (short, use | as separator)",
  "summary": "2-3 dense sentences: what the file does, key technology, main metrics, current state.",
  "m1": {
    "type": "empty",
    "last_updated": "YYYY-MM-DD | filename",
    "empty_title": "Short title",
    "empty_desc": "Explanation of why no backtest results are available.",
    "empty_trigger": "filename.py"
  },
  "m2": [
    {
      "id": "R-01",
      "tipo": "param",
      "tipo_label": "Parametro",
      "prioridad": "alta",
      "estado": "pendiente",
      "title": "Concise title",
      "desc": "Detailed description of the problem and the proposed solution.",
      "comment": ""
    }
  ],
  "m3": [
    {
      "id": "OBS-001",
      "tipo": "warn",
      "origin": "filename.py",
      "title": "Observation title",
      "desc": "Detailed description.",
      "comment": ""
    }
  ],
  "m4": [
    {
      "id": "H-01",
      "categoria": "bug",
      "title": "Finding title",
      "desc": "Problem description.",
      "code": "relevant_code_here()",
      "fix": "proposed_fix_here()",
      "comment": ""
    }
  ]
}
"""

# Static system blocks — marked ephemeral so the API caches them for the run duration.
# Only the dynamic user message (files content, date) is sent uncached each call.

ANALYSIS_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are a senior quantitative trading systems analyst reviewing a Python trading bot.\n\n"
            "Analyze the file(s) provided and return a JSON object following this schema exactly.\n"
            "OUTPUT ONLY VALID JSON. No markdown fences, no text before or after the JSON object.\n"
            "Use only ASCII characters (no em-dashes, no special quotes, no accented vowels) — this is critical for JSON safety.\n"
            "All descriptive text must be in Spanish.\n\n"
            f"Schema:\n{SCHEMA}\n\n"
            "Rules:\n"
            "- m1: Only CSV/backtest results justify type \"quality\". If only .py files are present, use type \"empty\".\n"
            "- m2: 5-10 recommendations. tipo must be one of: param, logic, risk, data, meta. prioridad: alta/media/baja. estado always \"pendiente\". comment always \"\".\n"
            "- m3: 5-8 observations. tipo must be one of: warn, error, info. comment always \"\".\n"
            "- m4: 5-10 code findings. categoria must be one of: bug, riesgo, ausencia, mejora. Use \\n for line breaks inside code/fix strings. comment always \"\".\n"
            "- Recommendations in m2 ordered alta -> media -> baja.\n"
            "- m4 bugs and riesgos first, then ausencias, then mejoras."
        ),
        "cache_control": {"type": "ephemeral"},
    }
]

FINALIZE_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are finalizing a trading bot analysis after the trader reviewed the draft.\n\n"
            "CRITICAL: Every correction listed in the user message is a MANDATORY change that MUST be reflected in the final analysis.\n"
            "Do NOT ignore, soften, or partially apply corrections. Treat each one as a direct instruction from the trader.\n\n"
            "Instructions:\n"
            "- For EVERY card that received a correction: incorporate the feedback substantively — rewrite the title, desc, or fix as needed. "
            "Set the card's \"comment\" field to a short note summarizing what the trader said and what was adjusted.\n"
            "- If m1 received a correction, update the m1 block accordingly.\n"
            "- For cards with no correction, keep \"comment\" as \"\".\n"
            "- Do NOT change IDs or overall structure.\n"
            "- Remove the \"correction\" key from all cards and from m1 if present.\n"
            "- OUTPUT ONLY VALID JSON. No markdown fences.\n"
            "- Use only ASCII characters. All text in Spanish."
        ),
        "cache_control": {"type": "ephemeral"},
    }
]

TIPO_MAP = {
    "param": "Parametro",
    "logic": "Logica",
    "risk": "Riesgo",
    "data": "Datos",
    "meta": "Meta",
}


def build_analysis_user(files_text, date_str):
    return f"Today: {date_str}\n\nFiles:\n{files_text}"


def build_finalize_user(group):
    trader_notes = (group.get("trader_notes") or "").strip()

    corrections = {}
    for card in group.get("m2", []) + group.get("m3", []) + group.get("m4", []):
        c = (card.get("correction") or "").strip()
        if c:
            corrections[card["id"]] = c

    m1 = group.get("m1", {})
    m1_correction = (m1.get("correction") or "").strip()
    has_m1_correction = bool(m1_correction)

    current = {
        "m1": m1,
        "m2": group.get("m2", []),
        "m3": group.get("m3", []),
        "m4": group.get("m4", []),
    }

    output_structure = (
        '{"m1": {...}, "m2": [...], "m3": [...], "m4": [...]}'
        if has_m1_correction
        else '{"m2": [...], "m3": [...], "m4": [...]}'
    )

    return (
        f"Current analysis:\n{json.dumps(current, ensure_ascii=True, indent=2)}\n\n"
        f"Trader general notes: \"{trader_notes}\"\n\n"
        f"M1 correction: \"{m1_correction if m1_correction else 'None'}\"\n\n"
        f"Card-level corrections (card_id -> trader correction):\n"
        f"{json.dumps(corrections, ensure_ascii=True, indent=2) if corrections else 'None'}\n\n"
        f"Output structure: {output_structure}"
    )


# ── Claude API ────────────────────────────────────────────────────────────────

def call_claude(system_blocks, user_content):
    msg = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
        betas=["prompt-caching-2024-07-31"],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return json.loads(text)


# ── Processing ────────────────────────────────────────────────────────────────

def process_pending(group):
    print(f"  Generating analysis for {group['badge']} — {group['name']}")

    parts = []
    for f in group.get("files", []):
        content = read_file(group["folder"], f["name"])
        if content:
            parts.append(f"=== {f['name']} ===\n{content}")

    if not parts:
        print("  No readable files found — skipping")
        return False

    user_content = build_analysis_user("\n\n".join(parts), date.today().isoformat())
    analysis = call_claude(ANALYSIS_SYSTEM, user_content)

    group["status"] = "en_revision"
    group["category"] = analysis.get("category", "")
    group["summary"] = analysis.get("summary", "")
    group["m1"] = analysis.get("m1", group.get("m1", {}))
    group["m2"] = analysis.get("m2", [])
    group["m3"] = analysis.get("m3", [])
    group["m4"] = analysis.get("m4", [])
    group["trader_notes"] = ""
    group["revision_submitted"] = False
    group["rereview_requested"] = False
    group["rereview_notes"] = ""

    for card in group["m2"]:
        if not card.get("tipo_label"):
            card["tipo_label"] = TIPO_MAP.get(card.get("tipo", ""), card.get("tipo", ""))

    print(f"  -> {len(group['m2'])} recs | {len(group['m3'])} obs | {len(group['m4'])} findings")
    return True


def process_pendiente_final(group):
    print(f"  Finalizing {group['badge']} — {group['name']}")

    trader_notes = (group.get("trader_notes") or "").strip()
    m1_correction = (group.get("m1", {}).get("correction") or "").strip()
    has_corrections = m1_correction or any(
        (card.get("correction") or "").strip()
        for card in group.get("m2", []) + group.get("m3", []) + group.get("m4", [])
    )

    if trader_notes or has_corrections:
        updated = call_claude(FINALIZE_SYSTEM, build_finalize_user(group))
        if "m1" in updated:
            group["m1"] = updated["m1"]
        group["m2"] = updated.get("m2", group["m2"])
        group["m3"] = updated.get("m3", group["m3"])
        group["m4"] = updated.get("m4", group["m4"])
    else:
        print("  No corrections — approving as-is")

    group["status"] = "activo"
    group["trader_notes"] = ""
    group["revision_submitted"] = False
    group["rereview_requested"] = False
    group["rereview_notes"] = ""

    for card in group.get("m2", []) + group.get("m3", []) + group.get("m4", []):
        card.pop("correction", None)
    if isinstance(group.get("m1"), dict):
        group["m1"].pop("correction", None)

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Trading Bot Analyzer ===")

    data = get_data()
    groups = data.get("groups", [])

    pending = [g for g in groups if g.get("status") == "pending"]
    pendiente_final = [g for g in groups if g.get("status") == "pendiente_final"]

    if not pending and not pendiente_final:
        print("Nothing to process.")
        return

    changed = False
    had_error = False

    for g in pending:
        print(f"\n[PENDING] {g['badge']} — {g['name']}")
        try:
            if process_pending(g):
                changed = True
                print(f"  -> en_revision")
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            had_error = True

    for g in pendiente_final:
        print(f"\n[PENDIENTE_FINAL] {g['badge']} — {g['name']}")
        try:
            if process_pendiente_final(g):
                changed = True
                print(f"  -> activo")
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            had_error = True

    if changed:
        print("\nWriting to Worker...")
        put_data(data)
        print("Done.")
    else:
        print("No changes.")

    if had_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
