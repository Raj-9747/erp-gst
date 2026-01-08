import streamlit as st
from io import BytesIO
import pandas as pd
import numpy as np
import re
from datetime import datetime
from typing import List, Optional, Tuple
import os
from difflib import SequenceMatcher
import xlsxwriter 
import openpyxl  

# ------------------ CONFIG ------------------
# # ERP_FILE     = r"/content/GSTR2_RECON_SEP_2025.xlsx"
# # NEW WAY
# uploaded_erp = st.file_uploader("Upload ERP File")
# if uploaded_erp:
#     erp_df = load_erp(uploaded_erp) # Your existing function works with this!
# PORTAL_FILE  = r"/content/sep.xlsx"
# OVERDUE_FILE_PATH= r"/content/GST_Recon_Overdue_Bills.xlsx" # Changed to a persistent path
# PORTAL_SHEET = "B2B"

# ------------------ STREAMLIT UI ------------------
st.set_page_config(page_title="GST Reconciliation", layout="wide")
st.title("📊 GST Reconciliation Tool")

with st.sidebar:
    st.header("Upload Files")
    uploaded_erp = st.file_uploader("1. Upload ERP Excel File", type=["xls","xlsx"])
    uploaded_portal = st.file_uploader("2. Upload Portal Excel File", type=["xls","xlsx"])
    uploaded_overdue = st.file_uploader("3. Upload Previous Overdue File (Optional)", type=["xls","xlsx"])
    
    PORTAL_SHEET = st.text_input("Portal Sheet Name", value="B2B")
    run_recon = st.button("🚀 Run Reconciliation")


ABS_TOL = 1.0   # ₹ absolute tolerance for "Exact"
PCT_TOL = 0.01  # 1% relative tolerance for "Almost"
FUZZY_THRESHOLD = 0.90  # invoice string similarity for typo-catching

# timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
# OUTPUT_FILE = f"GST_Recon_Output_{timestamp}.xlsx"

# ...existing code...
timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_FILE = f"GST_Recon_Output_{timestamp}.xlsx"
OVERDUE_FILE_PATH = f"GST_Recon_Overdue_Bills_{timestamp}.xlsx"
# ...existing code...
# Removed: OVERDUE_FILE = f"GST_Reon_Overdue_Bills_{timestamp}.xlsx"


# ------------------ HELPERS ------------------

def get_excel_engine(file):
    return "xlrd" if file.name.lower().endswith(".xls") else "openpyxl"

def _num(x) -> float:
    if pd.isna(x):
        return np.nan
    s = str(x).replace(",", "").strip()
    try:
        return float(s)
    except:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else np.nan

def normalize_invoice(x: str) -> str:
    if pd.isna(x):
        return ""
    x = str(x).upper()
    x = re.sub(r"\s+", "", x)
    x = re.sub(r"[^0-9A-Z]", "", x)
    return x

def normalize_gstin(x: str) -> str:
    if pd.isna(x):
        return ""
    s = str(x).upper().replace(" ", "")
    m = re.search(r"[0-9A-Z]{15}", s)
    return m.group(0) if m else s

def normalize_date(x):
    if pd.isna(x) or x == "":
        return None
    return pd.to_datetime(x, errors="coerce", dayfirst=True)

def find_header_row(df: pd.DataFrame, keywords: List[str]) -> Optional[int]:
    up_to = min(60, len(df))
    for i in range(up_to):
        row = df.iloc[i].astype(str).str.lower().tolist()
        if all(any(k in cell for cell in row) for k in keywords):
            return i
    return None

def _pick_invoice_col(cols: List[str]) -> str:
    for needle in ("invoice no", "invoice number", "inv no", "inv number"):
        for c in cols:
            if needle in c.lower():
                return c
    blockers = ("type", "date", "value", "rate", "tax")
    for c in cols:
        l = c.lower()
        if "invoice" in l and not any(b in l for b in blockers):
            return c
    raise ValueError("Invoice number column not found.")

def _pick_amount_col_erp(cols: List[str]) -> str:
    pref_order = [
        lambda c: "marg" in c and "taxable" in c,
        lambda c: "taxable value" in c,
        lambda c: "taxable" in c,
    ]
    for pred in pref_order:
        hits = [c for c in cols if pred(c.lower())]
        if hits:
            return hits[0]
    raise ValueError("ERP amount column not found.")

def _pick_amount_col_portal(cols: List[str]) -> str:
    pref = [c for c in cols if "taxable value" in c.lower()] \
        or [c for c in cols if "taxable" in c.lower()] \
        or [c for c in cols if "invoice value" in c.lower()] \
        or [c for c in cols if "total value" in c.lower()]
    if pref:
        return pref[0]
    raise ValueError("Portal amount column not found.")

def _pick_gstin_col(cols: List[str]) -> Optional[str]:
    for c in cols:
        if "gstin" in c.lower():
            return c
    return None

def _pick_date_col(cols: List[str]) -> Optional[str]:
    for c in cols:
        l = c.lower()
        if "invoice" in l and "date" in l:
            return c
    for c in cols:
        if "date" in c.lower():
            return c
    return None

def _first_numeric_col(df: pd.DataFrame) -> Optional[str]:
    num_cols = df.select_dtypes(include=["number"]).columns.tolist()
    return num_cols[0] if num_cols else None

def _rebuild_two_row_header(xls_path: str, sheet: str) -> pd.DataFrame:
    raw = pd.read_excel(xls_path, sheet_name=sheet, header=None)
    hdr = find_header_row(raw, ["invoice", "tax"]) or find_header_row(raw, ["invoice", "taxable"]) or 0
    header1 = raw.iloc[hdr].astype(str).tolist()
    header2 = raw.iloc[hdr+1].astype(str).tolist() if hdr + 1 < len(raw) else [""] * len(header1)
    combo = []
    for a, b in zip(header1, header2):
        a = str(a).strip(); b = str(b).strip()
        if a.lower() in (["", "nan"]):
            combo.append(b)
        elif b.lower() in (["", "nan"]):
            combo.append(a)
        else:
            combo.append(f"{a} {b}".strip())
    df = raw.iloc[hdr+2:].copy()
    df.columns = [str(c).strip() for c in combo]
    df = df.dropna(how="all")
    return df

def _pick_specific_col(cols: List[str], keywords: List[str]) -> Optional[str]:
    """Finds the first column whose lowercased name contains any of the provided keywords."""
    for keyword in keywords:
        for col in cols:
            if keyword.lower() in col.lower():
                return col
    return None

def load_erp(path: str) -> pd.DataFrame:
    engine = get_excel_engine(path)
    xls = pd.ExcelFile(path)
    first = xls.sheet_names[0]
    raw = pd.read_excel(
        path,
        sheet_name=first,
        header=None,
    )
    hdr = find_header_row(raw, ["invoice"]) or 0
    df  = pd.read_excel(path, sheet_name=first, header=hdr)
    df.columns = [str(c).strip() for c in df.columns]
    cols = df.columns.tolist()

    # Primary ERP columns
    inv_col = _pick_invoice_col(cols)
    try:
        amt_col = _pick_amount_col_erp(cols)
    except ValueError:
        fallback = _first_numeric_col(df)
        if not fallback:
            raise
        amt_col = fallback
    gstin_col = _pick_gstin_col(cols)
    date_col  = _pick_date_col(cols)

    # Additional ERP columns using _pick_specific_col
    marg_date_col     = _pick_specific_col(cols, ['marg date', 'm.date'])
    supplier_name_col = _pick_specific_col(cols, ['supplier name', 'party name'])
    marg_invoice_col  = _pick_specific_col(cols, ['marg invoice', 'm.invoice'])
    marg_gstin_col    = _pick_specific_col(cols, ['marge gstin', 'marg gstin'])
    marg_rate_col     = _pick_specific_col(cols, ['marg rate', 'm.rate'])
    total_tax_col     = _pick_specific_col(cols, ['total tax'])
    igst_col          = _pick_specific_col(cols, ['igst'])
    cgst_col          = _pick_specific_col(cols, ['cgst'])
    sgst_col          = _pick_specific_col(cols, ['sgst'])

    out = pd.DataFrame({
        "ERP_Invoice": df[inv_col],
        "ERP_Amount":  df[amt_col],
        "ERP_GSTIN":   df[gstin_col] if gstin_col else np.nan,
        "ERP_Date":    df[date_col] if date_col else np.nan,
        "ERP_Marg_Date": df[marg_date_col] if marg_date_col else np.nan,
        "ERP_Supplier_Name": df[supplier_name_col] if supplier_name_col else '',
        "ERP_Marg_Invoice": df[marg_invoice_col] if marg_invoice_col else np.nan,
        "ERP_Marg_GSTIN": df[marg_gstin_col] if marg_gstin_col else np.nan,
        "ERP_Marg_Rate": df[marg_rate_col] if marg_rate_col else np.nan,
        "ERP_Total_Tax": df[total_tax_col] if total_tax_col else np.nan,
        "ERP_IGST": df[igst_col] if igst_col else np.nan,
        "ERP_CGST": df[cgst_col] if cgst_col else np.nan,
        "ERP_SGST": df[sgst_col] if sgst_col else np.nan,
    })

    out = out[~out["ERP_Invoice"].isna()]
    out["ERP_Invoice_NORM"] = out["ERP_Invoice"].apply(normalize_invoice)
    out["ERP_Amount_NORM"]  = pd.to_numeric(out["ERP_Amount"].apply(_num), errors="coerce").round(2)
    out["ERP_GSTIN_NORM"]   = out["ERP_GSTIN"].apply(normalize_gstin)
    out["ERP_Date_NORM"]    = out["ERP_Date"].apply(normalize_date)

    # Normalization for new columns
    out["ERP_Marg_Date_NORM"] = out["ERP_Marg_Date"].apply(normalize_date)
    out["ERP_Marg_GSTIN_NORM"] = out["ERP_Marg_GSTIN"].apply(normalize_gstin)

    # Convert new numeric columns to numeric types
    out["ERP_Marg_Rate_NORM"] = pd.to_numeric(out["ERP_Marg_Rate"].apply(_num), errors="coerce").round(2)
    out["ERP_Total_Tax_NORM"] = pd.to_numeric(out["ERP_Total_Tax"].apply(_num), errors="coerce").round(2)
    out["ERP_IGST_NORM"] = pd.to_numeric(out["ERP_IGST"].apply(_num), errors="coerce").round(2)
    out["ERP_CGST_NORM"] = pd.to_numeric(out["ERP_CGST"].apply(_num), errors="coerce").round(2)
    out["ERP_SGST_NORM"] = pd.to_numeric(out["ERP_SGST"].apply(_num), errors="coerce").round(2)

    out = out.dropna(subset=["ERP_Amount_NORM"]).reset_index(drop=True)
    return out

def load_portal(path: str, sheet: str) -> Tuple[pd.DataFrame, pd.DataFrame, str]:
    engine = get_excel_engine(path)
    try:
        df_display = _rebuild_two_row_header(path, sheet)
    except Exception:
        xls = pd.ExcelFile(path)
        raw = pd.read_excel(xls, sheet_name=sheet, header=None)
        hdr = find_header_row(raw, ["invoice", "taxable"]) or 0
        df_display = pd.read_excel(path, sheet_name=sheet, header=hdr)

    df_display.columns = [str(c).strip() for c in df_display.columns]
    cols = df_display.columns.tolist()

    inv_col = _pick_invoice_col(cols)
    try:
        amt_col = _pick_amount_col_portal(cols)
    except ValueError:
        fallback = _first_numeric_col(df_display)
        if not fallback:
            raise
        amt_col = fallback

    gstin_col = _pick_gstin_col(cols)
    date_col  = _pick_date_col(cols)

    out = pd.DataFrame({
        "PORTAL_Invoice": df_display[inv_col],
        "PORTAL_Amount":  df_display[amt_col],
        "PORTAL_GSTIN":   df_display[gstin_col] if gstin_col else "",
        "PORTAL_Date":    df_display[date_col] if date_col else "",
    })
    out = out[~out["PORTAL_Invoice"].isna()]
    out["PORTAL_Invoice_NORM"] = out["PORTAL_Invoice"].apply(normalize_invoice)
    out["PORTAL_Amount_NORM"]  = pd.to_numeric(out["PORTAL_Amount"].apply(_num), errors="coerce").round(2)
    out["PORTAL_GSTIN_NORM"]   = out["PORTAL_GSTIN"].apply(normalize_gstin)
    out["PORTAL_Date_NORM"]    = out["PORTAL_Date"].apply(normalize_date)
    out = out.dropna(subset=["PORTAL_Amount_NORM"]).drop_duplicates(
        subset=["PORTAL_Invoice_NORM", "PORTAL_Amount_NORM"], keep="first"
    ).reset_index(drop=True)

    return out, df_display.copy(), inv_col

# def load_overdue(overdue_file_obj) -> pd.DataFrame:
#     expected_columns = [
#         "Source Invoice", "Source Amount", "Source GSTIN", "Source Date",
#         "Matched Invoice", "Matched Amount", "Amount Difference",
#         "Percentage Difference", "Recon Status", "Reason/Remark", "Record Type"
#     ]

#     if overdue_file_obj is None:
#         return pd.DataFrame(columns=expected_columns)

#     try:
#         df = pd.read_excel(overdue_file_obj, sheet_name="Overdue Bills")
#     except Exception:
#         return pd.DataFrame(columns=expected_columns)

#     if "Source Invoice (NORM)" not in df.columns:
#         df["Source Invoice (NORM)"] = df["Source Invoice"].apply(normalize_invoice)
#     if "Source Amount (NORM)" not in df.columns:
#         df["Source Amount (NORM)"] = pd.to_numeric(df["Source Amount"].apply(_num), errors="coerce").round(2)
#     if "Source GSTIN (NORM)" not in df.columns:
#         df["Source GSTIN (NORM)"] = df["Source GSTIN"].apply(normalize_gstin)

#     return df

# ...existing code...
def load_overdue(overdue_file_obj) -> pd.DataFrame:
    expected_columns = [
        "Source Invoice", "Source Amount", "Source GSTIN", "Source Date",
        "Matched Invoice", "Matched Amount", "Amount Difference",
        "Percentage Difference", "Recon Status", "Reason/Remark", "Record Type"
    ]

    # If no file provided, return an empty frame with expected + normalized columns
    if overdue_file_obj is None:
        df = pd.DataFrame(columns=expected_columns)
        df["Source Invoice (NORM)"] = pd.Series(dtype=str)
        df["Source Amount (NORM)"] = pd.Series(dtype='float64')
        df["Source GSTIN (NORM)"] = pd.Series(dtype=str)
        return df

    # Try to read the "Overdue Bills" sheet, fall back to first sheet
    try:
        df = pd.read_excel(overdue_file_obj, sheet_name="Overdue Bills")
    except Exception:
        try:
            df = pd.read_excel(overdue_file_obj, sheet_name=0)
        except Exception:
            # If we can't read anything, return empty structured frame
            df = pd.DataFrame(columns=expected_columns)
            df["Source Invoice (NORM)"] = pd.Series(dtype=str)
            df["Source Amount (NORM)"] = pd.Series(dtype='float64')
            df["Source GSTIN (NORM)"] = pd.Series(dtype=str)
            return df

    # Normalize column names to strings and strip whitespace
    df.columns = [str(c).strip() for c in df.columns]

    # Ensure base columns exist so normalization won't KeyError
    if "Source Invoice" not in df.columns and "Source Invoice (NORM)" not in df.columns:
        df["Source Invoice"] = pd.Series([""] * len(df), index=df.index, dtype=str)
    if "Source Amount" not in df.columns and "Source Amount (NORM)" not in df.columns:
        df["Source Amount"] = pd.Series([np.nan] * len(df), index=df.index, dtype='float64')
    if "Source GSTIN" not in df.columns and "Source GSTIN (NORM)" not in df.columns:
        df["Source GSTIN"] = pd.Series([""] * len(df), index=df.index, dtype=str)

    # Safely create normalized columns if they don't exist
    if "Source Invoice (NORM)" not in df.columns:
        df["Source Invoice (NORM)"] = df["Source Invoice"].apply(normalize_invoice)
    if "Source Amount (NORM)" not in df.columns:
        df["Source Amount (NORM)"] = pd.to_numeric(df["Source Amount"].apply(_num), errors="coerce").round(2)
    if "Source GSTIN (NORM)" not in df.columns:
        df["Source GSTIN (NORM)"] = df["Source GSTIN"].apply(normalize_gstin)

    return df
# ...existing code...

# ------------------ RECON WITH GSTIN+AMOUNT MATCHING ------------------
def reconcile(erp_df: pd.DataFrame, portal_df: pd.DataFrame, abs_tol: float = 1.0, pct_tol: float = 0.01, fuzzy_threshold: float = 0.90) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    # Build invoice map
    exact_map = {}
    for _, r in portal_df.iterrows():
        exact_map.setdefault(r["PORTAL_Invoice_NORM"], []).append({
            'amount': float(r["PORTAL_Amount_NORM"]),
            'gstin': r["PORTAL_GSTIN_NORM"],
            'index': r.name
        })
    portal_keys = list(exact_map.keys())

    # Build GSTIN+Amount map for secondary matching
    gstin_amount_map = {}
    for _, r in portal_df.iterrows():
        if r["PORTAL_GSTIN_NORM"]:
            key = (r["PORTAL_GSTIN_NORM"], float(r["PORTAL_Amount_NORM"]))
            gstin_amount_map.setdefault(key, []).append({
                'invoice': r["PORTAL_Invoice_NORM"],
                'amount': float(r["PORTAL_Amount_NORM"]),
                'index': r.name
            })

    statuses, reasons, matched_invoice, matched_amount, diffs, pct_diffs = [], [], [], [], [], []

    for _, r in erp_df.iterrows():
        inv, amt = r["ERP_Invoice_NORM"], float(r["ERP_Amount_NORM"])
        gstin = r["ERP_GSTIN_NORM"]

        status = "Not Found in Portal"
        reason = "No invoice with same normalized number in Portal"
        match_inv = ""
        match_amt = np.nan

        # STRATEGY 1: Invoice number match
        if inv in exact_map:
            p_records = exact_map[inv]
            diff_list = [abs(amt - p['amount']) for p in p_records]
            idx = int(np.argmin(diff_list))
            p_best = p_records[idx]
            match_inv, match_amt = inv, p_best['amount']
            diff = diff_list[idx]

            if diff <= ABS_TOL:
                status = "Exact Match"
                reason = f"Invoice match, amount within ₹{ABS_TOL:.2f}"
            else:
                pct = diff / max(abs(amt), 1.0)
                if pct <= PCT_TOL:
                    status = "Almost Matched"
                    reason = f"Invoice match, amount within {PCT_TOL*100:.2f}% (diff ₹{diff:.2f})"
                else:
                    status = "Mismatch on Amount"
                    reason = f"Invoice match but amount diff ₹{diff:.2f} ({pct*100:.2f}%)"

        # STRATEGY 2: GSTIN + Amount match (if invoice didn't match and GSTIN exists)
        elif gstin and gstin != "":
            # Look for exact GSTIN+Amount match
            for tolerance in [ABS_TOL, amt * PCT_TOL]:
                found = False
                for (p_gstin, p_amt), p_records in gstin_amount_map.items():
                    if p_gstin == gstin and abs(amt - p_amt) <= tolerance:
                        p_best = p_records[0]  # Take first match
                        match_inv, match_amt = p_best['invoice'], p_best['amount']
                        diff = abs(amt - match_amt)
                        pct = diff / max(abs(amt), 1.0)

                        status = "GSTIN+Amount Match"
                        reason = f"Invoice differs but GSTIN+Amount match (diff ₹{diff:.2f}, {pct*100:.2f}%)"
                        found = True
                        break
                if found:
                    break

            # If still not found, try fuzzy invoice match
            if not found:
                best_key, best_score = "", 0.0
                for k in portal_keys:
                    sc = SequenceMatcher(None, inv, k).ratio()
                    if sc > best_score:
                        best_key, best_score = k, sc
                if best_score >= FUZZY_THRESHOLD and best_key:
                    p_records = exact_map[best_key]
                    diff_list = [abs(amt - p['amount']) for p in p_records]
                    idx = int(np.argmin(diff_list))
                    p_best = p_records[idx]
                    match_inv, match_amt = best_key, p_best['amount']
                    diff = diff_list[idx]
                    pct = diff / max(abs(amt), 1.0)

                    if diff <= ABS_TOL:
                        status = "Almost Matched"
                        reason = f"Fuzzy invoice ({best_score:.2%}), amount within ₹{ABS_TOL:.2f}"
                    else:
                        status = "Mismatch on Amount"
                        reason = f"Fuzzy invoice ({best_score:.2%}), diff ₹{diff:.2f} ({pct*100:.2f}%)"

        # STRATEGY 3: Fuzzy invoice match only (if no GSTIN)
        else:
            best_key, best_score = "", 0.0
            for k in portal_keys:
                sc = SequenceMatcher(None, inv, k).ratio()
                if sc > best_score:
                    best_key, best_score = k, sc
            if best_score >= FUZZY_THRESHOLD and best_key:
                p_records = exact_map[best_key]
                diff_list = [abs(amt - p['amount']) for p in p_records]
                idx = int(np.argmin(diff_list))
                p_best = p_records[idx]
                match_inv, match_amt = best_key, p_best['amount']
                diff = diff_list[idx]
                pct = diff / max(abs(amt), 1.0)

                if diff <= ABS_TOL:
                    status = "Almost Matched"
                    reason = f"Fuzzy invoice ({best_score:.2%}), amount within ₹{ABS_TOL:.2f}"
                else:
                    status = "Mismatch on Amount"
                    reason = f"Fuzzy invoice ({best_score:.2%}), diff ₹{diff:.2f} ({pct*100:.2f}%)"

        statuses.append(status)
        reasons.append(reason)
        matched_invoice.append(match_inv)
        matched_amount.append(match_amt)
        diffs.append(round(amt - match_amt, 2) if not np.isnan(match_amt) else np.nan)
        pct_diffs.append(round(((amt - match_amt) / max(abs(amt), 1.0))*100, 4) if not np.isnan(match_amt) else np.nan)

    erp_out = erp_df.copy()
    erp_out["Portal_Matched_Invoice"] = matched_invoice
    erp_out["Portal_Matched_Amount"]  = matched_amount
    erp_out["Amount_Diff"]            = diffs
    erp_out["Percent_Diff"]           = pct_diffs
    erp_out["Recon_Status"]           = statuses
    erp_out["Reason"]                 = reasons

    # Build portal-side status
    used = erp_out[erp_out["Portal_Matched_Invoice"].notna() & (erp_out["Portal_Matched_Invoice"] != "")].copy()
    used["abs_diff"] = used["Amount_Diff"].abs()

    best_erp = (
        used.sort_values(["Portal_Matched_Invoice", "abs_diff"])
            .groupby("Portal_Matched_Invoice")
            .head(1)[["Portal_Matched_Invoice", "ERP_Amount_NORM", "Amount_Diff", "Percent_Diff", "Recon_Status"]]
            .rename(columns={
                "Portal_Matched_Invoice": "PORTAL_Invoice_NORM",
                "ERP_Amount_NORM": "Matched_ERP_Amount",
                "Amount_Diff": "Amount_Diff",
                "Percent_Diff": "Percent_Diff",
                "Recon_Status": "Recon_Status_from_ERP"
            })
    )

    dup_portal_mask = portal_df.duplicated("PORTAL_Invoice_NORM", keep=False)
    portal_key = portal_df[["PORTAL_Invoice","PORTAL_Amount","PORTAL_GSTIN","PORTAL_Date",
                            "PORTAL_Invoice_NORM","PORTAL_Amount_NORM"]].copy()
    portal_key["Portal_Duplicate_Flag"] = portal_key["PORTAL_Invoice_NORM"].isin(
        portal_df.loc[dup_portal_mask, "PORTAL_Invoice_NORM"].unique()
    )

    portal_ann = portal_key.merge(best_erp, on="PORTAL_Invoice_NORM", how="left")
    portal_ann["PORTAL_Recon_Status"] = np.where(
        portal_ann["Recon_Status_from_ERP"].notna(),
        "Matched in ERP",
        "Not Found in ERP"
    )
    portal_ann.loc[portal_ann["Portal_Duplicate_Flag"], "PORTAL_Recon_Status"] = "Duplicate (Portal)"

    only_erp = erp_out[erp_out["Recon_Status"] == "Not Found in Portal"].copy()
    only_portal = portal_ann[portal_ann["PORTAL_Recon_Status"] == "Not Found in ERP"].copy()

    # Fix for IndentationError: combining dup_counts on a single line
    dup_counts = portal_df.groupby("PORTAL_Invoice_NORM").size().reset_index(name="Duplicate_Count")
    dup_report = dup_counts[dup_counts["Duplicate_Count"] > 1].copy()

    return erp_out, portal_ann, only_erp, only_portal, dup_report

# def main():
    
def main(erp_file_obj, portal_file_obj, overdue_file_obj):
    erp_df = load_erp(erp_file_obj)
    # portal_df, portal_display, display_inv_col = load_portal(PORTAL_FILE, PORTAL_SHEET)
    portal_df, portal_display, display_inv_col = load_portal(portal_file_obj, PORTAL_SHEET)
    # overdue_df = load_overdue(OVERDUE_FILE_PATH) # Load previous overdue bills
    overdue_df = load_overdue(overdue_file_obj)


    # Perform initial reconciliation for current month's data
    recon_df, portal_ann, only_erp_current_month, only_portal_current_month, dup_report = reconcile(erp_df, portal_df)

    # Prepare combined ERP records for current month
    combined_erp_results = recon_df[[
        "ERP_Invoice", "ERP_Amount", "ERP_GSTIN", "ERP_Date",
        "ERP_Marg_Date", "ERP_Supplier_Name", "ERP_Marg_Invoice", "ERP_Marg_GSTIN",
        "ERP_Marg_Rate", "ERP_Total_Tax", "ERP_IGST", "ERP_CGST", "ERP_SGST",
        "Portal_Matched_Invoice", "Portal_Matched_Amount",
        "Amount_Diff", "Percent_Diff", "Recon_Status", "Reason"
    ]].copy()
    combined_erp_results.rename(columns={
        "ERP_Invoice": "Source Invoice", # Changed Invoice to Source Invoice
        "ERP_Amount": "Source Amount",   # Changed Amount to Source Amount
        "ERP_GSTIN": "Source GSTIN",     # Changed GSTIN to Source GSTIN
        "ERP_Date": "Source Date",       # Changed Date to Source Date
        "ERP_Marg_Date": "Marg_Date",
        "ERP_Supplier_Name": "Supplier_Name",
        "ERP_Marg_Invoice": "Marg_Invoice",
        "ERP_Marg_GSTIN": "Marg_GSTIN",
        "ERP_Marg_Rate": "Marg_Rate",
        "ERP_Total_Tax": "Total_Tax",
        "ERP_IGST": "IGST",
        "ERP_CGST": "CGST",
        "ERP_SGST": "SGST",
        "Portal_Matched_Invoice": "Matched Invoice",
        "Portal_Matched_Amount": "Matched Amount",
        "Amount_Diff": "Amount Difference",
        "Percent_Diff": "Percentage Difference",
        "Recon_Status": "Recon Status",
        "Reason": "Reason/Remark"
    }, inplace=True)
    combined_erp_results["Record Type"] = "ERP Record"

    # Calculate 'Total_Tax' where it's NaN
    # The ERP_IGST_NORM, ERP_CGST_NORM, ERP_SGST_NORM are already numeric or NaN from load_erp
    # Now, they are named as IGST, CGST, SGST in combined_erp_results
    nan_total_tax_mask = combined_erp_results['Total_Tax'].isna()

    # Sum IGST, CGST, SGST for rows where Total_Tax is NaN, treating individual NaNs as 0
    if 'IGST' in combined_erp_results.columns and 'CGST' in combined_erp_results.columns and 'SGST' in combined_erp_results.columns:
        # ------------------ GST STATE-BASED TAX VISIBILITY ------------------

        # def apply_gst_tax_logic(row):
        #     gstin = str(row.get("Source GSTIN", ""))

        #     # Gujarat → CGST + SGST only
        #     if gstin.startswith("24"):
        #         row["IGST"] = 0.0

        #     # Maharashtra (27), Telangana (36) → IGST only
        #     elif gstin.startswith(("27", "36")):
        #         row["CGST"] = 0.0
        #         row["SGST"] = 0.0

        #     return row

        # combined_erp_results = combined_erp_results.apply(apply_gst_tax_logic, axis=1)
        # calculated_tax = combined_erp_results.loc[nan_total_tax_mask, ['IGST', 'CGST', 'SGST']].fillna(0).sum(axis=1).round(2)
        # combined_erp_results.loc[nan_total_tax_mask, 'Total_Tax'] = calculated_tax

        # ------------------ GST STATE-BASED TAX VISIBILITY (SHOWING 0) ------------------

        def apply_gst_tax_logic(row):
            gstin = str(row.get("Source GSTIN", ""))

            # Gujarat (24) -> Show CGST/SGST, set IGST to 0
            if gstin.startswith("24"):
                row["IGST"] = 0.0

            # Maharashtra (27) or Telangana (36) -> Show IGST, set CGST/SGST to 0
            elif gstin.startswith(("27", "36")):
                row["CGST"] = 0.0
                row["SGST"] = 0.0

            return row

        # Apply the logic to set unwanted tax values to 0
        combined_erp_results = combined_erp_results.apply(apply_gst_tax_logic, axis=1)
        
        # Recalculate Total_Tax for missing values
        if all(col in combined_erp_results.columns for col in ['IGST', 'CGST', 'SGST']):
            nan_total_tax_mask = combined_erp_results['Total_Tax'].isna()
            
            # Sum the components (which are now 0.0 instead of empty)
            calculated_tax = combined_erp_results.loc[nan_total_tax_mask, ['IGST', 'CGST', 'SGST']].fillna(0).sum(axis=1).round(2)
            
            # Fill Total_Tax only where it was originally missing
            combined_erp_results.loc[nan_total_tax_mask, 'Total_Tax'] = calculated_tax


        # Conditional check: if all of IGST, CGST, SGST were NaN for an originally NaN Total_Tax row,
        # then set Total_Tax back to np.nan to accurately reflect missing tax information.
        all_tax_components_nan_mask = (
            combined_erp_results.loc[nan_total_tax_mask, 'IGST'].isna() &
            combined_erp_results.loc[nan_total_tax_mask, 'CGST'].isna() &
            combined_erp_results.loc[nan_total_tax_mask, 'SGST'].isna()
        )
        combined_erp_results.loc[nan_total_tax_mask, 'Total_Tax'] = np.where(
            all_tax_components_nan_mask, np.nan, combined_erp_results.loc[nan_total_tax_mask, 'Total_Tax']
        )

    # Add normalized columns for current month's ERP records
    combined_erp_results["Source Invoice (NORM)"] = combined_erp_results["Source Invoice"].apply(normalize_invoice) # Use Source Invoice
    combined_erp_results["Source Amount (NORM)"] = pd.to_numeric(combined_erp_results["Source Amount"].apply(_num), errors="coerce").round(2) # Use Source Amount
    combined_erp_results["Source GSTIN (NORM)"] = combined_erp_results["Source GSTIN"].apply(normalize_gstin) # Use Source GSTIN

    # Prepare Portal-only records for current month (This block is kept for generating 'only_portal_current_month' if needed later,
    # but these records will not be concatenated into `final_all_recon_df` as per instructions).
    combined_portal_only_results = only_portal_current_month[[
        "PORTAL_Invoice", "PORTAL_Amount", "PORTAL_GSTIN", "PORTAL_Date"
    ]].copy()
    combined_portal_only_results.rename(columns={
        "PORTAL_Invoice": "Source Invoice",
        "PORTAL_Amount": "Source Amount",
        "PORTAL_GSTIN": "Source GSTIN",
        "PORTAL_Date": "Source Date"
    }, inplace=True)
    combined_portal_only_results["Record Type"] = "Portal Only Record"
    combined_portal_only_results["Matched Invoice"] = np.nan
    combined_portal_only_results["Matched Amount"] = np.nan
    combined_portal_only_results["Amount Difference"] = np.nan
    combined_portal_only_results["Percentage Difference"] = np.nan
    combined_portal_only_results["Recon Status"] = "Not Found in ERP"
    combined_portal_only_results["Reason/Remark"] = "Record found only in Portal data"
    # Add normalized columns for current month's Portal-only records
    combined_portal_only_results["Source Invoice (NORM)"] = combined_portal_only_results["Source Invoice"].apply(normalize_invoice)
    combined_portal_only_results["Source Amount (NORM)"] = pd.to_numeric(combined_portal_only_results["Source Amount"].apply(_num), errors="coerce").round(2)
    combined_portal_only_results["Source GSTIN (NORM)"] = combined_portal_only_results["Source GSTIN"].apply(normalize_gstin)

    # Concatenate current month's ERP results and Portal-only results (now both have NORM columns)
    all_recon_current_month_df = pd.concat([combined_erp_results, combined_portal_only_results], ignore_index=True)

    # ------------------ SECONDARY RECONCILIATION WITH OVERDUE BILLS ------------------

    # Add original index for later vectorized updates
    all_recon_current_month_df['original_index'] = all_recon_current_month_df.index
    overdue_df['original_index'] = overdue_df.index

    # Consolidate all current month's unresolved items
    current_unresolved_all = all_recon_current_month_df[
        (all_recon_current_month_df["Recon Status"] == "Not Found in Portal") |\
        (all_recon_current_month_df["Recon Status"] == "Not Found in ERP")
    ].copy()
    current_unresolved_all.rename(columns={'original_index': 'original_index_curr'}, inplace=True)


    # Consolidate all previous overdue items that are still unresolved
    previous_overdue_all = overdue_df[
        (overdue_df["Recon Status"] == "Not Found in Portal") |\
        (overdue_df["Recon Status"] == "Not Found in ERP")
    ].copy()
    previous_overdue_all.rename(columns={'original_index': 'original_index_prev'}, inplace=True)


    # Add unique match key (Invoice + GSTIN) to facilitate merging
    current_unresolved_all['__match_key__'] = current_unresolved_all['Source Invoice (NORM)'] + "_" + current_unresolved_all['Source GSTIN (NORM)']
    previous_overdue_all['__match_key__'] = previous_overdue_all['Source Invoice (NORM)'] + "_" + previous_overdue_all['Source GSTIN (NORM)']

    # Perform single inner merge based on the unified match key
    merged_secondary_matches = pd.merge(
        current_unresolved_all,
        previous_overdue_all,
        on='__match_key__',
        how='inner',
        suffixes=('_curr', '_prev')
    )

    # Apply Amount Tolerance Filtering
    if not merged_secondary_matches.empty:
        # Calculate amount differences for tolerance check
        amount_diff = np.abs(merged_secondary_matches['Source Amount (NORM)_curr'] - merged_secondary_matches['Source Amount (NORM)_prev'])
        pct_diff = amount_diff / np.maximum(merged_secondary_matches['Source Amount (NORM)_curr'], 1.0) # Handle division by zero

        # Filter based on absolute or percentage tolerance
        filtered_secondary_matches = merged_secondary_matches[
            (amount_diff <= ABS_TOL) | (pct_diff <= PCT_TOL)
        ].copy()
    else:
        filtered_secondary_matches = pd.DataFrame() # No matches found, create empty DataFrame

    # Implement Vectorized Status Updates
    if not filtered_secondary_matches.empty:
        # --- Update all_recon_current_month_df (current month items resolved by overdue) ---
        curr_indices_to_update = filtered_secondary_matches['original_index_curr'].values

        # Explicitly ensure correct dtypes for robust assignment in all_recon_current_month_df
        for col_name in ['Recon Status', 'Reason/Remark', 'Matched Invoice']:
            if col_name in all_recon_current_month_df.columns and all_recon_current_month_df[col_name].dtype != 'object':
                all_recon_current_month_df[col_name] = all_recon_current_month_df[col_name].astype(object)
            elif col_name not in all_recon_current_month_df.columns:
                all_recon_current_month_df[col_name] = pd.Series(dtype=object, index=all_recon_current_month_df.index) # Add index to prevent SettingWithCopyWarning

        for col_name in ['Matched Amount', 'Amount Difference', 'Percentage Difference']:
            if col_name in all_recon_current_month_df.columns and all_recon_current_month_df[col_name].dtype != 'float64':
                all_recon_current_month_df[col_name] = pd.to_numeric(all_recon_current_month_df[col_name], errors='coerce')
            elif col_name not in all_recon_current_month_df.columns:
                all_recon_current_month_df[col_name] = pd.Series(dtype='float64', index=all_recon_current_month_df.index)

        all_recon_current_month_df.loc[curr_indices_to_update, 'Recon Status'] = 'Matched from Overdue File'
        all_recon_current_month_df.loc[curr_indices_to_update, 'Matched Invoice'] = filtered_secondary_matches['Source Invoice_prev'].values.astype(object) # Corrected column name
        all_recon_current_month_df.loc[curr_indices_to_update, 'Matched Amount'] = filtered_secondary_matches['Source Amount_prev'].values.astype(float) # Corrected column name
        all_recon_current_month_df.loc[curr_indices_to_update, 'Amount Difference'] = (filtered_secondary_matches['Source Amount (NORM)_curr'].values - filtered_secondary_matches['Source Amount (NORM)_prev'].values).astype(float)
        all_recon_current_month_df.loc[curr_indices_to_update, 'Percentage Difference'] = (
            (all_recon_current_month_df.loc[curr_indices_to_update, 'Amount Difference'] /
             np.maximum(filtered_secondary_matches['Source Amount (NORM)_curr'].values, 1.0)) * 100
        ).round(4).astype(float)
        all_recon_current_month_df.loc[curr_indices_to_update, 'Reason/Remark'] = (
            'Matched current ' +
            np.where(filtered_secondary_matches['Record Type_curr'] == 'ERP Record', 'ERP', 'Portal') +
            ' record with previous ' +
            np.where(filtered_secondary_matches['Record Type_prev'] == 'ERP Record', 'ERP', 'Portal') +
            ' record in overdue file'
        ).astype(object)


        # --- Update overdue_df (previously overdue items resolved by current data) ---
        prev_indices_to_update = filtered_secondary_matches['original_index_prev'].values

        # Explicitly ensure correct dtypes for robust assignment in overdue_df
        for col_name in ['Recon Status', 'Reason/Remark', 'Matched Invoice']:
            if col_name in overdue_df.columns and overdue_df[col_name].dtype != 'object':
                overdue_df[col_name] = overdue_df[col_name].astype(object)
            elif col_name not in overdue_df.columns:
                overdue_df[col_name] = pd.Series(dtype=object, index=overdue_df.index)

        for col_name in ['Matched Amount', 'Amount Difference', 'Percentage Difference']:
            if col_name in overdue_df.columns and overdue_df[col_name].dtype != 'float64':
                overdue_df[col_name] = pd.to_numeric(overdue_df[col_name], errors='coerce')
            elif col_name not in overdue_df.columns:
                overdue_df[col_name] = pd.Series(dtype='float64', index=overdue_df.index)

        overdue_df.loc[prev_indices_to_update, 'Recon Status'] = 'Matched by Current Data'
        overdue_df.loc[prev_indices_to_update, 'Matched Invoice'] = filtered_secondary_matches['Source Invoice_curr'].values.astype(object) # Corrected column name
        overdue_df.loc[prev_indices_to_update, 'Matched Amount'] = filtered_secondary_matches['Source Amount_curr'].values.astype(float) # Corrected column name
        overdue_df.loc[prev_indices_to_update, 'Amount Difference'] = (filtered_secondary_matches['Source Amount (NORM)_prev'].values - filtered_secondary_matches['Source Amount (NORM)_curr'].values).astype(float)
        overdue_df.loc[prev_indices_to_update, 'Percentage Difference'] = (
            (overdue_df.loc[prev_indices_to_update, 'Amount Difference'] /
             np.maximum(filtered_secondary_matches['Source Amount (NORM)_prev'].values, 1.0)) * 100
        ).round(4).astype(float)
        overdue_df.loc[prev_indices_to_update, 'Reason/Remark'] = (
            'Previous ' +
            np.where(filtered_secondary_matches['Record Type_prev'] == 'ERP Record', 'ERP', 'Portal') +
            ' record matched by current ' +
            np.where(filtered_secondary_matches['Record Type_curr'] == 'ERP Record', 'ERP', 'Portal') +
            ' data'
        ).astype(object)

    # Modified: final_all_recon_df will now only contain current month's ERP records
    final_all_recon_df = all_recon_current_month_df.copy()

    # Ensure no 'Portal Only Record' types are present in final_all_recon_df
    # (This step is redundant if combined_portal_only_results is not concatenated, but included for robustness)
    final_all_recon_df = final_all_recon_df[final_all_recon_df['Record Type'] != 'Portal Only Record'].copy()

    status_order = [
        "Exact Match",
        "GSTIN+Amount Match",
        "Almost Matched",
        "Matched from Overdue File", # New status for current month records resolved by overdue
        "Matched by Current Data",   # New status for previously overdue items resolved by current month
        "Mismatch on Amount",
        "Not Found in Portal"
    ] # Removed "Not Found in ERP" as final_all_recon_df will only contain ERP records
    final_all_recon_df["Recon Status"] = pd.Categorical(final_all_recon_df["Recon Status"], categories=status_order, ordered=True)
    final_all_recon_df.sort_values(by=["Recon Status", "Source Invoice"], inplace=True)

    # Prepare the OVERDUE_FILE_PATH for the next cycle
    # It contains current month records that remain unresolved after both stages
    # AND previous overdue records that are still unresolved.

    # Current month's ERP records that are still unresolved
    current_month_still_unresolved = final_all_recon_df[
        final_all_recon_df['Recon Status'].isin(['Mismatch on Amount', 'Not Found in Portal'])
    ].copy()

    # Append current month's 'Portal Only Record' to the overdue file
    # (These were previously excluded from final_all_recon_df, but should go to overdue if not matched)
    unresolved_portal_only = combined_portal_only_results[
        combined_portal_only_results['Recon Status'] == 'Not Found in ERP'
    ].copy()

    # Previous overdue records that are still unresolved
    previous_overdue_still_unresolved = overdue_df[
        overdue_df['Recon Status'].isin(['Mismatch on Amount', 'Not Found in Portal', 'Not Found in ERP']) &\
        (overdue_df['Recon Status'] != 'Matched by Current Data')
    ].copy()

    overdue_bills_for_next_month = pd.concat([
        current_month_still_unresolved,
        unresolved_portal_only,
        previous_overdue_still_unresolved
    ], ignore_index=True)

    # Deduplicate overdue_bills_for_next_month based on normalized invoice, amount, GSTIN, and record type
    # overdue_bills_for_next_month.drop_duplicates(
    #     subset=['Source Invoice (NORM)', 'Source Amount (NORM)', 'Source GSTIN (NORM)', 'Record Type'],
    #     inplace=True
    # )

    # # Define desired output columns for Reconciliation_Summary
    # desired_output_columns = [
    #     "Source Invoice", "Source Amount", "Source GSTIN", "Source Date", # Changed Invoice to Source Invoice, Amount to Source Amount, GSTIN to Source GSTIN, Date to Source Date
    #     "Marg_Date", "Supplier_Name", "Marg_Invoice", "Marg_GSTIN",
    #     "Marg_Rate", "Total_Tax", "IGST", "CGST", "SGST",
    #     "Matched Invoice", "Matched Amount", "Amount Difference",
    #     "Percentage Difference", "Recon Status", "Reason/Remark", "Record Type"
    # ]

    overdue_bills_for_next_month.drop_duplicates(
        subset=['Source Invoice (NORM)', 'Source Amount (NORM)', 'Source GSTIN (NORM)', 'Record Type'],
        inplace=True
    )

    # --- Compute summary counts BEFORE returning (fix UnboundLocalError) ---
    resolved_current_by_overdue_count = int(
        all_recon_current_month_df['Recon Status'].eq('Matched from Overdue File').sum()
    ) if 'Recon Status' in all_recon_current_month_df.columns else 0

    resolved_overdue_by_current_count = int(
        overdue_df['Recon Status'].eq('Matched by Current Data').sum()
    ) if 'Recon Status' in overdue_df.columns else 0

    # Define desired output columns for Reconciliation_Summary
    desired_output_columns = [
        "Source Invoice", "Source Amount", "Source GSTIN", "Source Date", # Changed Invoice to Source Invoice, Amount to Source Amount, GSTIN to Source GSTIN, Date to Source Date
        "Marg_Date", "Supplier_Name", "Marg_Invoice", "Marg_GSTIN",
        "Marg_Rate", "Total_Tax", "IGST", "CGST", "SGST",
        "Matched Invoice", "Matched Amount", "Amount Difference",
        "Percentage Difference", "Recon Status", "Reason/Remark", "Record Type"
    ]


    # Filter final_all_recon_df to only include desired columns
    # Check if all desired columns exist, if not, only include the ones that do
    existing_desired_columns = [col for col in final_all_recon_df.columns if col in desired_output_columns]
    final_all_recon_df = final_all_recon_df[existing_desired_columns].copy()

    # Drop normalized and temporary index columns before writing the final output files
    cols_to_drop_recon = [col for col in final_all_recon_df.columns if '(NORM)' in col or 'original_index' in col or '__match_key__' in col]
    final_all_recon_df.drop(columns=cols_to_drop_recon, errors='ignore', inplace=True)

    cols_to_drop_overdue = [col for col in overdue_bills_for_next_month.columns if '(NORM)' in col or 'original_index' in col or '__match_key__' in col]
    overdue_bills_for_next_month.drop(columns=cols_to_drop_overdue, errors='ignore', inplace=True)

    # Write main reconciliation and duplicates report
    # with pd.ExcelWriter(OUTPUT_FILE, engine="xlsxwriter") as w:
    #     final_all_recon_df.to_excel(w, index=False, sheet_name="Reconciliation_Summary")
    #     dup_report.to_excel(w, index=False, sheet_name="Duplicates_Report")
    from io import BytesIO

    # 1. Create virtual memory for the Main Report
    # main_report_buffer = BytesIO()
    # with pd.ExcelWriter(main_report_buffer, engine="xlsxwriter") as w:
    #     final_all_recon_df.to_excel(w, index=False, sheet_name="Reconciliation_Summary")
    #     dup_report.to_excel(w, index=False, sheet_name="Duplicates_Report")

    # # 2. Create virtual memory for the New Overdue Bills
    # overdue_output_buffer = BytesIO()
    # with pd.ExcelWriter(overdue_output_buffer, engine="xlsxwriter") as w:
    #     overdue_bills_for_next_month.to_excel(w, index=False, sheet_name="Overdue Bills")

    # # 3. Create the UI Buttons so the user can click and save
    # st.success("✅ Analysis Complete!")
    
    # col1, col2 = st.columns(2) # Put buttons side-by-side
    # with col1:
    #     st.download_button(
    #         label="📥 Download Main Reconciliation",
    #         data=main_report_buffer.getvalue(),
    #         file_name=f"GST_Recon_{datetime.now().strftime('%Y%m%d')}.xlsx",
    #         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    #     )
    # with col2:
    #     st.download_button(
    #         label="📥 Download New Overdue List",
    #         data=overdue_output_buffer.getvalue(),
    #         file_name="Overdue_Bills_Update.xlsx",
    #         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    #     )

    # ...existing code...
    # 1. Create virtual memory for the Main Report
    main_report_buffer = BytesIO()
    with pd.ExcelWriter(main_report_buffer, engine="xlsxwriter") as w:
        final_all_recon_df.to_excel(w, index=False, sheet_name="Reconciliation_Summary")
        dup_report.to_excel(w, index=False, sheet_name="Duplicates_Report")

    # 2. Create virtual memory for the New Overdue Bills
    overdue_output_buffer = BytesIO()
    with pd.ExcelWriter(overdue_output_buffer, engine="xlsxwriter") as w:
        overdue_bills_for_next_month.to_excel(w, index=False, sheet_name="Overdue Bills")

    # Instead of showing download buttons here (which disappear after Streamlit rerun),
    # return the generated bytes and filenames so the caller can store them in session_state.
    return {
        "main_bytes": main_report_buffer.getvalue(),
        "overdue_bytes": overdue_output_buffer.getvalue(),
        "main_filename": f"GST_Recon_{datetime.now().strftime('%Y%m%d')}.xlsx",
        "overdue_filename": "Overdue_Bills_Update.xlsx",
        # minimal metadata for UI summary (optional)
        "counts": {
            "total_erp": len(erp_df),
            "exact_matches": len(recon_df[recon_df['Recon_Status'] == 'Exact Match']),
            "gstin_amount_matches": len(recon_df[recon_df['Recon_Status'] == 'GSTIN+Amount Match']),
            "almost_matches": len(recon_df[recon_df['Recon_Status'] == 'Almost Matched']),
            "mismatches": len(recon_df[recon_df['Recon_Status'] == 'Mismatch on Amount']),
            "not_found_in_portal": len(only_erp_current_month),
            "not_found_in_erp": len(only_portal_current_month),
            "resolved_current_by_overdue": resolved_current_by_overdue_count,
            "resolved_overdue_by_current": resolved_overdue_by_current_count,
            "new_overdue_total": len(overdue_bills_for_next_month)
        }
    }
# ...existing code...

    # Write overdue bills to a separate Excel file
    # with pd.ExcelWriter(OVERDUE_FILE_PATH, engine="xlsxwriter") as w:
    #     overdue_bills_for_next_month.to_excel(w, index=False, sheet_name="Overdue Bills")

    print("✅ Reconciliation complete with GSTIN+Amount matching enabled.")
    print(f"📁 Main reconciliation file created → {OUTPUT_FILE}")
    print(f"📁 Overdue bills file created → {OVERDUE_FILE_PATH}")
    print(f"\nℹ️ Summary:")
    print(f"  - Total ERP records: {len(erp_df)}")
    print(f"  - Exact matches (current month): {len(recon_df[recon_df['Recon_Status'] == 'Exact Match'])}")
    print(f"  - GSTIN+Amount matches (current month): {len(recon_df[recon_df['Recon_Status'] == 'GSTIN+Amount Match'])}")
    print(f"  - Almost matched (current month): {len(recon_df[recon_df['Recon_Status'] == 'Almost Matched'])}")
    print(f"  - Mismatches (current month, initial): {len(recon_df[recon_df['Recon_Status'] == 'Mismatch on Amount'])}")
    print(f"  - Not found in Portal (current month, initial): {len(only_erp_current_month)}")
    print(f"  - Not found in ERP (current month, initial): {len(only_portal_current_month)}")

    # Calculate counts for new summary stats
    resolved_current_by_overdue_count = len(all_recon_current_month_df[
        all_recon_current_month_df['Recon Status'] == 'Matched from Overdue File'
    ])
    resolved_overdue_by_current_count = len(overdue_df[
        overdue_df['Recon Status'] == 'Matched by Current Data'
    ])

    print(f"  - Current month items resolved from Overdue File: {resolved_current_by_overdue_count}")
    print(f"  - Previously Overdue items resolved by Current Data: {resolved_overdue_by_current_count}")
    print(f"  - Total entries in new Overdue File: {len(overdue_bills_for_next_month)}")


# if __name__ == "__main__":
#     main()

# # This code stays outside of any function, at the bottom of the script
# if run_recon: # This refers to the st.button we created in the sidebar
#     if uploaded_erp and uploaded_portal:
#         # We call the main function and pass the 'baskets' of data
#         main(uploaded_erp, uploaded_portal, uploaded_overdue)
#     else:
#         st.error("⚠️ Please upload both the ERP and Portal files to continue.")

# ...existing code...
# This code stays outside of any function, at the bottom of the script
if run_recon: # This refers to the st.button we created in the sidebar
    if uploaded_erp and uploaded_portal:
        # Run reconciliation and store results in session_state so buttons survive reruns
        result = main(uploaded_erp, uploaded_portal, uploaded_overdue)
        st.session_state['last_recon_result'] = result
        st.success("✅ Analysis Complete!")
    else:
        st.error("⚠️ Please upload both the ERP and Portal files to continue.")

# If we previously ran reconciliation, show persistent download buttons using session_state
if 'last_recon_result' in st.session_state:
    res = st.session_state['last_recon_result']
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="📥 Download Main Reconciliation",
            data=res["main_bytes"],
            file_name=res["main_filename"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_main_recon"
        )
    with col2:
        st.download_button(
            label="📥 Download New Overdue List",
            data=res["overdue_bytes"],
            file_name=res["overdue_filename"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_overdue"
        )

    # Optional: show summary counts (keeps UI responsive after rerun)
    counts = res.get("counts", {})
    st.write(f"📁 Main reconciliation file ready → {res['main_filename']}")
    st.write(f"📁 Overdue bills file ready → {res['overdue_filename']}")
    st.write(f"ℹ️ Summary: Total ERP records: {counts.get('total_erp', '-')}, Exact: {counts.get('exact_matches','-')}, New Overdue: {counts.get('new_overdue_total','-')}")
# ...existing code...
