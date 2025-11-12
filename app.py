from flask import Flask, render_template, request, send_file, url_for, jsonify
from docx import Document
from docx.shared import RGBColor
import requests
import io
import os
import re
import json

app = Flask(__name__)

# Public LanguageTool API
LT_API_URL = "https://api.languagetool.org/v2/check"

# -----------------------------
# ✅ Dictionary helper (ONLY Black's Law Dictionary)
# -----------------------------
def get_word_meaning(word):
    """Return meaning only from local Black's Law Dictionary JSON"""
    if not word:
        return "(No meaning)"
    try:
        with open("blacklaw_terms.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        meaning = data.get(word.lower())
        if meaning:
            return f"{meaning} (Black's Law Dictionary)"
        else:
            return "(No meaning found in Black's Law Dictionary)"
    except Exception as e:
        return f"(Error reading dictionary: {str(e)})"


# -----------------------------
# Helper: detect reference-like lines
# -----------------------------
def is_reference_like(line: str) -> bool:
    line_strip = line.strip()
    if not line_strip:
        return True
    if "http" in line_strip or "www." in line_strip or "doi" in line_strip.lower():
        return True
    if re.match(r"^\[\d+\]$", line_strip):
        return True
    if re.match(r"^\(.+\d{4}.*\)$", line_strip):
        return True
    return False


# -----------------------------
# Helper: call LanguageTool
# -----------------------------
def lt_check_sentence(sentence: str, lang="en-US"):
    data = {"text": sentence, "language": lang}
    resp = requests.post(LT_API_URL, data=data)
    resp.raise_for_status()
    return resp.json()


# -----------------------------
# DOCX highlighting (for download)
# -----------------------------
def highlight_docx_paragraphs(doc: Document) -> Document:
    for para in doc.paragraphs:
        original_text = para.text
        if not original_text.strip() or is_reference_like(original_text):
            continue

        result = lt_check_sentence(original_text)
        matches = result.get("matches", [])
        if not matches:
            continue

        spans = []
        for m in matches:
            offset = m.get("offset")
            length = m.get("length")
            replacements = m.get("replacements", [])
            suggestion = replacements[0]["value"] if replacements else ""
            if offset is None or length is None:
                continue
            spans.append((offset, length, suggestion))
        spans.sort(key=lambda x: x[0])

        new_segments = []
        cursor = 0
        for offset, length, suggestion in spans:
            start, end = offset, offset + length
            if cursor < start:
                new_segments.append((original_text[cursor:start], None, False))
            wrong_word = original_text[start:end]
            new_segments.append((wrong_word, RGBColor(255, 0, 0), True))
            if suggestion:
                new_segments.append((" → " + suggestion, RGBColor(0, 128, 0), False))
            cursor = end
        if cursor < len(original_text):
            new_segments.append((original_text[cursor:], None, False))

        for r in para.runs:
            r.text = ""
        para.text = ""
        for text_part, color, bold in new_segments:
            run = para.add_run(text_part)
            if color:
                run.font.color.rgb = color
            run.bold = bold

    return doc


# -----------------------------
# Web preview builder
# -----------------------------
def process_text_line_by_line(text: str):
    lines = text.splitlines()
    final_html_parts = []
    all_issues = []
    line_no = 0

    for line in lines:
        line_no += 1
        if is_reference_like(line):
            final_html_parts.append(f"<p>{line}</p>")
            continue
        if not line.strip():
            final_html_parts.append("<p></p>")
            continue

        lt_result = lt_check_sentence(line)
        matches = lt_result.get("matches", [])
        spans = []
        for m in matches:
            offset = m.get("offset")
            length = m.get("length")
            replacements = m.get("replacements", [])
            suggestion = replacements[0]["value"] if replacements else None
            message = m.get("message", "")
            if offset is None or length is None:
                continue
            meaning_text = get_word_meaning(suggestion or "")
            spans.append({
                "offset": offset,
                "length": length,
                "suggestion": suggestion,
                "message": message,
                "meaning": meaning_text
            })
        spans.sort(key=lambda x: x["offset"])

        html_line = ""
        cursor = 0
        for sp in spans:
            start, end = sp["offset"], sp["offset"] + sp["length"]
            wrong = line[start:end]
            before = line[cursor:start]
            html_line += before
            html_line += (
                f"<span class='error' data-suggestion='{sp['suggestion'] or ''}' "
                f"data-message='{sp['message']}' "
                f"data-meaning='{sp['meaning']}'>{wrong}</span>"
            )
            all_issues.append({
                "line": line_no,
                "wrong": wrong,
                "suggestion": sp["suggestion"] or "",
                "message": sp["message"],
                "meaning": sp["meaning"]
            })
            cursor = end
        html_line += line[cursor:]
        final_html_parts.append(f"<p>{html_line}</p>")

    highlighted_html = "\n".join(final_html_parts)
    return highlighted_html, all_issues


# -----------------------------
# Routes for Web App
# -----------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        input_text = request.form.get("text", "").strip()
        file = request.files.get("file")

        if file and file.filename.endswith(".docx"):
            doc = Document(file)
            text = "\n".join([p.text for p in doc.paragraphs])
            original_name = os.path.splitext(file.filename)[0]
        else:
            text = input_text
            doc = Document()
            doc.add_paragraph(text)
            original_name = "corrected_output"

        if not text:
            return render_template("index.html", error="Please provide text or upload a .docx file.")

        highlighted_html, issues = process_text_line_by_line(text)
        highlighted_doc = highlight_docx_paragraphs(doc)

        os.makedirs("static", exist_ok=True)
        filename = f"{original_name}_corrected.docx"
        output_path = os.path.join("static", filename)
        highlighted_doc.save(output_path)

        return render_template(
            "result.html",
            highlighted_html=highlighted_html,
            issues=issues,
            download_link=url_for("download_file", filename=filename)
        )

    return render_template("index.html")


@app.route("/download/<filename>")
def download_file(filename):
    file_path = os.path.join("static", filename)
    return send_file(file_path, as_attachment=True)


# -----------------------------
# 🔹 API route for Word Add-in
# -----------------------------
@app.route("/api/grammar_check", methods=["POST"])
def api_grammar_check():
    """API endpoint for Word Add-in grammar checking"""
    text = request.json.get("text", "")
    if not text.strip():
        return jsonify({"error": "No text provided"}), 400

    try:
        data = {"text": text, "language": "en-US"}
        resp = requests.post(LT_API_URL, data=data, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
