#!/usr/bin/env python3
"""
Convert a China Merchants Bank (招商银行) PDF statement to ezbookkeeping native CSV.

Usage:
    python3 pdf_to_ezbookkeeping.py input.pdf output.csv [options]

Options:
    --account NAME      Source account name in ezbookkeeping (default: "")
    --account2 NAME     Transfer destination account name (default: "")
    --timezone OFFSET   Timezone string (default: "+08:00")
    -v, --verbose       Print per-page progress to stderr
"""

import argparse
import csv
import re
import sys
from datetime import datetime
from typing import Optional

EZBOOKKEEPING_COLUMNS = [
    "Time", "Timezone", "Type", "Category", "Sub Category",
    "Account", "Account Currency", "Amount",
    "Account2", "Account2 Currency", "Account2 Amount",
    "Geographic Location", "Tags", "Description",
]

# Last token of a description is a counter-party account/merchant ID if it is
# purely alphanumeric and contains at least one digit (e.g. '6214850287898511',
# '8151000WYDGJY2K'). Pure-text tokens like '店' or '（新)' are not IDs.
_ID_TOKEN_RE = re.compile(r'^[A-Za-z0-9]+$')


def split_counterparty(desc: str) -> tuple:
    """Return (name, account_id). account_id is '' when no ID token is found."""
    if not desc:
        return desc, ""
    parts = desc.rsplit(None, 1)
    if len(parts) == 1:
        return desc, ""
    name, last = parts
    if _ID_TOKEN_RE.match(last) and any(c.isdigit() for c in last):
        return name.strip(), last
    return desc, ""


TRANSFER_KEYWORDS = [
    "转账汇款", "信用卡还款", "信用卡自动还款", "还款",
    "基金赎回", "基金申购", "定期存款", "定期支取",
    "活期转定期", "定期转活期", "跨行转账", "行内转账",
    "网银转账", "零钱通", "余额宝", "理财",
    "ATM取款", "ATM提款", "ATM存款",
]

# x-coordinate thresholds from PDF layout (units: PDF points)
# Col 0 记账日期: x~36   Col 1 货币: x~98   Col 2 交易金额: x~156
# Col 3 联机余额: x~234  Col 4 交易摘要: x~307  Col 5 对手信息: x~417
X_DATE = 36
X_CURRENCY = 98
X_AMOUNT = 156
X_BALANCE = 234
X_SUMMARY = 307
X_COUNTERPARTY = 390  # threshold: x >= this → counter party column

DATE_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}$")

SKIP_TEXT = {
    "记账日期", "货币", "交易金额", "联机余额", "交易摘要", "对手信息",
    "Transaction", "Date", "Currency", "Balance", "Amount", "Counter", "Party",
    "Type", "招商银行交易流水", "Statement", "of", "China", "Merchants", "Bank",
    "户", "名：杨新雨", "账号：6214850286924045", "Name", "Account", "No",
    "账户类型：ALL/全币种", "开", "户", "行：成都玉双路支行", "Sub", "Branch",
    "申请时间：2025-04-25", "19:34:03", "验", "证", "码：7A72HF3M",
    "Verification", "Code",
}


def parse_amount(raw: str) -> float:
    cleaned = raw.replace(",", "").replace(" ", "").strip()
    if not cleaned:
        raise ValueError("empty amount string")
    return float(cleaned)


def parse_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            continue
    raise ValueError(f"cannot parse date: {raw!r}")


def classify_type(amount: float, summary: str) -> str:
    for kw in TRANSFER_KEYWORDS:
        if kw in summary:
            return "Transfer"
    if amount > 0:
        return "Income"
    return "Expense"


def extract_page_transactions(page, page_num: int, verbose: bool) -> list:
    """
    Use word-level x/y positions to correctly reconstruct each transaction row.

    PDF column layout (x coordinates):
      ~36   記账日期 (date)
      ~98   货币 (currency)
      ~156  交易金额 (amount)
      ~234  联机余额 (balance, skipped)
      ~307  交易摘要 (summary/type)
      ~417  对手信息 (counter party / description)

    Multi-line rows: when the counter party name is long it wraps to the next
    visual line, which appears in the PDF *between* two date-anchored rows.
    The continuation line has x >= X_COUNTERPARTY and no date in col 0.
    It belongs to the transaction whose date line immediately precedes it.
    """
    words = page.extract_words(x_tolerance=3, y_tolerance=3)
    if not words:
        return []

    # Group words into visual lines by y-position (round to nearest point)
    lines_by_y: dict = {}
    for w in words:
        y = round(w["top"])
        lines_by_y.setdefault(y, []).append(w)

    # Sort lines top-to-bottom; within each line sort left-to-right
    sorted_ys = sorted(lines_by_y.keys())

    # Classify each line
    # A "data line" starts with a date token at x ~ X_DATE
    # A "continuation line" has all tokens at x >= X_COUNTERPARTY

    parsed_lines = []
    for y in sorted_ys:
        line_words = sorted(lines_by_y[y], key=lambda w: w["x0"])
        first = line_words[0]
        first_text = first["text"].strip()

        if DATE_RE.match(first_text) and first["x0"] < X_CURRENCY:
            # This is a transaction anchor line
            # Collect tokens by column
            date_tok = currency_tok = amount_tok = balance_tok = ""
            summary_parts = []
            cp_parts = []
            for w in line_words:
                x = w["x0"]
                t = w["text"]
                if x < X_CURRENCY:
                    date_tok = t
                elif x < X_AMOUNT:
                    currency_tok = t
                elif x < X_BALANCE:
                    amount_tok = t
                elif x < X_SUMMARY:
                    balance_tok = t  # not used
                elif x < X_COUNTERPARTY:
                    summary_parts.append(t)
                else:
                    cp_parts.append(t)
            parsed_lines.append({
                "type": "tx",
                "y": y,
                "date": date_tok,
                "currency": currency_tok or "CNY",
                "amount": amount_tok,
                "summary": " ".join(summary_parts),
                "cp": " ".join(cp_parts),
            })
        else:
            # Check if this is a counter-party continuation line:
            # all words at x >= X_COUNTERPARTY (or close to it)
            if all(w["x0"] >= X_COUNTERPARTY - 10 for w in line_words):
                # Skip known header/label lines
                combined = " ".join(w["text"] for w in line_words)
                if any(skip in combined for skip in ("记账日期", "Transaction", "Date ", "Currency", "Counter", "招商银行")):
                    continue
                # Skip page number lines like "1/43"
                if re.match(r"^\d+/\d+$", combined.strip()):
                    continue
                parsed_lines.append({"type": "cont", "y": y, "cp": combined})

    # Merge each continuation line into the nearest transaction by y-distance.
    # In CMB PDFs a wrapped counter-party fragment is always 6 pts from its
    # owner transaction and 20+ pts from any other, so nearest-neighbour is exact.
    transactions = []
    for line in parsed_lines:
        if line["type"] != "cont":
            transactions.append(dict(line))
            continue

        y_cont = line["y"]
        cp_text = line["cp"]

        if not transactions:
            # No previous tx yet — will be picked up by the look-ahead below
            # Store as a pending prefix: attach to first transaction added later.
            # We achieve this by just prepending to parsed_lines in-place isn't
            # safe here; instead use a pending list.
            line["_pending"] = True
            transactions.append(line)
            continue

        prev_tx = transactions[-1]
        prev_y = prev_tx.get("y", y_cont - 999)
        dist_prev = abs(y_cont - prev_y)

        # Peek at the next tx in parsed_lines to compute dist_next
        # We don't have easy random access here, so we use the stored y values.
        # The logic: if dist_prev <= 8, attach to prev; otherwise defer to next.
        if dist_prev <= 8:
            existing = prev_tx["cp"]
            prev_tx["cp"] = (existing + " " + cp_text).strip() if existing else cp_text
        else:
            # Attach as prefix to the next transaction (mark as pending)
            line["_pending"] = True
            transactions.append(line)

    # Resolve pending prefixes: merge each pending continuation into the
    # tx that immediately follows it.
    resolved = []
    pending_cp = ""
    for item in transactions:
        if item.get("_pending"):
            pending_cp = (pending_cp + " " + item["cp"]).strip()
        else:
            if pending_cp:
                item["cp"] = (pending_cp + " " + item["cp"]).strip()
                pending_cp = ""
            resolved.append(item)
    transactions = resolved

    # Convert to output dicts
    results = []
    for tx in transactions:
        try:
            date_str = parse_date(tx["date"])
            amount = parse_amount(tx["amount"])
            summary = tx["summary"]
            description = tx["cp"]

            if amount == 0:
                print(f"  WARNING p{page_num+1}: zero-amount row, defaulting to Expense | {tx}", file=sys.stderr)

            tx_type = classify_type(amount, summary)
            results.append({
                "Time": date_str,
                "Type": tx_type,
                "Currency": tx["currency"],
                "Amount": f"{abs(amount):.2f}",
                "Description": description,
            })
        except (ValueError, KeyError) as e:
            print(f"  WARNING p{page_num+1}: skipping row ({e}) | {tx}", file=sys.stderr)

    return results


def extract_transactions(pdf_path: str, verbose: bool = False) -> list:
    try:
        import pdfplumber
    except ImportError:
        print("ERROR: pdfplumber not installed. Run: pip install pdfplumber", file=sys.stderr)
        sys.exit(1)

    transactions = []

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        if verbose:
            print(f"PDF has {total_pages} pages", file=sys.stderr)

        for page_num, page in enumerate(pdf.pages):
            if verbose:
                print(f"  Processing page {page_num+1}/{total_pages}...", file=sys.stderr)

            page_rows = extract_page_transactions(page, page_num, verbose)
            transactions.extend(page_rows)

            if verbose:
                print(f"    -> {len(page_rows)} transactions", file=sys.stderr)

    return transactions


def write_csv(transactions: list, output_path: str,
              timezone: str, account: str, account2: str) -> int:
    if output_path == "-":
        f = sys.stdout
        should_close = False
    else:
        f = open(output_path, "w", newline="", encoding="utf-8-sig")
        should_close = True

    try:
        writer = csv.DictWriter(f, fieldnames=EZBOOKKEEPING_COLUMNS)
        writer.writeheader()
        for tx in transactions:
            tx_type = tx["Type"]
            raw_desc = tx["Description"]

            if tx_type == "Transfer":
                # Split "王珮雯 6214850287898511" → Account2="王珮雯", Description="6214850287898511"
                cp_name, cp_id = split_counterparty(raw_desc)
                account2_val = cp_name if cp_name else account2
                description_val = cp_id
            else:
                account2_val = ""
                description_val = raw_desc

            writer.writerow({
                "Time": tx["Time"],
                "Timezone": timezone,
                "Type": tx_type,
                "Category": "",
                "Sub Category": "",
                "Account": account,
                "Account Currency": tx["Currency"],
                "Amount": tx["Amount"],
                "Account2": account2_val,
                "Account2 Currency": "",
                "Account2 Amount": "",
                "Geographic Location": "",
                "Tags": "",
                "Description": description_val,
            })
    finally:
        if should_close:
            f.close()

    return len(transactions)


def main():
    parser = argparse.ArgumentParser(
        description="Convert China Merchants Bank PDF statement to ezbookkeeping CSV"
    )
    parser.add_argument("input_pdf", help="Path to CMB PDF bank statement")
    parser.add_argument("output_csv", help="Output CSV path (use - for stdout)")
    parser.add_argument("--account", default="", help='Source account name (e.g. "招商银行储蓄卡")')
    parser.add_argument("--account2", default="", help="Transfer destination account name")
    parser.add_argument("--timezone", default="+08:00", help="Timezone offset (default: +08:00)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print per-page progress")
    args = parser.parse_args()

    transactions = extract_transactions(args.input_pdf, verbose=args.verbose)

    if not transactions:
        print("ERROR: No transactions extracted. Check that the PDF is a CMB statement.", file=sys.stderr)
        sys.exit(1)

    count = write_csv(transactions, args.output_csv, args.timezone, args.account, args.account2)
    print(f"Done: {count} transactions written to {args.output_csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
