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
import os
import re
import hashlib
import hmac
from functools import wraps
from flask import (Flask, request, jsonify, send_file, render_template,
                   session, redirect, url_for)

import resume_builder as rb
import tailor as T

app = Flask(__name__)

# --- session / password gate -------------------------------------------------
# Secret used to sign session cookies. Set FLASK_SECRET in the environment for
# a stable value across restarts; a random fallback is used if unset.
app.secret_key = os.environ.get("FLASK_SECRET") or os.urandom(32)

# The site password. Read from APP_PASSWORD env var if set; otherwise the
# default below. Stored/compared as a hash, never echoed back to the client.
_DEFAULT_PASSWORD = "Anand!@#"


def _password_hash(pw):
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _expected_hash():
    pw = os.environ.get("APP_PASSWORD", _DEFAULT_PASSWORD)
    return _password_hash(pw)


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("authed"):
            # API calls get JSON 401; page loads get the login screen
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "auth required"}), 401
            return redirect(url_for("login"))
        return fn(*a, **k)
    return wrapper

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
    pages, fill_pct = rb.estimate_pages(data)
    return {"used": round(used, 1), "capacity": round(cap, 1),
            "pct": pct, "fits": bool(fits), "warn_pct": WARN_PCT,
            "pages": pages, "fill_pct": fill_pct}


# ------------------------------------------------------------------ routes
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hmac.compare_digest(_password_hash(pw), _expected_hash()):
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html",
                           default=rb.DEFAULT_DATA, warn_pct=WARN_PCT)


@app.route("/api/estimate", methods=["POST"])
@login_required
def api_estimate():
    data = _payload_to_data(request.get_json(force=True))
    return jsonify(_metrics(data))


@app.route("/api/preview", methods=["POST"])
@login_required
def api_preview():
    data = _payload_to_data(request.get_json(force=True))
    pdf = rb.build_pdf_bytes(data)
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=False, download_name="preview.pdf")


@app.route("/api/generate", methods=["POST"])
@login_required
def api_generate():
    data = _payload_to_data(request.get_json(force=True))
    pdf = rb.build_pdf_bytes(data)
    fname = data["filename"] + ".pdf"
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=True, download_name=fname)


@app.route("/api/analyze", methods=["POST"])
@login_required
def api_analyze():
    """STAGE 1 (cheap): read JD, extract keywords, score match, list missing
    skills for the user to accept/reject. No rewrite, minimal cost."""
    payload = request.get_json(force=True)
    jd_text = (payload.get("jd") or "").strip()
    master = _payload_to_data(payload)
    try:
        result = T.analyze_jd(master, jd_text)
    except T.TailorError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa
        return jsonify({"ok": False,
                        "error": f"Unexpected error while analyzing: {e}"}), 500
    return jsonify({
        "ok": True,
        "match": result["match"],
        "jd": result["jd"],
        "analysis_cost": result["analysis_cost"],
        "tailor_cost_estimate": result["tailor_cost_estimate"],
    })


@app.route("/api/generate_tailored", methods=["POST"])
@login_required
def api_generate_tailored():
    """STAGE 2 (paid rewrite): runs once. Uses accepted/confirmed skills in
    skills list + bullets. Returns tailored fields for review before download."""
    payload = request.get_json(force=True)
    jd_text = (payload.get("jd") or "").strip()
    confirmed = payload.get("confirmed_skills") or []
    master = _payload_to_data(payload)
    try:
        result = T.generate_tailored(master, jd_text, confirmed_skills=confirmed)
    except T.TailorError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa
        return jsonify({"ok": False,
                        "error": f"Unexpected error while tailoring: {e}"}), 500

    t = result["tailored"]
    editor = {
        "summary": t["summary"],
        "skills": "\n".join(t["skills"]),
        "experience": [{
            "company": j["company"], "job_title": j["job_title"],
            "meta": j["meta"], "bullets": "\n".join(j["bullets"]),
        } for j in t["experience"]],
        "education": "\n".join(t["education"]),
        "certifications": "\n".join(t["certifications"]),
    }
    metrics = _metrics(t)
    return jsonify({
        "ok": True,
        "editor": editor,
        "match": result["match"],
        "notes": result["notes"],
        "warnings": result["warnings"],
        "cost": result["cost"],
        "confirmed_skills": result["confirmed_skills"],
        "metrics": metrics,
    })


@app.route("/api/ai_status")
@login_required
def api_ai_status():
    import os
    return jsonify({"enabled": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
