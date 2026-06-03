import google.generativeai as genai
import json
import os
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from urllib.parse import urlparse


# No hardcoded key. The key comes from the user (entered in the UI) or, as a
# convenience for local runs, from the GEMINI_API_KEY environment variable.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL_NAME = "gemini-3.1-flash-lite-preview"

## backup models - 1. gemini-3.1-flash-lite-preview, 2. gemini-3.1-flash-lite

model = None  # built once a key is provided via configure_gemini()

# Configure at import only if an env key exists (optional, for CLI/local use)
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(MODEL_NAME)
    except Exception:
        model = None


def configure_gemini(api_key):
    """
    Configure Gemini with the user-provided API key (from the UI), or fall back
    to the GEMINI_API_KEY environment variable. There is NO built-in key.
    Rebuilds the global model. Returns (ok: bool, message: str).
    """
    global model
    key = (api_key or "").strip() or GEMINI_API_KEY
    if not key:
        model = None
        return False, ("No Gemini API key provided. Enter your Google Gemini "
                       "API key in the sidebar to run an analysis.")
    try:
        genai.configure(api_key=key)
        model = genai.GenerativeModel(MODEL_NAME)
        return True, "Gemini configured."
    except Exception as e:
        return False, f"Could not configure Gemini: {e}"

# ── LOAD FUNDING PROMPT ──────────────────────────────────────
# The funding prompt lives in prompt.txt. Edit that file to change how the
# funding analysis behaves. It must contain a {CONTEXT} placeholder where the
# company info + scraped content gets injected.
_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.txt")

_FALLBACK_PROMPT = (
    "You are a funding & investment research analyst. From the content below, "
    "extract ONLY funding/investment data and return ONLY this JSON: "
    "{\"company_type\":\"\",\"publicly_traded\":\"\",\"ticker\":\"\","
    "\"funding_rounds\":[],\"total_funding\":\"\",\"valuation\":\"\","
    "\"key_investors\":[],\"revenue_analysis\":\"\",\"investment_focus\":\"\","
    "\"recent_milestones\":[],\"financial_health\":\"\",\"dxw_implication\":\"\","
    "\"sources_referenced\":[]}. Use \"INSUFFICIENT DATA\" when unknown.\n\n{CONTEXT}"
)


def load_prompt():
    """Read the funding prompt from prompt.txt; fall back to a built-in minimal one."""
    try:
        with open(_PROMPT_PATH, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        if "{CONTEXT}" not in text:
            text += "\n\n{CONTEXT}"
        return text
    except Exception as e:
        print(f"  WARN: could not read prompt.txt ({e}); using fallback prompt")
        return _FALLBACK_PROMPT



# ═══════════════════════════════════════════════════════════════
# URL HELPERS
# ═══════════════════════════════════════════════════════════════
def normalize_url(url):
    """Normalize URL for dedup comparison."""
    url = url.strip().lower()
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}{path}"


def is_duplicate(url, fetched_set):
    normalized = normalize_url(url)
    if normalized in fetched_set:
        return True
    no_scheme = normalized.split("://", 1)[-1]
    for existing in fetched_set:
        if existing.split("://", 1)[-1] == no_scheme:
            return True
    return False


def fetch_url_content(url, max_chars=2500):
    """Fetch and clean text from a single URL."""
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        resp = session.get(url.strip(), timeout=12, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "meta", "noscript", "iframe"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ", strip=True).split())
        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        print(f"  ✓ Fetched {url} — {len(text)} chars")
        return text
    except Exception as e:
        print(f"  ✗ Could not fetch {url}: {e}")
        return ""


def clean_urls(raw_urls):
    """Split multi-line URLs and deduplicate."""
    cleaned = []
    for raw in raw_urls:
        raw = str(raw).strip()
        if not raw or raw.lower() in ["nan", "none"]:
            continue
        for sep in ["\n", "\r", ",", " "]:
            if sep in raw:
                for p in raw.split(sep):
                    p = p.strip()
                    if p.startswith("http"):
                        cleaned.append(p)
                break
        else:
            if raw.startswith("http"):
                cleaned.append(raw)
    seen, unique = set(), []
    for u in cleaned:
        n = normalize_url(u)
        if n not in seen:
            seen.add(n)
            unique.append(u)
    return unique


# ═══════════════════════════════════════════════════════════════
# FUNDING & INVESTMENT DATA SOURCES (free, scrape-friendly)
# ═══════════════════════════════════════════════════════════════
SEC_HEADERS = {
    "User-Agent": "DataXWorks Lead Intelligence research@dataxworks.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}


def _sec_get(url, host="www.sec.gov"):
    h = dict(SEC_HEADERS)
    h["Host"] = host
    return requests.get(url, headers=h, timeout=15)


# ── 1. SEC EDGAR ─────────────────────────────────────────────
def fetch_sec_edgar(company_name, max_filings=6):
    """
    Look up a company on SEC EDGAR and pull recent funding-relevant filings
    (10-K, 10-Q, S-1, 8-K, Form D). Returns (content_block, sources).
    US-registered / SEC-reporting companies only. No API key required.
    """
    import xml.etree.ElementTree as ET

    name = company_name.strip()
    if not name:
        return "", []

    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&company={requests.utils.quote(name)}"
        "&type=&dateb=&owner=include&count=10&output=atom"
    )
    try:
        resp = _sec_get(url)
        if resp.status_code != 200 or "<feed" not in resp.text:
            print(f"  ✗ SEC EDGAR: no match for '{name}'")
            return "", []

        xml = resp.text.replace('xmlns="http://www.w3.org/2005/Atom"', "")
        root = ET.fromstring(xml)

        info = root.find("company-info")
        lines = [f"SEC EDGAR registrant found for '{name}':"]
        cik = ""
        if info is not None:
            cik = (info.findtext("cik") or "").strip()
            conformed = (info.findtext("conformed-name") or "").strip()
            sic = (info.findtext("assigned-sic-desc") or "").strip()
            state = (info.findtext("state-of-incorporation") or "").strip()
            lines.append(f"Registrant: {conformed} | CIK: {cik} | SIC: {sic} | Incorporated: {state}")

        FUNDING_FORMS = ("10-K", "10-Q", "S-1", "424B4", "8-K", "D", "20-F", "6-K")
        count = 0
        for e in root.findall("entry"):
            ftype = (e.findtext("content/filing-type") or "").strip()
            fdate = (e.findtext("content/filing-date") or "").strip()
            fhref = (e.findtext("content/filing-href") or "").strip()
            if not ftype or not any(ftype.startswith(f) for f in FUNDING_FORMS):
                continue
            lines.append(f"- {ftype} filed {fdate} | {fhref}")
            count += 1
            if count >= max_filings:
                break

        if count == 0 and info is None:
            return "", []

        block = "\n".join(lines)
        src = [{"url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
                "type": "SEC EDGAR"}] if cik else []
        print(f"  ✓ SEC EDGAR: {count} filings found for '{name}'")
        return block, src
    except Exception as e:
        print(f"  ✗ SEC EDGAR failed for '{name}': {e}")
        return "", []


# ── 2. WIKIPEDIA + WIKIDATA ──────────────────────────────────
def fetch_wikipedia_funding(company_name):
    """Wikipedia intro + Wikidata structured financials. Free, no key."""
    name = company_name.strip()
    if not name:
        return "", []

    headers = {"User-Agent": "DataXWorks Lead Intelligence research@dataxworks.com"}
    lines, sources = [], []
    try:
        s = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": name,
                    "format": "json", "srlimit": 1},
            headers=headers, timeout=12,
        ).json()
        hits = s.get("query", {}).get("search", [])
        if not hits:
            print(f"  ✗ Wikipedia: no page for '{name}'")
            return "", []
        title = hits[0]["title"]

        ex = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "prop": "extracts|pageprops",
                    "exintro": 1, "explaintext": 1, "redirects": 1,
                    "titles": title, "format": "json"},
            headers=headers, timeout=12,
        ).json()
        pages = ex.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        extract = (page.get("extract") or "").strip()
        if extract:
            lines.append(f"Wikipedia ({title}): {extract[:1500]}")
        wd_id = page.get("pageprops", {}).get("wikibase_item", "")
        sources.append({"url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}", "type": "Wikipedia"})

        if wd_id:
            wd = requests.get(
                f"https://www.wikidata.org/wiki/Special:EntityData/{wd_id}.json",
                headers=headers, timeout=12,
            ).json()
            claims = wd.get("entities", {}).get(wd_id, {}).get("claims", {})

            def _amount(pid):
                try:
                    v = claims[pid][0]["mainsnak"]["datavalue"]["value"]
                    return v.get("amount", "").lstrip("+")
                except Exception:
                    return ""
            rev = _amount("P2139")     # total revenue
            assets = _amount("P2403")  # total assets
            if rev:
                lines.append(f"Wikidata total revenue: {rev}")
            if assets:
                lines.append(f"Wikidata total assets: {assets}")
            sources.append({"url": f"https://www.wikidata.org/wiki/{wd_id}", "type": "Wikidata"})

        block = "\n".join(lines)
        print(f"  ✓ Wikipedia/Wikidata: {len(block)} chars for '{title}'")
        return block, sources
    except Exception as e:
        print(f"  ✗ Wikipedia failed for '{name}': {e}")
        return "", []


# ── MULTI-ENGINE WEB SEARCH (DuckDuckGo + Bing fallback) ─────
import re as _re
from urllib.parse import quote_plus, urlparse as _uparse, parse_qs, unquote


def _bing_search(query, max_results=8):
    """Keyless Bing HTML search fallback. Returns [{url,title,snippet}]."""
    out = []
    try:
        url = "https://www.bing.com/search?q=" + quote_plus(query) + "&count=" + str(max_results)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"}
        r = requests.get(url, headers=headers, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        for li in soup.select("li.b_algo")[:max_results]:
            a = li.find("a", href=True)
            if not a:
                continue
            link = a["href"]
            title = a.get_text(" ", strip=True)
            p = li.find("p")
            snippet = p.get_text(" ", strip=True) if p else ""
            if link.startswith("http"):
                out.append({"url": link, "title": title, "snippet": snippet})
    except Exception as e:
        print(f"  Bing search failed: {e}")
    return out


def _web_search(query, max_results=8):
    """
    Open-web search across multiple engines. Tries DuckDuckGo news, then
    DuckDuckGo text, then Bing HTML. Returns merged, deduped results.
    No API key required for any engine.
    """
    results, seen = [], set()

    def _add(rows, src):
        for r in rows:
            u = r.get("url") or r.get("href", "")
            if not u or not u.startswith("http"):
                continue
            n = normalize_url(u)
            if n in seen:
                continue
            seen.add(n)
            results.append({
                "url": u,
                "title": r.get("title", ""),
                "snippet": r.get("body") or r.get("excerpt") or r.get("snippet", ""),
                "engine": src,
            })

    # 1) DuckDuckGo news (freshest)
    try:
        _add(list(DDGS().news(query, max_results=max_results)), "ddg-news")
    except Exception:
        pass
    # 2) DuckDuckGo text
    try:
        _add(list(DDGS().text(query, max_results=max_results)), "ddg-text")
    except Exception:
        pass
    # 3) Bing fallback - always run so we widen the net (maximum reach)
    _add(_bing_search(query, max_results=max_results), "bing")

    return results


# ── 3. TUNED FUNDING / PRESS SEARCH ──────────────────────────
def search_funding(company_name, fetched_set, max_results=8, max_to_fetch=18, location=""):
    """
    Broad, multi-angle OPEN-WEB funding sweep across multiple engines
    (DuckDuckGo + Bing). Casts a wide net over every funding-related angle,
    scrapes the best hits (maximum reach), and keeps all snippets. Ranking is
    handled downstream (rank-then-cap) - nothing is hard-dropped by keyword.
    `location` biases regional queries.
    """
    name = company_name.strip()
    if not name:
        return [], []

    loc = (location or "").strip()
    sfx = f" {loc}" if loc else ""

    # MANY funding angles - rounds, investors, valuation, revenue, M&A, debt,
    # grants, crowdfunding, IPO, regional press, aggregator pages.
    queries = [
        f"{name} funding round{sfx}",
        f"{name} raises million Series A B C D{sfx}",
        f"{name} total funding raised to date",
        f"{name} valuation latest{sfx}",
        f"{name} investors backers lead investor",
        f"{name} venture capital investment{sfx}",
        f"{name} seed round angel investors",
        f"{name} funding announcement press release{sfx}",
        f"{name} acquisition merger deal",
        f"{name} IPO public offering",
        f"{name} annual revenue earnings financial results",
        f"{name} debt financing credit facility",
        f"{name} grant award government funding{sfx}",
        f"{name} crowdfunding campaign raised",
        f"{name} where investing capital expansion R&D",
        f"{name} news {loc}".strip(),
        f"site:crunchbase.com {name}",
        f"site:pitchbook.com {name}",
        f"site:tracxn.com {name}",
        f"site:techcrunch.com {name} funding",
    ]

    TRUSTED = (
        "techcrunch.com", "reuters.com", "bloomberg.com", "wsj.com", "ft.com",
        "forbes.com", "cnbc.com", "businesswire.com", "prnewswire.com",
        "globenewswire.com", "finsmes.com", "crunchbase.com", "pitchbook.com",
        "tracxn.com", "sec.gov", "venturebeat.com", "axios.com",
        "theinformation.com", "sifted.eu", "tech.eu", "eu-startups.com",
        "businessinsider.com", "fortune.com", "cnn.com", "nytimes.com",
        "theverge.com", "yahoo.com", "marketwatch.com", "seekingalpha.com",
        "inc42.com", "entrackr.com", "yourstory.com", "livemint.com",
        "economictimes.indiatimes.com", "moneycontrol.com",
    )

    def _is_trusted(url):
        u = url.lower()
        return any(d in u for d in TRUSTED)

    blocks, sources, results = [], [], []
    seen = set()
    for q in queries:
        try:
            print(f"  Web sweep: {q}")
            for r in _web_search(q, max_results=max_results):
                n = normalize_url(r["url"])
                if n in seen:
                    continue
                seen.add(n)
                r["trusted"] = _is_trusted(r["url"])
                results.append(r)
        except Exception as e:
            print(f"  Web sweep failed '{q}': {e}")

    print(f"  Web sweep: {len(results)} unique results across engines")

    # Trusted first, then everything else (ranking, not exclusion)
    results.sort(key=lambda x: not x.get("trusted", False))

    # Enrich (scrape) up to max_to_fetch pages - maximum reach
    fetched = 0
    for item in results:
        if fetched >= max_to_fetch:
            break
        if is_duplicate(item["url"], fetched_set):
            continue
        content = fetch_url_content(item["url"], max_chars=1800)
        if content and len(content) > 100:
            tag = "Trusted News" if item.get("trusted") else "Web"
            blocks.append(f"[SOURCE: {item['url']}]\n[TYPE: {tag}]\n[TITLE: {item.get('title','')}]\n{content}")
            sources.append({"url": item["url"], "type": tag})
            fetched_set.add(normalize_url(item["url"]))
            fetched += 1

    # Keep ALL remaining snippets too - downstream ranking decides
    for item in results:
        if not is_duplicate(item["url"], fetched_set) and item.get("snippet"):
            tag = "Trusted Snippet" if item.get("trusted") else "Web Snippet"
            blocks.append(f"[SOURCE: {item['url']}]\n[TYPE: {tag}]\n[TITLE: {item.get('title','')}]\n{item['snippet']}")
            sources.append({"url": item["url"], "type": tag})
            fetched_set.add(normalize_url(item["url"]))

    print(f"  Web sweep: {len(blocks)} blocks gathered "
          f"({sum(1 for s in sources if 'Trusted' in s['type'])} trusted) "
          f"from {fetched} scraped pages")
    return blocks, sources


# ── 4. CLIENT-DOMAIN: SMART LINK DISCOVERY (no hard filters) ──
# We DO NOT hard-reject pages by keyword. Instead we compute a soft RELEVANCE
# SCORE used only to RANK content - low-scoring pages still pass through, just
# lower in priority, so we never throw away data that might be useful. The
# signal set is built dynamically from a small seed of root concepts (expanded
# with morphological variants) plus the company name, rather than a fixed list.

# Seed concepts only - expanded at runtime into many variants. Editing the
# prompt or this seed is optional; nothing here hard-blocks content.
_SIGNAL_SEEDS = [
    "fund", "financ", "invest", "capital", "round", "series", "valuation",
    "shareholder", "equity", "venture", "ipo", "acquisi", "merger", "revenue",
    "earning", "profit", "raise", "backer", "stake", "grant", "subsid",
    "annual report", "10-k", "10k", "balance sheet", "press", "news",
    "investor", "ir", "stock", "ticker", "dividend", "deploy", "allocat",
]


def _build_signals(company_name=""):
    """Build a dynamic signal set: seeds + simple variants + company tokens."""
    sigs = set()
    for s in _SIGNAL_SEEDS:
        s = s.lower().strip()
        sigs.add(s)
        # cheap morphological variants so we are not tied to one exact spelling
        sigs.update({s + "ing", s + "ed", s + "s", s + "ment", s + "ation"})
    # company-name tokens help keep on-topic pages
    for tok in str(company_name or "").lower().replace("-", " ").split():
        if len(tok) > 2:
            sigs.add(tok)
    return sigs


def _relevance_score(text, signals):
    """Soft score = how many distinct signals appear. Used for RANKING only."""
    low = (text or "").lower()
    return sum(1 for s in signals if s and s in low)


def _discover_funding_links(base_url, html, signals, max_links=12):
    """
    Parse homepage HTML and RANK funding/investor/press links by signal score.
    Accepts same-site, subdomain, and external IR hosts. Threshold is minimal
    (>0) so we keep a wide net; ranking decides priority, not exclusion.
    """
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.replace("www.", "").lower()
    root = ".".join(base_host.split(".")[-2:]) if "." in base_host else base_host

    scored = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        host = parsed.netloc.replace("www.", "").lower()
        same_site = root and root in host
        ext_ir = any(x in host for x in ("q4web", "q4inc", "sec.gov", "investis", "irplus"))
        if not (same_site or ext_ir):
            continue
        anchor = a.get_text(" ", strip=True)
        s = _relevance_score(full + " " + anchor, signals)
        key = full.split("#")[0].rstrip("/")
        # keep even score-0 same-site links, but rank them last
        if key not in scored or s > scored[key]:
            scored[key] = s

    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    return [u for u, _ in ranked[:max_links]]


def crawl_client_domain_funding(client_domain, fetched_set, company_name=""):
    """
    Discover and scrape pages from a domain WITHOUT hardcoded paths and WITHOUT
    a hard keyword gate. Every fetched page is kept (deduped, length-checked)
    and tagged with a relevance score so the caller can rank, not discard.
    """
    blocks, sources = [], []
    if not client_domain or not str(client_domain).strip():
        return blocks, sources

    signals = _build_signals(company_name)

    domain = str(client_domain).strip()
    if not domain.lower().startswith("http"):
        domain = "https://" + domain
    base = domain.rstrip("/")

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                              "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"})
        resp = session.get(base, timeout=12, allow_redirects=True)
        home_html = resp.text
    except Exception as e:
        print(f"  Client domain: could not load homepage {base}: {e}")
        home_html = ""

    # Homepage content - always kept
    home_text = fetch_url_content(base, max_chars=2200)
    if home_text and len(home_text) > 80:
        blocks.append((_relevance_score(home_text, signals),
                       f"[SOURCE: {base}]\n[TYPE: Client Domain]\n{home_text}",
                       {"url": base, "type": "Client Domain"}))
        fetched_set.add(normalize_url(base))

    discovered = _discover_funding_links(base, home_html, signals) if home_html else []
    if discovered:
        print(f"  Client domain: discovered {len(discovered)} candidate links")
    else:
        discovered = [base + p for p in ("/investors", "/investor-relations", "/news", "/about")]
        print("  Client domain: no links discovered, minimal fallback")

    fetched = 0
    for url in discovered:
        if fetched >= 8:
            break
        if is_duplicate(url, fetched_set):
            continue
        content = fetch_url_content(url, max_chars=2200)
        if content and len(content) > 80:
            # NO keyword gate - keep the page, score it for ranking only
            blocks.append((_relevance_score(content, signals),
                           f"[SOURCE: {url}]\n[TYPE: Client Domain]\n{content}",
                           {"url": url, "type": "Client Domain"}))
            fetched_set.add(normalize_url(url))
            fetched += 1

    # Rank by relevance score, return (block_text, source) pairs
    blocks.sort(key=lambda x: x[0], reverse=True)
    out_blocks = [b for _, b, _ in blocks]
    out_sources = [s for _, _, s in blocks]
    print(f"  Client domain: kept {len(out_blocks)} pages from {base} (ranked, no hard filter)")
    return out_blocks, out_sources


# ═══════════════════════════════════════════════════════════════
# BUILD FUNDING CONTENT (open-internet, all available funding data)
# ═══════════════════════════════════════════════════════════════
def build_funding_content(raw_urls, company_name, client_domain="", location=""):
    """
    Grab ALL available funding/investment information from across the internet,
    not a fixed set of lanes. Sources combined:
      - Client / provided domain (discovered investor/press/about pages)
      - SEC EDGAR filings (for US-listed entities)
      - Wikipedia + Wikidata (structured financials)
      - BROAD multi-engine open-web sweep (DuckDuckGo + Bing) across many
        funding angles: rounds, investors, valuation, revenue, M&A, debt,
        grants, crowdfunding, IPO, regional press, aggregators.
    Everything is collected, then RANKED and capped for the model (no hard
    keyword filtering). Returns (content_block, sources_list).
    """
    all_sources, content_blocks = [], []
    fetched_set = set()

    urls = clean_urls(raw_urls)

    # ── 1. Client-provided domain ──
    if client_domain:
        print(f"\n📌 Client-provided domain: {client_domain}")
        cd_blocks, cd_sources = crawl_client_domain_funding(client_domain, fetched_set, company_name)
        content_blocks.extend(cd_blocks)
        all_sources.extend(cd_sources)

    # ── 2. Authoritative registry by region (US -> SEC EDGAR) ──
    loc = (location or "").strip().lower()
    is_us = (not loc) or any(k in loc for k in ("us", "u.s", "united states", "usa", "america"))
    if is_us:
        print("\nSEC EDGAR (US region)")
        sec_block, sec_sources = fetch_sec_edgar(company_name)
        if sec_block:
            content_blocks.append(f"[SOURCE: SEC EDGAR]\n[TYPE: SEC Filings]\n{sec_block}")
            all_sources.extend(sec_sources)
    else:
        print(f"\nNon-US region ('{location}') - relying on Wikipedia + regional news search")
        # Still try SEC in case the company is US-listed anyway
        sec_block, sec_sources = fetch_sec_edgar(company_name)
        if sec_block:
            content_blocks.append(f"[SOURCE: SEC EDGAR]\n[TYPE: SEC Filings]\n{sec_block}")
            all_sources.extend(sec_sources)

    # ── 3. Wikipedia + Wikidata ──
    print("\n📌 Wikipedia / Wikidata")
    wiki_block, wiki_sources = fetch_wikipedia_funding(company_name)
    if wiki_block:
        content_blocks.append(f"[SOURCE: Wikipedia/Wikidata]\n[TYPE: Encyclopedia]\n{wiki_block}")
        all_sources.extend(wiki_sources)

    # ── 4. Funding / press search ──
    print("\n📌 Funding search")
    fund_blocks, fund_sources = search_funding(company_name, fetched_set, location=location)
    content_blocks.extend(fund_blocks)
    all_sources.extend(fund_sources)

    # RANK THEN CAP: score every block by funding relevance and keep the most
    # relevant first, up to a character budget - so nothing is hard-dropped by
    # keyword, but the model still gets the best material within its token room.
    signals = _build_signals(company_name)
    CONTENT_BUDGET = 30000  # maximum reach: wide net, ranked, capped for the model
    scored_blocks = sorted(content_blocks, key=lambda b: _relevance_score(b, signals), reverse=True)
    kept, used = [], 0
    for blk in scored_blocks:
        if used + len(blk) > CONTENT_BUDGET and kept:
            break
        kept.append(blk)
        used += len(blk)

    full_content = "\n\n---\n\n".join(kept) if kept else \
        "No funding content could be gathered. Use general knowledge and mark fields INSUFFICIENT DATA where unknown."

    print(f"\nFunding sources collected: {len(all_sources)}; "
          f"kept {len(kept)}/{len(content_blocks)} blocks within budget")
    return full_content, all_sources


# ═══════════════════════════════════════════════════════════════
# GEMINI — FUNDING ANALYSIS ONLY
# ═══════════════════════════════════════════════════════════════
_LAST_ERROR = ""


def _build_generation_config(max_tokens, disable_thinking=False):
    """Build a GenerationConfig, disabling 'thinking' when supported so the
    token budget goes to the actual answer (Gemini 3.x are thinking models)."""
    kwargs = {"temperature": 0.3, "max_output_tokens": max_tokens}
    if disable_thinking:
        try:
            # Supported on newer google-generativeai; ignored/errors otherwise.
            kwargs["thinking_config"] = genai.types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass
    try:
        return genai.GenerationConfig(**kwargs)
    except TypeError:
        # Older SDK without thinking_config kwarg -> drop it
        kwargs.pop("thinking_config", None)
        return genai.GenerationConfig(**kwargs)


def _extract_text(resp):
    try:
        return (resp.text or "").strip()
    except Exception:
        try:
            return "".join(
                p.text for p in resp.candidates[0].content.parts
                if hasattr(p, "text") and p.text
            ).strip()
        except Exception:
            return ""


def _finish_reason(resp):
    try:
        return getattr(resp.candidates[0], "finish_reason", None)
    except Exception:
        return None


def _call_gemini(prompt, label=""):
    global _LAST_ERROR

    if model is None:
        _LAST_ERROR = "No Gemini API key configured. Enter your key in the sidebar."
        print(f"  {label} ERROR: {_LAST_ERROR}")
        return ""

    # Try in escalating fashion: big budget; if MAX_TOKENS, retry with
    # thinking disabled and an even larger budget.
    attempts = [
        {"max_tokens": 8192, "disable_thinking": False},
        {"max_tokens": 16384, "disable_thinking": True},
        {"max_tokens": 24576, "disable_thinking": True},
    ]

    for n, cfg in enumerate(attempts, 1):
        try:
            resp = model.generate_content(
                prompt,
                generation_config=_build_generation_config(
                    cfg["max_tokens"], cfg["disable_thinking"]
                ),
            )
            text = _extract_text(resp)
            if text:
                print(f"  {label} response: {len(text)} chars (attempt {n})")
                return text

            # empty -> figure out why
            fr = _finish_reason(resp)
            pf = getattr(resp, "prompt_feedback", None)
            if pf and getattr(pf, "block_reason", None):
                _LAST_ERROR = f"Gemini prompt blocked: {pf.block_reason}"
                print(f"  {label} BLOCKED: {_LAST_ERROR}")
                return ""  # blocking won't be fixed by retrying
            # finish_reason 2 == MAX_TOKENS -> retry with bigger budget / no thinking
            if str(fr) in ("2", "FinishReason.MAX_TOKENS", "MAX_TOKENS"):
                _LAST_ERROR = "Gemini hit MAX_TOKENS (model spent budget thinking)"
                print(f"  {label} MAX_TOKENS on attempt {n}, retrying with larger budget...")
                continue
            _LAST_ERROR = f"Gemini returned no text (finish_reason={fr})"
            print(f"  {label} EMPTY: {_LAST_ERROR}")
            return ""
        except Exception as e:
            _LAST_ERROR = f"{type(e).__name__}: {e}"
            print(f"  {label} ERROR (attempt {n}): {_LAST_ERROR}")
            # network/transient -> try next attempt
            continue

    print(f"  {label} gave up after {len(attempts)} attempts: {_LAST_ERROR}")
    return ""

def _parse_json(text):
    if not text:
        return None
    c = text
    if "```json" in c:
        c = c.split("```json")[1]
    if "```" in c:
        c = c.split("```")[0]
    return json.loads(c.strip())


FUNDING_SCHEMA = '{"funding_and_investment":{"company_type":"","publicly_traded":"","ticker":"","funding_rounds":[{"round":"","amount":"","date":"","lead_investor":"","source":""}],"total_funding":"","valuation":"","key_investors":[],"revenue_analysis":"","investment_focus":"","recent_milestones":[],"financial_health":"","dxw_implication":"","sources_referenced":[]}}'


def analyze_company(company_info, urls, contacts=None, client_domain="", location=""):
    """
    Funding-only analysis. `contacts` kept for signature compatibility but unused.
    Returns (ai_result_dict, sources_list). ai_result has key 'funding_and_investment'.
    """
    company_name = company_info.get("company_name", "Unknown")
    scraped_content, sources = build_funding_content(urls, company_name, client_domain=client_domain, location=location)

    # Content is already ranked + capped inside build_funding_content (rank-then-cap).

    # Build the context block injected into the prompt's {CONTEXT} placeholder
    region_line = f"REGION (user-provided): {location}\n" if (location or "").strip() else ""
    context = (
        f"COMPANY: {company_name} | "
        f"Industry: {company_info.get('industry', 'Unknown')} | "
        f"Size: {company_info.get('employee_size', 'Unknown')} | "
        f"Revenue: {company_info.get('revenue', 'Unknown')}\n"
        f"{region_line}\n"
        f"SOURCES & CONTENT (funding-focused):\n{scraped_content}"
    )

    # Load the funding prompt from prompt.txt and inject the context
    instructions = load_prompt().replace("{CONTEXT}", context)

    print("\n" + "=" * 60)
    print("FUNDING ANALYSIS - single Gemini call")
    print("=" * 60)
    text = _call_gemini(instructions, "Funding")

    try:
        data = _parse_json(text) if text else None
        if not data:
            return _fallback(company_name), sources
        if "funding_and_investment" not in data:
            data = {"funding_and_investment": data}
        print("Funding analysis parsed")
        return data, sources
    except json.JSONDecodeError as e:
        print(f"JSON PARSE ERROR: {e}")
        return _fallback(company_name), sources
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return _fallback(company_name), sources


def _fallback(company_name):
    return {
        "funding_and_investment": {
            "executive_summary": "Analysis failed or no funding data could be gathered. Retry, or check the company name spelling.",
            "confidence_level": "Low",
            "confidence_notes": "No usable data was retrieved.",
            "entity_type": "INSUFFICIENT DATA",
            "fund_allocation": [],
            "fund_allocation_summary": "INSUFFICIENT DATA",
            "company_type": "INSUFFICIENT DATA",
            "publicly_traded": "INSUFFICIENT DATA",
            "ticker": "",
            "funding_rounds": [],
            "total_funding": "INSUFFICIENT DATA",
            "valuation": "INSUFFICIENT DATA",
            "key_investors": [],
            "revenue_analysis": "Analysis failed or no data found.",
            "investment_focus": "",
            "recent_milestones": [],
            "financial_health": "INSUFFICIENT DATA",
            "dxw_implication": "Retry - no funding data could be gathered.",
            "error": _LAST_ERROR or "Gemini returned empty/unparseable output.",
            "sources_referenced": [],
        }
    }


# ═══════════════════════════════════════════════════════════════
# DERIVE COMPANY NAME FROM DOMAIN
# ═══════════════════════════════════════════════════════════════
def normalize_domain(raw):
    """Accept messy input (Stripe, stripe.com, https://www.stripe.com/) -> clean https URL + bare host."""
    raw = str(raw or "").strip()
    if not raw:
        return "", ""
    # If it looks like a plain company name (no dot), return as-is, no URL
    if "." not in raw and " " in raw:
        return "", raw
    candidate = raw
    if not candidate.lower().startswith("http"):
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    host = (parsed.netloc or parsed.path).replace("www.", "").strip("/").lower()
    if not host:
        return "", raw
    clean_url = "https://" + host
    return clean_url, host


def _guess_name_from_host(host):
    """Fallback: turn 'data-x-works.co.uk' -> 'Data X Works'."""
    if not host:
        return ""
    core = host.split("/")[0]
    # strip common TLDs
    for tld in [".com", ".io", ".co.uk", ".ai", ".co", ".net", ".org", ".app", ".dev", ".so", ".inc"]:
        if core.endswith(tld):
            core = core[: -len(tld)]
            break
    core = core.split(".")[0]
    words = core.replace("-", " ").replace("_", " ").split()
    return " ".join(w.capitalize() for w in words) if words else ""


def derive_company_name(raw_domain):
    """
    Given a domain (or plain name), return (clean_url, best_guess_name).
    Scrapes homepage <title> + meta, falls back to the hostname.
    """
    clean_url, host = normalize_domain(raw_domain)

    # plain company name typed, no domain
    if not clean_url and host:
        return "", host

    guess = _guess_name_from_host(host)
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                              "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"})
        resp = session.get(clean_url, timeout=10, allow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Prefer og:site_name, then <title>
        og = soup.find("meta", property="og:site_name")
        title = ""
        if og and og.get("content"):
            title = og["content"].strip()
        elif soup.title and soup.title.string:
            title = soup.title.string.strip()
        if title:
            seps = ["|", ":", " - ", "\u2013", "\u2014", "\u2022", "\u00b7"]
            for sep in seps:
                if sep in title:
                    title = title.split(sep)[0].strip()
                    break
            if 1 <= len(title) <= 40:
                guess = title
        print(f"  Derived name for {host}: {guess}")
    except Exception as e:
        print(f"  Name derivation failed for {host}: {e} (using fallback '{guess}')")

    return clean_url, guess
