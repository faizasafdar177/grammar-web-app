from flask import Flask, render_template, request, send_file, url_for, jsonify
from docx import Document
from docx.shared import RGBColor
import requests
import os
import re
import json
from html import escape  # ✅ safe HTML escaping

app = Flask(__name__)
LT_API_URL = "https://api.languagetool.org/v2/check"

# ---------------------------------------------------------------------
# ✅ Load Black’s Law Dictionary once (UNCHANGED LOGIC)
# ---------------------------------------------------------------------
try:
    with open("blacklaw_terms.json", "r", encoding="utf-8") as f:
        BLACKLAW = json.load(f)
except Exception:
    BLACKLAW = {}


def normalize_key(word: str) -> str:
    """Normalize a word for lookup in BLACKLAW."""
    return re.sub(r"[^a-z\s]", "", word.lower().strip())


# ---------------------------------------------------------------------
# ✅ Legal fixes (UNCHANGED)
# ---------------------------------------------------------------------
LEGAL_FIX = {
    "prima facia": "prima facie",
    "mens reaa": "mens rea",
    "ratio decedendi": "ratio decidendi",
    "precedant": "precedent",
    "jurispridence": "jurisprudence",
    "suo moto": "suo motu",
}

# ---------------------------------------------------------------------
# ✅ Ignore citations (UNCHANGED)
# ---------------------------------------------------------------------
def is_reference_like(line: str) -> bool:
    s = line.strip()
    return (
        not s
        or "http" in s
        or "www." in s
        or "doi" in s.lower()
        or re.match(r"^\[\d+\]$", s)
        or re.match(r"^\(.+\d{4}.*\)$", s)
    )

# ---------------------------------------------------------------------
# ✅ LanguageTool filtering (UNCHANGED)
# ---------------------------------------------------------------------
def lt_check_sentence(sentence: str, lang: str = "en-US"):
    try:
        data = {"text": sentence, "language": lang}
        r = requests.post(LT_API_URL, data=data, timeout=10)
        r.raise_for_status()
        result = r.json()
    except Exception:
        return {"matches": []}

    clean_matches = []
    for m in result.get("matches", []):
        good_reps = []
        for rep in m.get("replacements", []):
            s = rep["value"].strip().lower()

            if abs(len(s) - m.get("length", 0)) > 3:
                continue
            if s in {"motor", "father", "furthermore"}:
                continue

            if s not in BLACKLAW:
                if not re.match(r"^[a-z]+$", s):
                    continue

            good_reps.append(rep)

        if good_reps:
            m["replacements"] = good_reps
            clean_matches.append(m)

    result["matches"] = clean_matches
    return result


# ---------------------------------------------------------------------
# ✅ Legal corrections (UNCHANGED LOGIC)
# ---------------------------------------------------------------------
def apply_legal_corrections(sentence: str):
    corrected = sentence
    applied = []

    for wrong, correct in LEGAL_FIX.items():
        pattern = re.compile(r"\b" + re.escape(wrong) + r"\b", flags=re.IGNORECASE)
        if re.search(pattern, corrected):
            corrected = re.sub(pattern, correct, corrected)

            key = normalize_key(correct)
            meaning = BLACKLAW.get(key, "(Meaning not found in Black’s Law Dictionary)")
            applied.append((wrong, correct, meaning))

    return corrected, applied


# ---------------------------------------------------------------------
# ✅ DOCX APPLY CHANGES (UNCHANGED)
# ---------------------------------------------------------------------
def apply_replacements_to_doc(doc: Document, replacements):
    for para in doc.paragraphs:
        for r in replacements:
            wrong = r["old"]
            correct = r["new"]
            if wrong.lower() in para.text.lower():
                para.text = re.sub(
                    rf"\b{re.escape(wrong)}\b",
                    correct,
                    para.text,
                    flags=re.IGNORECASE,
                )
    return doc


# ---------------------------------------------------------------------
# ✅ HTML PREVIEW – (UNCHANGED)
# ---------------------------------------------------------------------
def process_text_line_by_line(text: str):
    lines = text.splitlines()
    html_parts, issues = [], []

    for i, line in enumerate(lines, start=1):
        if is_reference_like(line):
            html_parts.append(f"<p>{line}</p>")
            continue

        if not line.strip():
            html_parts.append("<p></p>")
            continue

        corrected, legal_hits = apply_legal_corrections(line)
        html_line = corrected

        for wrong, correct, meaning in legal_hits:
            wrong_e = escape(wrong)
            correct_e = escape(correct)

            html_line = re.sub(
                re.escape(wrong),
                (
                    "<span class='error legal-correct' "
                    f"data-wrong='{wrong_e}' "
                    f"data-suggestion='{correct_e}' "
                    f"data-black-suggestion='{correct_e}' "
                    f"data-general-suggestion='' "
                    f"data-message='Legal correction'>{correct_e}</span>"
                ),
                html_line,
                flags=re.IGNORECASE,
            )

            issues.append(
                {"line": i, "wrong": wrong, "suggestion": correct,
                 "message": "Legal correction", "meaning": meaning}
            )

        lt_result = lt_check_sentence(corrected)
        matches = lt_result.get("matches", [])

        for m in matches:
            off, ln = m.get("offset", 0), m.get("length", 0)
            reps = m.get("replacements", [])
            if not reps:
                continue

            black_sug = None
            general_sug = None

            for rep in reps:
                cand = rep["value"].strip()
                norm = normalize_key(cand)
                if norm in BLACKLAW and not black_sug:
                    black_sug = cand
                if norm not in BLACKLAW and not general_sug:
                    general_sug = cand

            primary_sug = black_sug or general_sug or reps[0]["value"]
            wrong = corrected[off : off + ln]

            meaning = BLACKLAW.get(normalize_key(primary_sug), "(No meaning found)")

            wrong_e = escape(wrong)
            primary_e = escape(primary_sug)
            black_e = escape(black_sug) if black_sug else ""
            general_e = escape(general_sug) if general_sug else ""

            html_line = html_line.replace(
                wrong,
                (
                    "<span class='error grammar-wrong' "
                    f"data-wrong='{wrong_e}' "
                    f"data-suggestion='{primary_e}' "
                    f"data-black-suggestion='{black_e}' "
                    f"data-general-suggestion='{general_e}'>"
                    f"{wrong_e}</span>"
                ),
            )

            issues.append(
                {"line": i, "wrong": wrong, "suggestion": primary_sug,
                 "message": m.get("message", ""), "meaning": meaning}
            )

        html_parts.append(f"<p>{html_line}</p>")

    return "\n".join(html_parts), issues


# ---------------------------------------------------------------------
# ✅ MAIN ROUTE (UNCHANGED)
# ---------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        text_input = request.form.get("text", "").strip()
        file = request.files.get("file")
        replacements_json = request.form.get("replacements")
        replacements = json.loads(replacements_json) if replacements_json else []

        if file and file.filename.endswith(".docx"):
            doc = Document(file)
            text = "\n".join(p.text for p in doc.paragraphs)
            base = os.path.splitext(file.filename)[0]
        else:
            text = text_input
            doc = Document()
            doc.add_paragraph(text)
            base = "corrected_output"

        if not text:
            return render_template("index.html", error="Please provide text or upload a .docx file.")

        html_out, issues = process_text_line_by_line(text)

        return render_template("result.html", highlighted_html=html_out)

    return render_template("index.html")


# ---------------------------------------------------------------------
# ✅ **THIS IS THE IMPORTANT FIX**
# ---------------------------------------------------------------------
@app.route("/download_corrected", methods=["POST"])
def download_corrected():

    final_text = request.form.get("final_text", "")
    replacements = json.loads(request.form.get("replacements", "[]"))

    doc = Document()

    for line in final_text.split("\n"):
        doc.add_paragraph(line)

    doc = apply_replacements_to_doc(doc, replacements)

    os.makedirs("static", exist_ok=True)
    output_path = os.path.join("static", "Corrected_Final_Output.docx")
    doc.save(output_path)

    return send_file(output_path, as_attachment=True)


# ---------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
