from flask import Flask, render_template, request, send_file
from docx import Document
import requests
import os
import re
import json
from html import escape

app = Flask(__name__)
LT_API_URL = "https://api.languagetool.org/v2/check"

# -------------------------------------------------------------
# Load Black’s Law Dictionary
# -------------------------------------------------------------
try:
    with open("blacklaw_terms.json", "r", encoding="utf-8") as f:
        BLACKLAW = json.load(f)
except:
    BLACKLAW = {}

def normalize_key(word):
    return re.sub(r"[^a-z]", "", word.lower().strip())

# -------------------------------------------------------------
# Legal Fixes
# -------------------------------------------------------------
LEGAL_FIX = {
    "prima facia": "prima facie",
    "mens reaa": "mens rea",
    "ratio decedendi": "ratio decidendi",
    "precedant": "precedent",
    "jurispridence": "jurisprudence",
    "suo moto": "suo motu",
}

def is_reference_like(line):
    s = line.strip()
    return (
        not s
        or "http" in s
        or "www." in s
        or "doi" in s.lower()
        or re.match(r"^\[\d+\]$", s)
        or re.match(r"^\(.+\d{4}.*\)$", s)
    )

# -------------------------------------------------------------
# LanguageTool API Cleaned
# -------------------------------------------------------------
def lt_check_sentence(text):
    try:
        data = {"text": text, "language": "en-US"}
        r = requests.post(LT_API_URL, data=data, timeout=10)
        r.raise_for_status()
        return r.json()
    except:
        return {"matches": []}

# -------------------------------------------------------------
# Legal Corrections
# -------------------------------------------------------------
def apply_legal_corrections(sentence):
    corrected = sentence
    hits = []

    for wrong, correct in LEGAL_FIX.items():
        pattern = re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)
        if re.search(pattern, corrected):
            corrected = re.sub(pattern, correct, corrected)
            meaning = BLACKLAW.get(normalize_key(correct), "(Meaning not found)")
            hits.append((wrong, correct, meaning))

    return corrected, hits

# -------------------------------------------------------------
# Highlight Engine (HTML)
# -------------------------------------------------------------
def process_text_line_by_line(text):
    lines = text.splitlines()
    html_output = []
    issues = []

    for line_no, line in enumerate(lines, start=1):

        if is_reference_like(line):
            html_output.append(f"<p>{line}</p>")
            continue

        corrected, legal_hits = apply_legal_corrections(line)
        html_line = corrected

        # Apply legal highlights
        for wrong, correct, meaning in legal_hits:
            wrong_e = escape(wrong)
            correct_e = escape(correct)

            html_line = re.sub(
                re.escape(wrong),
                (
                    f"<span class='error legal-correct' "
                    f"data-wrong='{wrong_e}' "
                    f"data-suggestion='{correct_e}' "
                    f"data-black-suggestion='{correct_e}' "
                    f"data-general-suggestion='' >{correct_e}</span>"
                ),
                html_line,
                flags=re.IGNORECASE
            )

            issues.append({
                "line": line_no,
                "wrong": wrong,
                "suggestion": correct,
                "message": "Legal correction",
                "meaning": meaning
            })

        # Grammar via LanguageTool
        response = lt_check_sentence(corrected)

        for m in response.get("matches", []):
            offs, ln = m["offset"], m["length"]
            wrong = corrected[offs:offs+ln]

            black_sug = None
            general_sug = None

            for rep in m["replacements"]:
                sug = rep["value"]
                if normalize_key(sug) in BLACKLAW and not black_sug:
                    black_sug = sug
                elif not general_sug:
                    general_sug = sug

            primary = black_sug or general_sug
            if not primary:
                continue

            wrong_e = escape(wrong)
            primary_e = escape(primary)
            black_e = escape(black_sug) if black_sug else ""
            general_e = escape(general_sug) if general_sug else ""

            html_line = html_line.replace(
                wrong,
                (
                    f"<span class='error grammar-wrong' "
                    f"data-wrong='{wrong_e}' "
                    f"data-suggestion='{primary_e}' "
                    f"data-black-suggestion='{black_e}' "
                    f"data-general-suggestion='{general_e}' >{wrong_e}</span>"
                )
            )

            meaning = BLACKLAW.get(normalize_key(primary), "(No meaning found)")

            issues.append({
                "line": line_no,
                "wrong": wrong,
                "suggestion": primary,
                "message": m["message"],
                "meaning": meaning
            })

        html_output.append(f"<p>{html_line}</p>")

    return "\n".join(html_output), issues

# -------------------------------------------------------------
# APPLY FIXES TO DOCX
# -------------------------------------------------------------
def apply_replacements_to_doc(doc, replacements):
    for para in doc.paragraphs:
        for r in replacements:
            wrong = r["old"]
            new = r["new"]
            para.text = re.sub(rf"\b{re.escape(wrong)}\b", new, para.text, flags=re.IGNORECASE)
    return doc

# -------------------------------------------------------------
# ROUTES
# -------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":

        text = request.form.get("text", "").strip()
        file = request.files.get("file")

        if file and file.filename.endswith(".docx"):
            doc = Document(file)
            text = "\n".join(p.text for p in doc.paragraphs)
        else:
            doc = Document()
            doc.add_paragraph(text)

        highlighted, issues = process_text_line_by_line(text)

        return render_template("result.html",
                               highlighted_html=highlighted,
                               issues=issues)

    return render_template("index.html")


@app.route("/download_corrected", methods=["POST"])
def download_corrected():
    final_text = request.form.get("final_text", "")
    replacements = json.loads(request.form.get("replacements", "[]"))

    doc = Document()
    for line in final_text.split("\n"):
        doc.add_paragraph(line)

    output_path = "static/corrected_output.docx"
    doc.save(output_path)

    return send_file(output_path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)
