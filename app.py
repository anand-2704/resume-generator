"""
app.py — Flask web app for the ATS-safe resume generator.

Endpoints:
  GET  /                -> the single-page editor UI
  POST /api/estimate    -> {used, capacity, pct, fits} for the live meter (A)
  POST /api/preview     -> returns the actual PDF inline (B, exact preview)
  POST /api/generate    -> returns the actual PDF as a download

Run locally:
  pip install -r requirements.txt
  python app.py
  open http://127.0.0.1:5000
"""

import io
import re
from flask import Flask, request, jsonify, send_file, render_template

import resume_builder as rb

app = Flask(__name__)

WARN_PCT = 95.0  # meter turns amber at/above this; red when over 100%


# ------------------------------------------------------------------ helpers
def _clean_filename(name):
    name = (name or "").strip() or "resume"
    name = re.sub(r"[^\w\-. ]", "", name).strip().replace(" ", "_")
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name or "resume"


def _payload_to_data(p):
    """Map the JSON posted by the browser to resume_builder's data dict.
    Falls back to DEFAULT_DATA values for any missing piece so the app is
    robust even with partially filled forms."""
    d = dict(rb.DEFAULT_DATA)  # shallow copy of scalars
    d = {k: (v.copy() if isinstance(v, list) else v) for k, v in d.items()}

    d["filename"] = _clean_filename(p.get("filename"))
    d["name"] = p.get("name", d["name"]).strip() or d["name"]
    d["title"] = p.get("title", d["title"]).strip() or d["title"]
    d["contact"] = p.get("contact", d["contact"]).strip() or d["contact"]
    d["summary"] = p.get("summary", d["summary"]).strip() or d["summary"]

    def lines(v, fallback):
        if v is None:
            return fallback
        out = [ln for ln in v.split("\n")] if isinstance(v, str) else list(v)
        out = [ln.rstrip() for ln in out if ln.strip() != ""]
        return out if out else fallback

    d["skills"] = lines(p.get("skills"), d["skills"])
    d["education"] = lines(p.get("education"), d["education"])
    d["certifications"] = lines(p.get("certifications"), d["certifications"])

    exp = []
    posted_exp = p.get("experience")
    if posted_exp:
        for i, job in enumerate(posted_exp):
            base = rb.DEFAULT_DATA["experience"][i] if i < 2 else {
                "company": "", "job_title": "", "meta": "", "bullets": []}
            exp.append({
                "company": (job.get("company") or base["company"]).strip(),
                "job_title": (job.get("job_title") or base["job_title"]).strip(),
                "meta": (job.get("meta") or base["meta"]).strip(),
                "bullets": lines(job.get("bullets"), base["bullets"]),
            })
    else:
        exp = d["experience"]
    d["experience"] = exp
    return d


def _metrics(data):
    used, cap, fits = rb.estimate_fits_one_page(data)
    pct = round(100.0 * used / cap, 1)
    return {"used": round(used, 1), "capacity": round(cap, 1),
            "pct": pct, "fits": bool(fits), "warn_pct": WARN_PCT}


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    return render_template("index.html",
                           default=rb.DEFAULT_DATA, warn_pct=WARN_PCT)


@app.route("/api/estimate", methods=["POST"])
def api_estimate():
    data = _payload_to_data(request.get_json(force=True))
    return jsonify(_metrics(data))


@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = _payload_to_data(request.get_json(force=True))
    pdf = rb.build_pdf_bytes(data)
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=False, download_name="preview.pdf")


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = _payload_to_data(request.get_json(force=True))
    pdf = rb.build_pdf_bytes(data)
    fname = data["filename"] + ".pdf"
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=True, download_name=fname)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
