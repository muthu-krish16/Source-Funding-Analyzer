import streamlit as st
import pandas as pd
from analyzer import analyze_company, derive_company_name, normalize_domain, configure_gemini
from database import (
    create_tables, save_report, save_client_domain,
    clear_all_reports, get_client_domains,
)

MAX_BATCH = 10

create_tables()
st.set_page_config(page_title="DXW Funding Intelligence", page_icon="money", layout="wide")
st.title("DXW Funding Intelligence")
st.caption("Enter client domains -> review names -> get funding & investment data")

# ── Sidebar ──
with st.sidebar:
    st.header("Gemini API Key")
    gemini_key = st.text_input(
        "Your Google Gemini API key (optional)",
        type="password",
        help="Paste your own Gemini API key to use your own quota. Leave blank "
             "to use the built-in default key. Get one at "
             "https://aistudio.google.com/apikey",
    )
    st.session_state.gemini_key = (gemini_key or "").strip()
    st.caption("Using your API key." if st.session_state.gemini_key
               else "Using the built-in default key.")
    st.divider()

    st.header("Database")
    st.caption("Each analysis run auto-clears prior funding reports before saving.")
    if st.button("Clear DB now"):
        clear_all_reports()
        st.success("Funding reports cleared.")
    st.divider()
    st.caption(f"Tip: up to {MAX_BATCH} domains per run. " "You can paste 'stripe.com', 'https://www.stripe.com', or just 'Stripe'.")

st.divider()

# ── session state for two-stage flow ──
if "rows" not in st.session_state:
    st.session_state.rows = None       # list of {domain, name}
if "results" not in st.session_state:
    st.session_state.results = []      # list of {name, domain, data, sources, error}
if "location" not in st.session_state:
    st.session_state.location = ""
if "gemini_key" not in st.session_state:
    st.session_state.gemini_key = ""


def ok(val):
    return val and val not in ["", "-", "INSUFFICIENT DATA", "None"]


def prose(text):
    if not ok(text):
        return
    import re
    t = str(text)
    # Strip stray backticks so $ amounts don't render as green code boxes
    t = t.replace("```", "").replace("`", "")
    # Turn [http...] citations into clickable links
    t = re.sub(r'\[(https?://[^\]]+)\]', r'[\1](\1)', t)
    st.markdown(t)


# ═══════════════════════════════════════════════════════════════
# STAGE 1 — enter domains
# ═══════════════════════════════════════════════════════════════
st.subheader("1. Enter domains")
col_a, col_b = st.columns([3, 2])
with col_a:
    domains_text = st.text_area(
        "Client domains (one per line)",
        height=160,
        placeholder="stripe.com\nnotion.so\nhttps://www.databricks.com",
    )
with col_b:
    optional_names = st.text_area(
        "Optional: company names (one per line, matching order)",
        height=160,
        placeholder="Stripe\nNotion\nDatabricks",
        help="If you already know the company name, type it here on the same " "line number as its domain. Leave blank to auto-detect.",
    )

location = st.text_input(
    "Which region are these companies based in?",
    placeholder="e.g. US, India, UK, Europe",
    help="We collect funding data from this region: it biases news/funding " "searches and picks the right authoritative source (US -> SEC EDGAR).",
)

if st.button("Find company names", type="primary"):
    raw_domains = [d.strip() for d in domains_text.splitlines() if d.strip()]
    raw_names = [n.strip() for n in optional_names.splitlines()]

    if not raw_domains:
        st.warning("Please enter at least one domain.")
    else:
        if len(raw_domains) > MAX_BATCH:
            st.warning(f"Only the first {MAX_BATCH} domains will be processed.")
            raw_domains = raw_domains[:MAX_BATCH]

        rows = []
        prog = st.progress(0.0, text="Detecting company names...")
        for i, d in enumerate(raw_domains):
            given = raw_names[i] if i < len(raw_names) and raw_names[i] else ""
            if given:
                clean_url, _ = normalize_domain(d)
                name = given
            else:
                clean_url, name = derive_company_name(d)
            rows.append({"Domain": clean_url or d, "Company name": name})
            prog.progress((i + 1) / len(raw_domains), text=f"Detected {i+1} of {len(raw_domains)}")
        prog.empty()
        st.session_state.rows = rows
        st.session_state.location = (location or "").strip()
        st.session_state.results = []  # reset prior results

# ═══════════════════════════════════════════════════════════════
# STAGE 2 — review & edit names
# ═══════════════════════════════════════════════════════════════
if st.session_state.rows:
    st.divider()
    st.subheader("2. Review & edit names")
    st.caption("Fix any wrong names before analyzing. SEC/Wikipedia lookups use these names.")

    edited = st.data_editor(
        pd.DataFrame(st.session_state.rows),
        width="stretch",
        num_rows="dynamic",
        column_config={
            "Domain": st.column_config.TextColumn("Domain", width="medium"),
            "Company name": st.column_config.TextColumn("Company name", width="medium"),
        },
        key="review_editor",
    )

    if st.button("Analyze All", type="primary"):
        # Configure Gemini with the user's key (or GEMINI_API_KEY env var)
        _ok, _msg = configure_gemini(st.session_state.get("gemini_key", ""))
        if not _ok:
            st.error(_msg)
            st.stop()  # no key -> do not run
        clear_all_reports()  # auto-clear before new run
        records = edited.to_dict("records")
        region = st.session_state.get("location", "")
        results = []
        prog = st.progress(0.0, text="Starting...")
        n = len(records)
        for i, rec in enumerate(records):
            name = str(rec.get("Company name", "")).strip()
            domain = str(rec.get("Domain", "")).strip()
            if not name and not domain:
                continue
            prog.progress(i / max(n, 1), text=f"Analyzing {i+1} of {n}: {name or domain}")
            try:
                if domain:
                    save_client_domain(name or "Unknown", domain)
                info = {"company_name": name or domain, "industry": "", "employee_size": "",
                        "revenue": "", "city": "", "state": "", "country": ""}
                ai, sources = analyze_company(info, [domain] if domain else [], client_domain=domain, location=region)
                save_report(ai, None, info, client_domain=domain, region=region)
                f = ai.get("funding_and_investment", {})
                results.append({"name": name or domain, "domain": domain,
                                "data": f, "sources": sources,
                                "error": f.get("error", "")})
            except Exception as e:
                results.append({"name": name or domain, "domain": domain,
                                "data": {}, "sources": [], "error": str(e)})
        prog.progress(1.0, text="Done")
        prog.empty()
        st.session_state.results = results
        st.success(f"Analyzed {len(results)} compan{'y' if len(results)==1 else 'ies'}.")

# ═══════════════════════════════════════════════════════════════
# STAGE 3 — results
# ═══════════════════════════════════════════════════════════════
if st.session_state.results:
    st.divider()
    st.subheader("3. Funding results")

    # summary table
    table = []
    for r in st.session_state.results:
        f = r["data"]
        table.append({
            "Company": r["name"],
            "Total Funding": f.get("total_funding", "-") or "-",
            "Valuation": f.get("valuation", "-") or "-",
            "Investors": ", ".join(f.get("key_investors", []) or [])[:60] or "-",
            "Health": f.get("financial_health", "-") or "-",
            "Confidence": f.get("confidence_level", "-") or "-",
            "Status": "FAILED" if r.get("error") else "OK",
        })
    st.dataframe(pd.DataFrame(table), width="stretch")

    # per-company detail
    for r in st.session_state.results:
        f = r["data"]
        label = f"{r['name']}  —  {f.get('total_funding','?') or '?'} raised"
        if r.get("error"):
            label = f"{r['name']}  —  FAILED"
        with st.expander(label):
            if r.get("error"):
                st.error(f"Error: {r['error']}")

            # The funding story (rich narrative) at the very top
            summ = f.get("executive_summary", "")
            if ok(summ):
                st.markdown("####  The Funding Story")
                prose(summ)

            # Confidence badge
            conf = (f.get("confidence_level", "") or "").strip()
            cnotes = f.get("confidence_notes", "")
            if ok(conf):
                badge = {"High": "[HIGH confidence]", "Medium": "[MEDIUM confidence]", "Low": "[LOW confidence - verify before use]"}.get(conf, f"[{conf} confidence]")
                if conf.lower() == "high":
                    st.success(badge)
                elif conf.lower() == "medium":
                    st.info(badge)
                else:
                    st.warning(badge)
                if ok(cnotes):
                    st.caption(cnotes)
            st.divider()

            ct, pub, tick = f.get("company_type", ""), f.get("publicly_traded", ""), f.get("ticker", "")
            ent = f.get("entity_type", "")
            meta = []
            if ok(ent):
                meta.append(f"**Entity:** {ent}")
            if ok(ct):
                meta.append(f"**Type:** {ct}")
            if pub and "yes" in str(pub).lower() and tick:
                meta.append(f"**Publicly traded:** {tick}")
            elif ok(pub):
                meta.append(f"**Publicly traded:** {pub}")
            if meta:
                st.markdown(" | ".join(meta))

            m1, m2 = st.columns(2)
            with m1:
                st.metric("Total Funding", f.get("total_funding", "-") or "-")
            with m2:
                st.metric("Valuation", f.get("valuation", "-") or "-")

            rounds = f.get("funding_rounds", [])
            if rounds:
                st.markdown("**Funding Rounds**")
                for rd in rounds:
                    line = f"{rd.get('round','-')} - {rd.get('amount','-')} ({rd.get('date','-')})"
                    if ok(rd.get("lead_investor", "")):
                        line += f" - Lead: {rd['lead_investor']}"
                    st.write(line)
                    if ok(rd.get("source", "")):
                        st.caption(f"Source: {rd['source']}")

            inv = f.get("key_investors", [])
            if inv:
                st.markdown(f"**Key Investors:** {', '.join(inv)}")
            # Where the funding goes - sector/area allocation
            alloc = f.get("fund_allocation", [])
            alloc_rows = [a for a in alloc if isinstance(a, dict) and ok(a.get("area", ""))]
            if alloc_rows:
                st.markdown("**Where the Funding Goes (by sector / area)**")
                st.dataframe(
                    pd.DataFrame([{
                        "Area / Sector": a.get("area", "-"),
                        "Amount / %": a.get("amount", "-") or "-",
                        "Note": a.get("note", "-") or "-",
                    } for a in alloc_rows]),
                    width="stretch",
                    hide_index=True,
                )
            asum = f.get("fund_allocation_summary", "")
            if ok(asum):
                prose(asum)

            # Narrative fields
            for key, lbl in [("revenue_analysis", "Revenue Analysis"),
                             ("investment_focus", "Investment Focus"),
                             ("financial_health", "Financial Health")]:
                v = f.get(key, "")
                if ok(v):
                    st.markdown(f"**{lbl}:**")
                    prose(v)

            ms = f.get("recent_milestones", [])
            if ms:
                st.markdown("**Recent Milestones:**")
                for m in ms:
                    st.write(f"- {m}")

            dx = f.get("dxw_implication", "")
            if ok(dx):
                st.success("**DXW Implication:**")
                prose(dx)
            if r.get("sources"):
                with st.expander(f"Sources ({len(r['sources'])})"):
                    for s in r["sources"]:
                        st.markdown(f"- **[{s['type']}]** [{s['url']}]({s['url']})")
            with st.expander("Raw JSON"):
                st.json(f)
