import sqlite3
import json
from datetime import datetime, timezone

DB_NAME = "lead_intelligence.db"


def create_tables():
    """Create funding-focused tables if they do not already exist."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Funding reports table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS funding_reports (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at         TEXT,
            company_name       TEXT,
            industry           TEXT,
            location           TEXT,
            client_domain      TEXT,
            entity_type        TEXT,
            company_type       TEXT,
            publicly_traded    TEXT,
            ticker             TEXT,
            total_funding      TEXT,
            valuation          TEXT,
            key_investors      TEXT,
            fund_allocation    TEXT,
            fund_allocation_summary TEXT,
            revenue_analysis   TEXT,
            investment_focus   TEXT,
            financial_health   TEXT,
            dxw_implication    TEXT,
            funding_rounds     TEXT,
            recent_milestones  TEXT,
            full_funding_json  TEXT
        )
    """)

    # Migrate older DBs: add any missing columns
    cursor.execute("PRAGMA table_info(funding_reports)")
    _cols = [r[1] for r in cursor.fetchall()]
    for _c in ("entity_type", "fund_allocation", "fund_allocation_summary"):
        if _c not in _cols:
            cursor.execute(f"ALTER TABLE funding_reports ADD COLUMN {_c} TEXT")

    # Client-provided domains (reusable mapping company -> domain)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_domains (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT,
            domain       TEXT,
            added_at     TEXT,
            UNIQUE(company_name, domain)
        )
    """)

    conn.commit()
    conn.close()


# ── CLIENT DOMAIN STORAGE ────────────────────────────────────
def save_client_domain(company_name, domain):
    if not domain or not str(domain).strip():
        return
    domain = str(domain).strip()
    company_name = (company_name or "Unknown").strip()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO client_domains (company_name, domain, added_at) VALUES (?, ?, ?)",
            (company_name, domain, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print(f"✓ Client domain stored: {company_name} → {domain}")
    except Exception as e:
        print(f"DB ERROR (save_client_domain): {e}")
    finally:
        conn.close()


def get_client_domains(company_name):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT domain FROM client_domains WHERE company_name = ? ORDER BY added_at DESC",
        ((company_name or "").strip(),),
    )
    rows = [r[0] for r in cursor.fetchall()]
    conn.close()
    return rows


# ── DB CLEARING ──────────────────────────────────────────────
def clear_all_reports():
    """Wipe all funding reports. Client domains are preserved (reusable)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM funding_reports")
        conn.commit()
        print("✓ Cleared all funding reports")
    except Exception as e:
        print(f"DB ERROR (clear_all_reports): {e}")
    finally:
        conn.close()


def clear_reports_for_company(company_name):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM funding_reports WHERE company_name = ?", (company_name,))
        conn.commit()
        print(f"✓ Cleared prior funding reports for {company_name}")
    except Exception as e:
        print(f"DB ERROR (clear_reports_for_company): {e}")
    finally:
        conn.close()


# ── SAVE FUNDING REPORT ──────────────────────────────────────
def save_report(ai_result, final_score, company_info, contacts=None, client_domain="", region=""):
    """
    Save a funding report. `final_score` and `contacts` kept for signature
    compatibility but unused in funding-only mode.
    `region` is the user-provided location (e.g. 'US') stored in the location column.
    ai_result expected to contain key 'funding_and_investment'.
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        f = ai_result.get("funding_and_investment", {})

        company_name = company_info.get("company_name", "Unknown")
        industry = company_info.get("industry", "")
        # Prefer the user-provided region; fall back to any city/state/country
        location = (region or "").strip() or ", ".join(filter(None, [
            company_info.get("city", ""),
            company_info.get("state", ""),
            company_info.get("country", ""),
        ]))

        cursor.execute("""
            INSERT INTO funding_reports (
                created_at, company_name, industry, location, client_domain,
                entity_type, company_type, publicly_traded, ticker, total_funding, valuation,
                key_investors, fund_allocation, fund_allocation_summary,
                revenue_analysis, investment_focus, financial_health,
                dxw_implication, funding_rounds, recent_milestones, full_funding_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            company_name,
            industry,
            location,
            (client_domain or "").strip(),
            f.get("entity_type", ""),
            f.get("company_type", ""),
            f.get("publicly_traded", ""),
            f.get("ticker", ""),
            f.get("total_funding", ""),
            f.get("valuation", ""),
            json.dumps(f.get("key_investors", []), ensure_ascii=False),
            json.dumps(f.get("fund_allocation", []), ensure_ascii=False),
            f.get("fund_allocation_summary", ""),
            f.get("revenue_analysis", ""),
            f.get("investment_focus", ""),
            f.get("financial_health", ""),
            f.get("dxw_implication", ""),
            json.dumps(f.get("funding_rounds", []), ensure_ascii=False),
            json.dumps(f.get("recent_milestones", []), ensure_ascii=False),
            json.dumps(f, ensure_ascii=False),
        ))
        report_id = cursor.lastrowid
        conn.commit()
        print(f"✓ Funding report saved — ID: {report_id} | Company: {company_name}")
        return report_id
    except Exception as e:
        conn.rollback()
        print(f"DATABASE ERROR: {e}")
        return None
    finally:
        conn.close()


# ── READ HELPERS ─────────────────────────────────────────────


def get_all_reports():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, created_at, company_name, industry, location, client_domain,
               entity_type, company_type, publicly_traded, ticker, total_funding,
               valuation, fund_allocation_summary, revenue_analysis,
               financial_health, dxw_implication
        FROM funding_reports ORDER BY created_at DESC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_report_by_id(report_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM funding_reports WHERE id = ?", (report_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        report = dict(row)
        for k in ("key_investors", "fund_allocation", "funding_rounds",
                  "recent_milestones", "full_funding_json"):
            try:
                report[k] = json.loads(report[k])
            except Exception:
                pass
        return report
    return None
