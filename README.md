# Resume Generator — one-page, ATS-safe

A small web app that turns pasted-in text into a PDF résumé that **exactly
matches** the reference layout (A4, Calibri/Carlito, section rules, bold
keywords, right-aligned dates). Built for **ATS parsing**: no tables, no
columns, no text boxes, no images — just clean, selectable, linear text that
Workday / Greenhouse / Taleo / iCIMS read correctly.

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
