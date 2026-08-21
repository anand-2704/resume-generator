# Resume Generator — ATS-safe, auto multi-page

A small web app that turns pasted-in text into a PDF résumé that **exactly
matches** the reference layout (A4, Calibri/Carlito, section rules, bold
keywords, right-aligned dates). Built for **ATS parsing**: no tables, no
columns, no text boxes, no images — just clean, selectable, linear text that
Workday / Greenhouse / Taleo / iCIMS read correctly.

## Access & accounts

The site is protected by a single shared **password gate**. Visitors see a
login screen and must enter the password before using the tool; a session
cookie keeps them signed in, and there's a **Log out** link.

- Default password: `Anand!@#`
- To change it without editing code, set the `APP_PASSWORD` environment
  variable on the server.
- Also set `FLASK_SECRET` (any long random string) so logins survive server
  restarts. Without it a random secret is used and everyone is logged out on
  each redeploy.

This is a single shared password (good for a personal/friends tool), **not**
individual user accounts.

## My Details — save & restore (per browser)

The **"My Details"** bar at the top of the editor has:
- **Save my details** — stores everything currently in the form in *this
  browser* (localStorage). It reloads automatically on your next visit.
- **Clear / restore blank** — wipes the saved details and blanks the form so
  someone else can enter their own resume on their own device.

Saved data lives only in that browser on that device (no server storage, no
accounts). Clearing browser data or switching devices means re-entering.

## Pages

The PDF **flows onto additional pages automatically** when content exceeds one
page — nothing is ever clipped. The fill meter shows the current page count
("1 page" / "2 pages") as information. Trim content if you prefer a single
page; section headings are kept with their content across page breaks.

## What you get

- **Live editor** (left): paste Summary, Technical Skills, two Experience
  sections, Education, Certifications. Company names, titles, dates, education
  and certs come **pre-filled** and are fully editable.
- **Bold anywhere**: wrap text in `**double asterisks**` to make it bold.
- **One-page guardrail** (right): a fill meter that turns **amber at 95%** and
  **red over 100%**, with a "trim ~N lines" hint. A live HTML replica updates
  as you type (estimate), plus a **Preview exact PDF** button that renders the
  real output.
- **Generate & download**: name the file and download the final PDF.

## Run locally

```bash
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

> **Font note.** The PDF uses **Carlito**, the metric-compatible open twin of
> Calibri (identical letter widths, so the layout matches Calibri exactly).
> The four Carlito `.ttf` files are bundled in `fonts/` — nothing to install.

## Deploy free

The app is a standard Flask + gunicorn service. The `Procfile`, `runtime.txt`
and `requirements.txt` are included.

**Render.com (free web service)**
1. Push this folder to a GitHub repo.
2. New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Deploy. (Carlito ships in `fonts/`, so fonts work on the server too.)

**Railway / Fly.io / PythonAnywhere** work the same way — point them at
`gunicorn app:app`.

## Files

| File | Purpose |
|------|---------|
| `resume_builder.py` | The PDF engine (ReportLab). Layout constants match the reference resume. Also holds `DEFAULT_DATA` (your pre-filled content). |
| `app.py` | Flask routes: `/`, `/api/estimate`, `/api/preview`, `/api/generate`. |
| `templates/index.html` | The editor UI, live meter, and previews. |
| `fonts/` | Carlito TTFs (Regular/Bold/Italic/BoldItalic). |

## Editing the pre-filled defaults

Open `resume_builder.py` and edit `DEFAULT_DATA` near the bottom — it maps
1:1 to the form fields. Changing it changes what loads in the editor.

## The one-page meter, explained

- The **live meter** and HTML replica are a fast estimate (server-measured
  text wrapping), tuned to be slightly conservative.
- The **Preview exact PDF** button and **Generate** produce the *real*
  ReportLab PDF, so verify there before you submit.
- Warning threshold is `WARN_PCT = 95.0` in `app.py` — change it if you want
  more or less safety margin.

## Job-description tailoring (AI, optional) — two-stage, cost-controlled

Paste a job description into the **"Tailor to a job"** box at the top. The flow
is split into a cheap analysis step and a paid rewrite step, so you only pay
for rewrites you actually want:

**Stage 1 — Analyze job (near-free, ~$0.003)**
Click **Analyze job**. The tool extracts the job's skills/keywords/seniority,
scores how well your current resume matches, and lists:
- **Matched** keywords (green).
- **Missing** keywords, each with an **"I have used this"** checkbox.

It then shows the analysis cost and an estimate for the paid step. If the match
is poor and you don't want to proceed, you stop here having spent ~$0.003.

**Stage 2 — Generate tailored resume (the paid rewrite, ~$0.008–0.02)**
Tick any missing keywords you have **genuinely used**, then click **Generate
tailored resume**. The rewrite runs **once** and:
- Rewrites your summary and reorders skills to match the job.
- Selects and rephrases your experience bullets to mirror the job's language.
- Adds each **confirmed** skill to your skills list **and** weaves it into an
  existing bullet that describes real work where that tool applies.

**Review, then download.** The result shows what changed, any confirmed skills
added, and a grounding check. Click **Apply to editor** (reversible with
**Undo**), review the fields, then **Generate & download**.

### Honesty guardrails (by design)

- The AI uses only your master resume **plus** skills you explicitly confirm
  you've used. It never adds a missing keyword you didn't confirm.
- Confirmed skills are added truthfully — named within real work you already
  listed. The AI is instructed **never to invent a metric, number, project, or
  accomplishment**, even around a confirmed skill.
- A grounding check flags any number in the output not found in your master
  resume, so a fabricated metric can't slip through unnoticed.
- Companies, titles, dates, education and certifications stay locked to your
  real values.
- You approve every change before it reaches the PDF.

> This tool ends at "download tailored PDF." It does **not** auto-submit
> applications or act on your email/accounts — you apply yourself.

### Enabling AI tailoring — set your API key

The tailoring calls Anthropic's **Claude Haiku** model. It needs *your* API
key, set as an environment variable on the server. Without it, the rest of the
tool still works; only the tailor button is disabled.

1. Create a key at **console.anthropic.com** → Settings → API Keys.
2. Add a few dollars of prepaid credit (Settings → Billing). ~1–2 cents per
   tailoring, so $5 lasts a long time.
3. On **Render**: open your service → **Environment** → **Add Environment
   Variable** → Key `ANTHROPIC_API_KEY`, Value `sk-ant-...` → **Save**. Render
   redeploys and the tailor button turns on.

**Never commit the key to GitHub or put it in the code.** It lives only in the
server's environment. If it leaks, delete it in the console and make a new one.

The model is `claude-haiku-4-5` (cheapest). Your master resume is sent as a
cached block so repeated tailoring for different jobs is billed at ~10% input
cost. To change the model, edit `MODEL` in `tailor.py`.
