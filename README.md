# Source Funding Analyzer

A Streamlit app that researches a company's **funding and investment data** from
across the open internet and produces a clear, storytelling summary of how much
they have raised, who backed them, and where that money is going.

You give it one or more company domains; it finds the company, gathers funding
data from multiple sources, and returns a narrative report plus structured fields
(rounds, investors, valuation, sector-wise fund allocation, and a DataXWorks
implication).

## Features

- **Domain-first input** — paste company domains (one per line); the app derives
  the company name automatically, with a review step to correct it.
- **Open-internet research** — combines:
  - The client's own website (auto-discovered investor / press / about pages, no
    hardcoded paths)
  - SEC EDGAR filings (for US-listed entities)
  - Wikipedia + Wikidata structured financials
  - A broad multi-engine web sweep (DuckDuckGo + Bing) across many funding angles
- **Storytelling summary** — a rich, plain-English narrative of the company's
  funding journey, with a confidence level (High / Medium / Low).
- **Fund allocation** — a sector/area breakdown of where the capital goes
  (auto-detects operating company vs investor/fund).
- **Region aware** — tell it the company's region to bias sources.
- **Bring your own key** — enter your Google Gemini API key in the sidebar.

## Setup

```bash
# 1. Clone
git clone https://github.com/muthu-krish16/Source-Funding-Analyzer.git
cd Source-Funding-Analyzer

# 2. Create a virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

## Gemini API key

The app needs a Google Gemini API key (no key is bundled). Get one free at
<https://aistudio.google.com/apikey>.

You can provide it either way:

- **In the app** — paste it into the "Your Google Gemini API key" field in the
  sidebar (recommended).
- **As an environment variable** — set `GEMINI_API_KEY` before running.

```bash
# optional
export GEMINI_API_KEY="your-key-here"      # macOS / Linux
setx GEMINI_API_KEY "your-key-here"        # Windows
```

## Run

```bash
streamlit run app.py
```

Then in the browser:

1. Paste one or more company domains (e.g. `stripe.com`, `databricks.com`).
2. (Optional) Enter the region (e.g. `US`, `India`).
3. Click **Find company names**, review/correct the names.
4. Click **Analyze All** to generate the funding reports.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI and workflow |
| `analyzer.py` | Research pipeline + Gemini analysis |
| `database.py` | SQLite storage (funding reports, client domains) |
| `prompt.txt` | The funding analysis prompt (edit to tune the output) |
| `test_gemini.py` | Quick health check for your Gemini API key |
| `requirements.txt` | Python dependencies |

The SQLite database (`lead_intelligence.db`) is created automatically at runtime
and is not committed.

## Notes

- Web search relies on free engines (DuckDuckGo, Bing HTML) and can occasionally
  be rate-limited; SEC and Wikipedia provide a reliable baseline. The confidence
  badge reflects how well-verified the figures are.
- Editing `prompt.txt` changes how the analysis behaves — keep the `{CONTEXT}`
  placeholder and the JSON structure intact.
