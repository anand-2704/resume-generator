"""
tailor.py
---------
Job-description-aware resume tailoring.

Pipeline (mirrors the user's flow):
  extract_jd()      -> pull skills/tools/keywords/seniority from the JD
  match_resume()    -> compare JD terms against the master resume
  tailor_resume()   -> AI (Haiku) reorders + rewrites the user's REAL content
                       to echo JD terminology, tailors summary, selects bullets
  quality_check()   -> guardrail pass: flags any claim not grounded in master

HONESTY GUARDRAILS (hard rules, enforced in prompts + post-checks):
  * The model may ONLY rephrase, reorder, emphasise, or drop content that
    already exists in the master resume.
  * It must NOT invent skills, tools, employers, metrics, dates or outcomes.
  * "Missing" JD keywords are reported to the user, never auto-inserted.
  * Every tailored bullet is traceable to a master bullet; the quality pass
    warns if a number or tool appears that wasn't in the master.

Cost control:
  * Model = claude-haiku-4-5 (cheapest).
  * The master resume is sent as a cacheable system block (prompt caching)
    so repeated tailoring for different JDs reuses it at 0.1x input cost.
"""

import os
import re
import json
import urllib.request
import urllib.error

MODEL = "claude-haiku-4-5-20251001"
API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class TailorError(Exception):
    pass


# --------------------------------------------------------------------------
# Low-level API call
# --------------------------------------------------------------------------
def _api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise TailorError(
            "No API key configured. Set the ANTHROPIC_API_KEY environment "
            "variable on the server (see the tool's setup notes)."
        )
    return key


def _call(system_blocks, messages, max_tokens=2000, temperature=0.2):
    """POST to the Messages API. Returns (text, usage_dict)."""
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_blocks,
        "messages": messages,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("x-api-key", _api_key())
    req.add_header("anthropic-version", ANTHROPIC_VERSION)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        if e.code == 401:
            raise TailorError("API key rejected (401). Check the key value.")
        if e.code == 429:
            raise TailorError("Rate limited or out of credit (429). Add credit "
                              "in the Anthropic console and retry.")
        raise TailorError(f"API error {e.code}: {detail[:300]}")
    except urllib.error.URLError as e:
        raise TailorError(f"Network error contacting the API: {e.reason}")

    text = "".join(
        blk.get("text", "") for blk in payload.get("content", [])
        if blk.get("type") == "text"
    )
    usage = payload.get("usage", {})
    return text, usage


def _extract_json(text):
    """Pull the first JSON object/array out of a model reply, tolerating
    stray prose or ``` fences."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    # find first { or [ and matching end
    start = None
    for i, ch in enumerate(t):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise TailorError("Model did not return JSON.")
    depth = 0
    opener = t[start]
    closer = "}" if opener == "{" else "]"
    for j in range(start, len(t)):
        if t[j] == opener:
            depth += 1
        elif t[j] == closer:
            depth -= 1
            if depth == 0:
                chunk = t[start:j + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    break
    # last resort
    return json.loads(t[start:])


# --------------------------------------------------------------------------
# Master resume -> compact text (for the cacheable system block)
# --------------------------------------------------------------------------
def master_to_text(d):
    lines = []
    lines.append(f"NAME: {d['name']}")
    lines.append(f"TITLE: {d['title']}")
    lines.append(f"CONTACT: {d['contact']}")
    lines.append("SUMMARY:\n" + d["summary"])
    lines.append("TECHNICAL SKILLS (one category per line):")
    for s in d["skills"]:
        lines.append("  - " + s)
    lines.append("PROFESSIONAL EXPERIENCE:")
    for j in d["experience"]:
        lines.append(f"  COMPANY: {j['company']} | TITLE: {j['job_title']} | {j['meta']}")
        for b in j["bullets"]:
            lines.append("    * " + b)
    lines.append("EDUCATION:")
    for e in d["education"]:
        lines.append("  - " + e)
    lines.append("CERTIFICATIONS:")
    for c in d["certifications"]:
        lines.append("  - " + c)
    return "\n".join(lines)


def _cacheable_master_block(master_text):
    """A system block carrying the master resume, marked for prompt caching
    so repeated JD tailoring reuses it cheaply."""
    return {
        "type": "text",
        "text": "MASTER RESUME (the candidate's real, verified experience — "
                "the single source of truth):\n\n" + master_text,
        "cache_control": {"type": "ephemeral"},
    }


# --------------------------------------------------------------------------
# Step 1: extract structured info from the JD
# --------------------------------------------------------------------------
EXTRACT_SYS = (
    "You are an expert technical recruiter and ATS analyst. Extract structured "
    "hiring signals from a job description. Return ONLY JSON, no prose. "
    "Schema: {\"required_skills\":[],\"preferred_skills\":[],"
    "\"tools_technologies\":[],\"responsibilities\":[],\"qualifications\":[],"
    "\"keywords\":[],\"seniority\":\"\"}. "
    "keywords = the exact ATS terms a parser would scan for (tools, methods, "
    "acronyms), deduplicated, most important first. seniority = one of "
    "'intern','junior','mid','senior','lead','manager','director','unspecified'."
)


def extract_jd(jd_text):
    msgs = [{"role": "user", "content":
             f"JOB DESCRIPTION:\n\n{jd_text}\n\nReturn the JSON."}]
    text, usage = _call([{"type": "text", "text": EXTRACT_SYS}],
                        msgs, max_tokens=1200, temperature=0.0)
    data = _extract_json(text)
    return data, usage


# --------------------------------------------------------------------------
# Step 2: keyword match (local, no API) between JD terms and master resume
# --------------------------------------------------------------------------
def _norm(s):
    return re.sub(r"[^a-z0-9+.# ]", " ", s.lower())


# lightweight synonym map so wording differences don't cause false gaps
SYNONYMS = {
    "experimentation": ["a/b testing", "ab testing", "hypothesis testing", "split testing"],
    "a/b testing": ["experimentation", "split testing"],
    "forecasting": ["time series", "prophet", "arima", "predictive"],
    "etl": ["elt", "pipelines", "data pipelines", "ingestion"],
    "bi": ["business intelligence", "dashboards", "reporting"],
    "dashboards": ["tableau", "power bi", "looker", "quicksight", "reporting"],
    "cloud": ["aws", "s3", "redshift", "athena", "glue", "emr", "lambda", "gcp", "azure"],
    "ml": ["machine learning", "scikit-learn", "predictive modeling", "classification"],
    "sql": ["postgresql", "redshift", "bigquery", "snowflake", "queries"],
    "python": ["pandas", "numpy", "pyspark"],
    "data warehouse": ["snowflake", "redshift", "bigquery", "dimensional modelling", "star schema"],
}


def _expand(term):
    t = _norm(term).strip()
    out = {t}
    if t in SYNONYMS:
        out.update(_norm(x) for x in SYNONYMS[t])
    # also map any key whose synonyms include t
    for k, vs in SYNONYMS.items():
        if t in [_norm(v) for v in vs]:
            out.add(_norm(k))
            out.update(_norm(v) for v in vs)
    return out


def match_resume(jd_data, master_text):
    hay = _norm(master_text)
    kws = []
    seen = set()
    for k in (jd_data.get("keywords", []) +
              jd_data.get("required_skills", []) +
              jd_data.get("tools_technologies", []) +
              jd_data.get("preferred_skills", [])):
        kn = _norm(k).strip()
        if not kn or kn in seen:
            continue
        seen.add(kn)
        kws.append(k)

    matched, missing = [], []
    for k in kws:
        variants = _expand(k)
        hit = any(v and v in hay for v in variants)
        (matched if hit else missing).append(k)

    total = len(matched) + len(missing)
    score = round(100 * len(matched) / total) if total else 100
    return {
        "score": score,
        "matched": matched,
        "missing": missing,
        "keywords_considered": kws,
    }


# --------------------------------------------------------------------------
# Step 3: AI tailoring (Haiku) — reorder + rewrite REAL content only
# --------------------------------------------------------------------------
TAILOR_SYS = (
    "You tailor a candidate's resume to a specific job, and you are strictly "
    "truthful. HARD RULES:\n"
    "1. Use ONLY facts present in the MASTER RESUME, PLUS any skills in the "
    "CONFIRMED SKILLS list (skills the candidate has explicitly told you they "
    "have really used but hadn't written down). Never invent skills, tools, "
    "employers, titles, or dates beyond these.\n"
    "2. NEVER invent metrics, numbers, outcomes, projects, or accomplishments. "
    "Every number and every result must already exist in the master resume. "
    "Do not attach a new metric to a confirmed skill.\n"
    "3. For each CONFIRMED SKILL, you MAY: add it to the skills list, and weave "
    "it into an EXISTING bullet that describes real work where that tool "
    "plausibly applies — by naming the tool within that real work (e.g. "
    "'Built Power BI dashboards using DAX measures…' if a dashboard bullet "
    "exists). You are describing the SAME real work, now naming a tool the "
    "candidate has used. You must NOT create a brand-new accomplishment or "
    "metric around a confirmed skill.\n"
    "4. You MAY rephrase bullets to mirror the job's terminology, reorder "
    "skills and bullets by relevance, tailor the summary, and drop irrelevant "
    "content.\n"
    "5. Do NOT add any missing keyword that is NOT in the confirmed list.\n"
    "6. Keep the same companies, titles, dates, education, and certifications "
    "exactly as in the master (you may reorder bullets within a job).\n"
    "7. Preserve the candidate's real seniority; do not inflate it.\n"
    "Return ONLY JSON matching the requested schema, no prose. You may use "
    "**double asterisks** around words to mark them bold in the output."
)

TAILOR_SCHEMA = (
    '{"summary": "string", '
    '"skills": ["category line", ...], '
    '"experience": [{"company":"", "job_title":"", "meta":"", '
    '"bullets":["", ...]}, ...], '
    '"education": ["", ...], '
    '"certifications": ["", ...], '
    '"notes": ["short reviewer notes on what you changed / could not add"]}'
)


def tailor_resume(master_data, master_text, jd_text, jd_data, match,
                  confirmed_skills=None):
    confirmed_skills = confirmed_skills or []
    confirmed_line = (
        "CONFIRMED SKILLS (the candidate says they have really used these — "
        "you may add them to skills and weave into existing real bullets, but "
        "never invent metrics/projects around them): "
        + (", ".join(confirmed_skills) if confirmed_skills else "(none)")
    )
    user = (
        f"TARGET JOB DESCRIPTION:\n{jd_text}\n\n"
        f"EXTRACTED JD SIGNALS:\n{json.dumps(jd_data)}\n\n"
        f"KEYWORD MATCH (already computed):\n"
        f"matched={match['matched']}\nmissing={match['missing']}\n\n"
        f"{confirmed_line}\n\n"
        "TASK: Produce a tailored version of the resume for THIS job.\n"
        "- Rewrite the SUMMARY to lead with the candidate's real strengths "
        "most relevant to this job, echoing the job's language where truthful. "
        "You may mention confirmed skills here if they fit naturally.\n"
        "- Reorder the SKILLS category lines so the most job-relevant appear "
        "first; keep each category's real contents. ADD each confirmed skill "
        "to the most appropriate category (do not add non-confirmed missing "
        "keywords).\n"
        "- For each job, select and REORDER the most relevant bullets first, "
        "and rephrase them to mirror the JD's terminology using work actually "
        "described in the master. Where a confirmed skill plausibly applies to "
        "an existing bullet's real work, name it within that bullet — WITHOUT "
        "inventing any new metric, project, or outcome.\n"
        "- Do NOT add any 'missing' keyword that is not in the confirmed list.\n"
        "- Keep companies/titles/dates/education/certifications truthful.\n"
        "- In notes[], briefly list what you emphasised, which confirmed skills "
        "you added and where, and any JD requirement still unmet (so the human "
        "can decide).\n\n"
        f"Return ONLY JSON with this exact schema:\n{TAILOR_SCHEMA}"
    )
    system = [
        {"type": "text", "text": TAILOR_SYS},
        _cacheable_master_block(master_text),
    ]
    text, usage = _call(system, [{"role": "user", "content": user}],
                        max_tokens=3000, temperature=0.25)
    data = _extract_json(text)

    # merge onto master so any missing field falls back safely
    out = dict(master_data)
    out = {k: (v.copy() if isinstance(v, list) else v) for k, v in out.items()}
    out["summary"] = data.get("summary", out["summary"]).strip() or out["summary"]
    if data.get("skills"):
        out["skills"] = [s for s in data["skills"] if s.strip()]
    if data.get("education"):
        out["education"] = [s for s in data["education"] if s.strip()]
    if data.get("certifications"):
        out["certifications"] = [s for s in data["certifications"] if s.strip()]

    # experience: keep master company/title/meta, take tailored bullets by index
    if data.get("experience"):
        new_exp = []
        for i, master_job in enumerate(master_data["experience"]):
            tj = data["experience"][i] if i < len(data["experience"]) else {}
            bullets = tj.get("bullets") or master_job["bullets"]
            new_exp.append({
                "company": master_job["company"],   # locked truthful
                "job_title": master_job["job_title"],
                "meta": master_job["meta"],
                "bullets": [b for b in bullets if b and b.strip()],
            })
        out["experience"] = new_exp

    notes = data.get("notes", [])
    return out, notes, usage


# --------------------------------------------------------------------------
# Step 4: quality / grounding check (local heuristic, no API)
# --------------------------------------------------------------------------
_NUM_RE = re.compile(r"\$?\d[\d,\.]*\s?(?:%|k|m|b|pb|tb|x|\+)?", re.I)


def quality_check(master_text, tailored, confirmed_skills=None):
    """Warn if a NUMBER appears in the tailored output that never appeared in
    the master (possible fabricated metric). Confirmed skills are allowed as
    tool names, so we only police numbers/metrics here."""
    master_nums = set(re.findall(r"\d[\d,\.]*", master_text))
    warnings = []

    def scan(text, where):
        for m in _NUM_RE.findall(text or ""):
            digits = re.sub(r"[^\d.]", "", m)
            if digits and digits not in "".join(master_nums) and \
               not any(digits in mn for mn in master_nums):
                warnings.append(f"{where}: number '{m.strip()}' not found in "
                                f"master resume — verify it's real.")

    scan(tailored.get("summary", ""), "Summary")
    for j in tailored.get("experience", []):
        for b in j["bullets"]:
            scan(b, f"{j['company']} bullet")

    # de-duplicate, cap
    seen, uniq = set(), []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq[:12]


# --------------------------------------------------------------------------
# Cost estimate from usage
# --------------------------------------------------------------------------
# Haiku 4.5: $1 / MTok input, $5 / MTok output. Cached read ~0.1x input.
def cost_from_usage(usages):
    in_tok = out_tok = cache_read = cache_write = 0
    for u in usages:
        in_tok += u.get("input_tokens", 0)
        out_tok += u.get("output_tokens", 0)
        cache_read += u.get("cache_read_input_tokens", 0)
        cache_write += u.get("cache_creation_input_tokens", 0)
    cost = (in_tok * 1.0 + cache_write * 1.25 + cache_read * 0.10
            + out_tok * 5.0) / 1_000_000
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "usd": round(cost, 4),
    }


# --------------------------------------------------------------------------
# Two-stage orchestration
# --------------------------------------------------------------------------
# Rough token estimate for showing an up-front cost before the paid rewrite.
def estimate_tailor_cost(master_text, jd_text):
    # Haiku: $1/MTok in, $5/MTok out. Master is cached after first call.
    approx_in = 900 + len(jd_text) // 4        # jd + instructions (master cached)
    approx_out = 1500
    usd = (approx_in * 1.0 + approx_out * 5.0) / 1_000_000
    return round(usd, 4)


def analyze_jd(master_data, jd_text):
    """STAGE 1 (cheap): extract JD signals + keyword match. No rewrite."""
    if not jd_text or len(jd_text.strip()) < 40:
        raise TailorError("Please paste a fuller job description (a few lines "
                          "at least) so the analysis has something to work with.")
    master_text = master_to_text(master_data)
    jd_data, usage = extract_jd(jd_text)
    match = match_resume(jd_data, master_text)
    cost = cost_from_usage([usage])
    tailor_estimate = estimate_tailor_cost(master_text, jd_text)
    return {
        "jd": jd_data,
        "match": match,
        "analysis_cost": cost,
        "tailor_cost_estimate": tailor_estimate,
    }


def generate_tailored(master_data, jd_text, confirmed_skills=None):
    """STAGE 2 (paid rewrite): runs once, using confirmed skills."""
    if not jd_text or len(jd_text.strip()) < 40:
        raise TailorError("Job description missing or too short.")
    master_text = master_to_text(master_data)
    confirmed_skills = confirmed_skills or []

    # re-extract + match so the rewrite has fresh signals (cheap; also lets the
    # match reflect confirmed skills as now-covered)
    jd_data, u1 = extract_jd(jd_text)
    match = match_resume(jd_data, master_text)
    # confirmed skills count as matched for the reported score
    if confirmed_skills:
        cs_norm = {_norm(c) for c in confirmed_skills}
        still_missing = []
        for k in match["missing"]:
            if _norm(k) in cs_norm:
                match["matched"].append(k)
            else:
                still_missing.append(k)
        match["missing"] = still_missing
        tot = len(match["matched"]) + len(match["missing"])
        match["score"] = round(100 * len(match["matched"]) / tot) if tot else 100

    tailored, notes, u2 = tailor_resume(
        master_data, master_text, jd_text, jd_data, match,
        confirmed_skills=confirmed_skills)

    warnings = quality_check(master_text, tailored, confirmed_skills)
    cost = cost_from_usage([u1, u2])
    return {
        "jd": jd_data,
        "match": match,
        "tailored": tailored,
        "notes": notes,
        "warnings": warnings,
        "cost": cost,
        "confirmed_skills": confirmed_skills,
    }
