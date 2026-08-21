"""
resume_builder.py
-----------------
ATS-safe, single-page resume PDF generator built with ReportLab.

Design goals:
  * NO tables, columns, text boxes, images, headers/footers -> parses cleanly
    in Workday / Greenhouse / Taleo / iCIMS.
  * Real selectable text in Carlito (metric-compatible twin of Calibri).
  * Layout, font sizes, margins and spacing reverse-engineered to match the
    reference resume exactly (A4, ~10.8pt left / 6.4pt right margins).

Inline bold:  wrap any run in **double asterisks** inside body text to bold it.

The public entry points are:
  build_pdf(data, out_path)        -> writes a PDF file
  build_pdf_bytes(data)            -> returns PDF as bytes (for web download)
  estimate_fits_one_page(data)     -> (used_pts, page_capacity_pts, fits: bool)
"""

import io
import os
import re
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import stringWidth

# --------------------------------------------------------------------------
# Geometry / typography constants (points) — measured from the reference PDF
# --------------------------------------------------------------------------
PAGE_W, PAGE_H = A4                     # 595.32 x 842.04

MARGIN_L = 10.8
MARGIN_R = 6.4
MARGIN_T = 14.0
MARGIN_B = 13.0
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R
CONTENT_BOTTOM = MARGIN_B               # y at which content must stop

# font sizes
SZ_NAME = 16
SZ_TITLE = 12
SZ_CONTACT = 10.6
SZ_HEADING = 12
SZ_BODY = 10

# vertical rhythm  (measured from reference: body lines advance ~14.0pt,
# but section gaps in the source are tighter than a naive layout — these
# values reproduce the reference's 1-page packing exactly)
LEAD_BODY = 13.9        # line height for body text
GAP_AFTER_NAME = 5.5    # name bottom -> title top
GAP_AFTER_TITLE = 4.5   # title -> contact
GAP_AFTER_CONTACT = 4.5 # contact -> first heading
GAP_BEFORE_HEADING = 2.5  # extra space above a section heading
GAP_HEADING_RULE = 2.0    # heading baseline -> rule
GAP_AFTER_RULE = 2.8      # rule -> first body line
RULE_WIDTH = 0.72

# bullets
BULLET = "\u2022"
BULLET_INDENT = 14.0    # text hang indent for wrapped bullet lines
BULLET_GAP = 4.0        # spacing after '•' glyph

# fonts
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
F_REG = "Carlito"
F_BOLD = "Carlito-Bold"
F_ITAL = "Carlito-Italic"
F_BI = "Carlito-BoldItalic"

_FONTS_REGISTERED = False


def register_fonts():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont(F_REG, os.path.join(FONT_DIR, "Carlito-Regular.ttf")))
    pdfmetrics.registerFont(TTFont(F_BOLD, os.path.join(FONT_DIR, "Carlito-Bold.ttf")))
    pdfmetrics.registerFont(TTFont(F_ITAL, os.path.join(FONT_DIR, "Carlito-Italic.ttf")))
    pdfmetrics.registerFont(TTFont(F_BI, os.path.join(FONT_DIR, "Carlito-BoldItalic.ttf")))
    pdfmetrics.registerFontFamily(
        F_REG, normal=F_REG, bold=F_BOLD, italic=F_ITAL, boldItalic=F_BI
    )
    _FONTS_REGISTERED = True


# --------------------------------------------------------------------------
# Inline **bold** parsing -> list of (text, bold) runs
# --------------------------------------------------------------------------
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def parse_runs(text, base_bold=False):
    """Turn 'a **b** c' into [('a ',F),('b',T),(' c',F)] respecting base_bold."""
    runs = []
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], base_bold))
        runs.append((m.group(1), not base_bold if base_bold else True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], base_bold))
    if not runs:
        runs = [("", base_bold)]
    return runs


def _font_for(bold, italic):
    if bold and italic:
        return F_BI
    if bold:
        return F_BOLD
    if italic:
        return F_ITAL
    return F_REG


def _run_width(runs, size, italic=False):
    return sum(stringWidth(t, _font_for(b, italic), size) for t, b in runs)


def wrap_runs(runs, size, max_w, italic=False):
    """Word-wrap a list of (text,bold) runs into lines that fit max_w.
    Returns list of lines, each a list of (word_or_space, bold)."""
    # tokenize into words with trailing spaces, preserving bold flags
    tokens = []
    for text, bold in runs:
        parts = re.split(r"(\s+)", text)
        for p in parts:
            if p:
                tokens.append((p, bold))
    lines = []
    cur = []
    cur_w = 0.0
    for tok, bold in tokens:
        w = stringWidth(tok, _font_for(bold, italic), size)
        if tok.strip() == "":  # whitespace
            if cur:  # don't start a line with space
                cur.append((tok, bold))
                cur_w += w
            continue
        if cur_w + w > max_w and cur:
            # trim trailing space
            while cur and cur[-1][0].strip() == "":
                cur_w -= stringWidth(cur[-1][0], _font_for(cur[-1][1], italic), size)
                cur.pop()
            lines.append(cur)
            cur = [(tok, bold)]
            cur_w = w
        else:
            cur.append((tok, bold))
            cur_w += w
    if cur:
        while cur and cur[-1][0].strip() == "":
            cur.pop()
        lines.append(cur)
    return lines if lines else [[]]


# --------------------------------------------------------------------------
# Layout model: build a flat list of "ops" so we can both measure & draw
# --------------------------------------------------------------------------
class Layout:
    """Computes drawing operations and total height without drawing."""

    def __init__(self, data):
        register_fonts()
        self.d = data
        self.ops = []   # each: dict describing something to draw at a y offset
        self.y = MARGIN_T
        self.content_bottom = MARGIN_T  # updated as real ink is placed
        self._build()

    # helpers -------------------------------------------------------------
    def _advance(self, dy):
        # The ink for the line just placed ends roughly at current y + glyph
        # bottom; track the largest real ink extent for accurate fit checks.
        self.content_bottom = max(self.content_bottom, self.y + SZ_BODY)
        self.y += dy

    def _center_text(self, text, font, size, extra_after):
        self.ops.append({"kind": "center", "text": text, "font": font,
                         "size": size, "top": self.y})
        self._advance(size + extra_after)

    def _heading(self, text):
        self._advance(GAP_BEFORE_HEADING)
        self.ops.append({"kind": "heading", "text": text.upper(),
                         "top": self.y})
        # heading occupies size, then a rule below
        self._advance(SZ_HEADING + GAP_HEADING_RULE)
        self.ops.append({"kind": "rule", "top": self.y})
        self._advance(GAP_AFTER_RULE)

    def _para(self, text, base_bold=False, italic=False):
        runs = parse_runs(text, base_bold=base_bold)
        lines = wrap_runs(runs, SZ_BODY, CONTENT_W, italic=italic)
        for ln in lines:
            self.ops.append({"kind": "line", "runs": ln, "x": MARGIN_L,
                             "top": self.y, "italic": italic})
            self._advance(LEAD_BODY)

    def _bullet(self, text, base_bold=False):
        runs = parse_runs(text, base_bold=base_bold)
        avail = CONTENT_W - BULLET_INDENT
        lines = wrap_runs(runs, SZ_BODY, avail)
        for i, ln in enumerate(lines):
            self.ops.append({
                "kind": "bullet_line",
                "runs": ln,
                "top": self.y,
                "first": (i == 0),
            })
            self._advance(LEAD_BODY)

    def _company_line(self, left_bold, left_ital, right_text):
        """company | title  ........................  location | dates
        Rendered on ONE text line: left run(s) left-aligned, right run
        right-aligned. Still linear text for ATS (space-separated)."""
        self.ops.append({
            "kind": "company",
            "left_bold": left_bold,      # e.g. 'Netflix'
            "left_ital": left_ital,      # e.g. 'Data Analyst'
            "right": right_text,         # e.g. 'CA | October 2024 – Present'
            "top": self.y,
        })
        self._advance(LEAD_BODY)

    # build ---------------------------------------------------------------
    def _build(self):
        d = self.d
        # Header
        self._center_text(d["name"], F_BOLD, SZ_NAME, GAP_AFTER_NAME)
        self._center_text(d["title"], F_BOLD, SZ_TITLE, GAP_AFTER_TITLE)
        self._center_text(d["contact"], F_REG, SZ_CONTACT, GAP_AFTER_CONTACT)

        # Summary
        self._heading("Summary")
        self._para(d["summary"])

        # Technical skills (one per line; label may be bolded via **)
        self._heading("Technical Skills")
        for line in d["skills"]:
            if line.strip():
                self._para(line)

        # Professional experience
        self._heading("Professional Experience")
        for job in d["experience"]:
            self._company_line(job["company"], job["job_title"], job["meta"])
            for b in job["bullets"]:
                if b.strip():
                    self._bullet(b)

        # Education
        self._heading("Education")
        for line in d["education"]:
            if line.strip():
                self._para(line)

        # Certifications
        self._heading("Certifications")
        for line in d["certifications"]:
            if line.strip():
                self._bullet(line)

    # public --------------------------------------------------------------
    def total_height(self):
        return self.y

    def used_points(self):
        """Real ink height consumed, from top margin to last glyph bottom."""
        return self.content_bottom - MARGIN_T

    def capacity_points(self):
        # Usable vertical space from top margin to the printable bottom.
        return (PAGE_H - MARGIN_B) - MARGIN_T

    def fits(self):
        # Last glyph bottom must not cross the printable bottom edge.
        return self.content_bottom <= (PAGE_H - MARGIN_B) + 0.3

    def page_count(self):
        # How many pages the content will actually occupy after pagination.
        _, pages = paginate(self.ops)
        return pages

    def fill_pct_of_page(self):
        # Fraction of a single page the content fills (can exceed 100).
        cap = (PAGE_H - MARGIN_B) - MARGIN_T
        return round(100.0 * (self.content_bottom - MARGIN_T) / cap, 1)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _draw_line_runs(c, runs, x, baseline_y, size, italic=False):
    cx = x
    for tok, bold in runs:
        font = _font_for(bold, italic)
        c.setFont(font, size)
        c.drawString(cx, baseline_y, tok)
        cx += stringWidth(tok, font, size)


def render(layout, c):
    register_fonts()
    ops, num_pages = paginate(layout.ops)
    current_page = 1
    for op in ops:
        # page break when this op belongs to a later page
        while op.get("page", 1) > current_page:
            c.showPage()
            current_page += 1
        top = op["ptop"]          # within-page top-origin coordinate
        kind = op["kind"]

        if kind == "center":
            size = op["size"]
            baseline = PAGE_H - top - size
            c.setFont(op["font"], size)
            w = stringWidth(op["text"], op["font"], size)
            c.drawString((PAGE_W - w) / 2, baseline, op["text"])

        elif kind == "heading":
            size = SZ_HEADING
            baseline = PAGE_H - top - size
            c.setFont(F_BOLD, size)
            c.drawString(MARGIN_L, baseline, op["text"])

        elif kind == "rule":
            y = PAGE_H - top
            c.setLineWidth(RULE_WIDTH)
            c.line(MARGIN_L, y, PAGE_W - MARGIN_R, y)

        elif kind == "line":
            baseline = PAGE_H - top - SZ_BODY
            _draw_line_runs(c, op["runs"], op["x"], baseline, SZ_BODY,
                            italic=op.get("italic", False))

        elif kind == "bullet_line":
            baseline = PAGE_H - top - SZ_BODY
            if op["first"]:
                c.setFont(F_REG, SZ_BODY)
                c.drawString(MARGIN_L, baseline, BULLET)
            _draw_line_runs(c, op["runs"], MARGIN_L + BULLET_INDENT,
                            baseline, SZ_BODY)

        elif kind == "company":
            baseline = PAGE_H - top - SZ_BODY
            cx = MARGIN_L
            c.setFont(F_BOLD, SZ_BODY)
            c.drawString(cx, baseline, op["left_bold"])
            cx += stringWidth(op["left_bold"], F_BOLD, SZ_BODY)
            sep = " | "
            c.setFont(F_BOLD, SZ_BODY)
            c.drawString(cx, baseline, sep)
            cx += stringWidth(sep, F_BOLD, SZ_BODY)
            c.setFont(F_BI, SZ_BODY)
            c.drawString(cx, baseline, op["left_ital"])
            c.setFont(F_BOLD, SZ_BODY)
            rw = stringWidth(op["right"], F_BOLD, SZ_BODY)
            c.drawString(PAGE_W - MARGIN_R - rw, baseline, op["right"])

    return num_pages


# --------------------------------------------------------------------------
# Pagination: assign each op a page index + within-page top.
# --------------------------------------------------------------------------
PAGE_BOTTOM = PAGE_H - MARGIN_B          # lowest y (top-origin) content may use
USABLE_H = PAGE_BOTTOM - MARGIN_T        # height of the content area per page


def _op_height(op):
    """Vertical space an op occupies before the next op (approx, top-origin)."""
    k = op["kind"]
    if k == "center":
        return op["size"]
    if k == "heading":
        return SZ_HEADING
    if k == "rule":
        return 0.0
    return SZ_BODY


def paginate(ops):
    """Walk ops (which carry a continuous 'top') and reflow them onto pages.
    Returns the same ops with 'page' and 'ptop' (within-page top) set.

    Rules:
      * When an op's bottom would exceed PAGE_BOTTOM, start a new page.
      * A section 'heading' (with its 'rule' and the FIRST following content
        line) is kept together so a heading never sits alone at a page foot.
    """
    if not ops:
        return ops, 1

    # group indices that must stay together (heading + rule + first content)
    keep_with_next = set()
    for i, op in enumerate(ops):
        if op["kind"] == "heading":
            # this heading, its rule, and the first real content after it
            keep_with_next.add(i)                     # heading -> rule
            j = i + 1
            if j < len(ops) and ops[j]["kind"] == "rule":
                keep_with_next.add(j)                 # rule -> first content
                # also keep the first content line with it (already covered by j)

    # We reflow by walking and tracking the running within-page cursor.
    # base_top of the current page's first op in the ORIGINAL continuous space.
    page = 1
    # offset subtracted from continuous 'top' to get within-page top
    offset = ops[0]["top"] - MARGIN_T
    i = 0
    n = len(ops)
    while i < n:
        op = ops[i]
        ptop = op["top"] - offset
        bottom = ptop + _op_height(op)

        # does this op (or its keep-together group) overflow the page?
        overflow = bottom > PAGE_BOTTOM + 0.5

        # if this op must keep with next, check the group's combined bottom
        if not overflow and i in keep_with_next:
            # find the end of the keep-with-next chain starting at i
            k = i
            while k in keep_with_next and k + 1 < n:
                k += 1
            group_bottom = (ops[k]["top"] - offset) + _op_height(ops[k])
            if group_bottom > PAGE_BOTTOM + 0.5:
                overflow = True

        if overflow and ptop > MARGIN_T + 0.5:
            # push this op to the top of a new page
            page += 1
            offset = op["top"] - MARGIN_T
            ptop = MARGIN_T

        op["page"] = page
        op["ptop"] = ptop
        i += 1

    return ops, page
def build_pdf_bytes(data):
    register_fonts()
    layout = Layout(data)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(data.get("filename", "resume"))
    c.setAuthor(data.get("name", ""))
    render(layout, c)      # draws page breaks internally as needed
    c.showPage()           # finish the final page
    c.save()
    buf.seek(0)
    return buf.read()


def build_pdf(data, out_path):
    pdf = build_pdf_bytes(data)
    with open(out_path, "wb") as f:
        f.write(pdf)
    return out_path


def estimate_fits_one_page(data):
    layout = Layout(data)
    return layout.used_points(), layout.capacity_points(), layout.fits()


def estimate_pages(data):
    """Return (page_count, fill_pct_of_one_page) for the meter."""
    layout = Layout(data)
    return layout.page_count(), layout.fill_pct_of_page()


# --------------------------------------------------------------------------
# Default (pre-filled) data derived from the reference resume
# --------------------------------------------------------------------------
DEFAULT_DATA = {
    "filename": "Anandagani_Reddy_Data_Analyst_Resume",
    "name": "Anandaganireddy Nallamilli",
    "title": "Data Analyst | Analytics Engineering",
    "contact": "San Jose, CA | anandaganireddy@gmail.com | (216) 678-9519 | LinkedIn",
    "summary": (
        "Data Analyst with **5+ years** of experience driving product and business "
        "impact through data, specialising in **SQL, Python, Tableau, Power BI**, and "
        "**experimentation (A/B testing)** across streaming media and financial services "
        "platforms. Proven ability to translate complex datasets into scalable insights, "
        "**data products, and KPI frameworks** that influence product strategy and "
        "executive decision-making. Demonstrated success in **predictive forecasting, "
        "fraud analytics, and ETL optimisation**, delivering measurable revenue impact, "
        "improved user engagement, and operational efficiency at scale. Strong partner to "
        "Product, Engineering, and Finance, leading **end-to-end analytics** from problem "
        "framing to experimentation and production deployment."
    ),
    "skills": [
        "**Programming & Querying:** SQL (Advanced), Python (Pandas, NumPy, PySpark), R, Advanced Excel (Power Query, Power Pivot, VBA)",
        "**Data Analysis & Statistical Methods:** Exploratory Data Analysis (EDA), Statistical Analysis, Hypothesis Testing, A/B Testing, Bayesian Analysis, Regression Analysis, Cohort Analysis, Funnel Analysis, Root Cause Analysis, Data Mining",
        "**Machine Learning & Advanced Analytics:** Predictive Modeling, Time Series Forecasting (Prophet, ARIMA), Classification, Feature Engineering, Anomaly Detection, scikit-learn, Model Validation",
        "**Business Intelligence & Visualisation:** Tableau, Power BI, Looker (LookML), Amazon QuickSight, Google Data Studio, KPI Dashboards, Data Storytelling, Executive Reporting, Self-Service Analytics",
        "**Databases & Cloud Platforms:** Snowflake, Amazon Redshift, Google BigQuery, PostgreSQL, AWS (S3, Athena, Glue, EMR, Lambda), Databricks",
        "**Data Engineering & ETL:** ETL/ELT Pipelines, Apache Spark, dbt, Apache Airflow, Data Warehousing, Dimensional Modelling, Star Schema, Data Quality Monitoring, Data Lineage, Data Governance",
        "**Product & Business Analytics:** Experiment Design, Customer Segmentation, Fraud Analytics, Pricing Analytics, Retention Analytics",
        "**Tools & Methodologies:** Git, JIRA, Confluence, REST APIs, Agile/Scrum, Google Workspace",
    ],
    "experience": [
        {
            "company": "Netflix",
            "job_title": "Data Analyst",
            "meta": "CA | October 2024 \u2013 Present",
            "bullets": [
                "Architected a content-performance and subscriber-retention framework using **Tableau and Looker (LookML)**, tracking **DAU, MAU, CTR, churn,** and **LTV** across **250M+ global subscribers**, contributing to a **9% improvement in platform retention**.",
                "Designed and executed large-scale **A/B testing** frameworks using **Bayesian analysis and hypothesis testing** across UI ranking and content recommendation surfaces, driving a **12% increase in session duration** and **8% growth in CTR**.",
                "Developed time-series forecasting models using **Prophet, ARIMA, and cross-validation** to predict content demand and subscriber growth, improving planning efficiency by **20%** and directly supporting a **$2M+ content acquisition decision**.",
                "Optimised **SQL-based ETL pipelines** using **AWS S3, Redshift, Athena, and EMR**, processing **5+ PB of daily streaming data** and reducing query latency by **40%**, improving **data freshness** for the recommendation engine and personalisation models used across all surfaces.",
                "Implemented **AI-driven anomaly detection** and **data validation workflows** using **Python, PySpark, and Databricks**, reducing manual quality checks by **50%** and improving reporting accuracy for **viewing hours, LTV,** and engagement metrics across **15+ stakeholder teams**.",
                "Collaborated with cross-functional **Agile/Scrum** teams using **JIRA and Confluence** to deliver content performance insights and audience behaviour analysis, accelerating stakeholder reporting cycles by **25%** and reducing ad hoc data requests by **30%**.",
            ],
        },
        {
            "company": "JPMorgan Chase & Co",
            "job_title": "Data Analyst",
            "meta": "India | March 2019 \u2013 June 2023",
            "bullets": [
                "Analysed terabyte-scale **financial transaction datasets** using **SQL, Python (Pandas, NumPy), and AWS (Redshift, Athena, Glue)**, identifying **fraud patterns** and **risk signals** that reduced false-positive rates and protected **$1.5M+ in annual fraud exposure**.",
                "Designed **star schema data models** and interactive **Power BI dashboards** for Finance and Risk teams, reducing reporting turnaround time by **30%** and enabling **real-time KPI** visibility for **10+ analysts** across quarterly business review cycles.",
                "Built predictive risk-scoring and regression models using **scikit-learn and Python**, evaluating digital banking initiatives through **A/B testing and hypothesis testing**, contributing to a **15% increase in customer conversion rates** and **$500K+ in incremental revenue**.",
                "Automated data cleansing, transformation, and reconciliation workflows using **Python, SQLAlchemy, and SQL**, reducing manual analyst effort by **40%** and improving data accuracy for **executive-level financial reporting** and **forecasting cycles**.",
                "Implemented data governance, quality controls, and **SOX-compliant** reporting processes within **AWS** analytics environments, ensuring regulatory compliance, audit readiness, and enterprise-level data integrity across global banking operations.",
                "Delivered executive **dashboards, ad hoc analysis,** and **financial performance** scorecards using **Power BI and SQL**, translating complex financial datasets into actionable insights for **C-suite and cross-functional stakeholders** across **3 global banking regions**.",
            ],
        },
    ],
    "education": [
        "**Master\u2019s in Business Analytics** | Kent State University, Kent, OH, USA. August 2023 \u2013 December 2024",
        "**Master of Business Administration (MBA)** | Lovely Professional University, India.",
    ],
    "certifications": [
        "**Microsoft Certified**: Power BI Data Analyst Associate (PL-300)",
        "**Certified Python Business Analyst (CPBA)** \u2014 Henry Harvin",
    ],
}


if __name__ == "__main__":
    used, cap, fits = estimate_fits_one_page(DEFAULT_DATA)
    print(f"used={used:.1f}pt capacity={cap:.1f}pt fits={fits} "
          f"({100*used/cap:.1f}% of page)")
    build_pdf(DEFAULT_DATA, "/home/claude/resume_generator/_test_output.pdf")
    print("wrote _test_output.pdf")
