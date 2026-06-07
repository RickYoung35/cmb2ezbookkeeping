#!/usr/bin/env python3
"""
Convert a China Merchants Bank (招商银行) PDF statement to ezbookkeeping native CSV.

Usage:
    python3 pdf_to_ezbookkeeping.py input.pdf output.csv [options]

Options:
    --account NAME        Source account name in ezbookkeeping (default: "")
    --account2 NAME       Transfer destination account name (default: "")
    --timezone OFFSET     Timezone string (default: "+08:00")
    --categorize          Use local Ollama LLM to categorize unmatched transactions
    --ollama-url URL      Ollama base URL (default: http://localhost:11434)
    --ollama-model MODEL  Ollama model name (default: qwen2.5:32b)
    -v, --verbose         Print per-page progress to stderr
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

EZBOOKKEEPING_COLUMNS = [
    "Time", "Timezone", "Type", "Category", "Sub Category",
    "Account", "Account Currency", "Amount",
    "Account2", "Account2 Currency", "Account2 Amount",
    "Geographic Location", "Tags", "Description",
]

# ---------------------------------------------------------------------------
# Classification rules — loaded from categories.csv at startup
# ---------------------------------------------------------------------------

INCOME_RULES: list = []
EXPENSE_RULES: list = []
TRANSFER_RULES: list = []


def load_rules(path: str) -> tuple:
    """Load classification rules from a CSV file. Returns (income, expense, transfer) rule lists."""
    income, expense, transfer = [], [], []
    target = {"income": income, "expense": expense, "transfer": transfer}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lst = target.get(row["type"].strip().lower())
            if lst is not None:
                lst.append((row["keyword"], row["category"], row["sub_category"]))
    return income, expense, transfer


def classify(tx_type: str, description: str, account2: str) -> tuple:
    """Return (category, sub_category) for a transaction."""
    desc = description.strip()

    if tx_type == "Transfer":
        for keyword, cat, sub in TRANSFER_RULES:
            if keyword in account2:
                return cat, sub
        return "转账", "转账"

    rules = INCOME_RULES if tx_type == "Income" else EXPENSE_RULES
    for keyword, cat, sub in rules:
        if re.search(keyword, desc):
            return cat, sub

    return "", ""


# ---------------------------------------------------------------------------
# LLM-based categorization (Ollama fallback)
# ---------------------------------------------------------------------------

# All category/sub-category pairs the hard-coded rules use, plus ezbookkeeping
# defaults — presented to the LLM so it picks from a known set.
_CATEGORY_OPTIONS = """
Expense categories:
  餐饮 / 餐厅
  餐饮 / 外卖
  餐饮 / 快餐
  餐饮 / 烘焙/咖啡
  餐饮 / 饮料/甜品
  餐饮 / 便利店
  餐饮 / 小吃
  餐饮 / 超市/外卖
  餐饮 / 生鲜/水果
  购物 / 网购
  购物 / 超市
  购物 / 服装
  购物 / 美妆
  购物 / 母婴
  购物 / 数码
  购物 / 家电
  购物 / 家居
  购物 / 运动
  购物 / 玩具
  购物 / 烟酒
  购物 / 金饰
  购物 / 美发/美容
  购物 / 其他
  出行 / 网约车
  出行 / 公共交通
  出行 / 火车
  出行 / 机票/酒店
  出行 / 住宿
  出行 / 旅游
  出行 / 共享单车/租车
  居家 / 电费
  居家 / 物业费
  居家 / 停车
  居家 / 生活缴费
  通讯 / 手机话费
  医疗 / 医院
  医疗 / 药店
  医疗 / 健康管理
  生活 / 共享充电
  娱乐 / 视频/游戏
  娱乐 / 门票
  娱乐 / 网吧
  娱乐 / 社交
  娱乐 / 旅游/景点
  理财 / 理财产品
  理财 / 基金申购
  转账 / 微信转账
  转账 / 微信红包
  转账 / 家人转账
  转账 / 朋友转账
  转账 / 清算
  其他 / 其他

Income categories:
  工资 / 工资
  理财 / 基金赎回
  理财 / 利息
  出行 / 退款
  购物 / 退款
  餐饮 / 退款
  其他 / 税费退款
  其他 / 其他收入

Transfer categories:
  转账 / 家人转账
  转账 / 本人转账
  转账 / 朋友转账
  转账 / 转账
"""

_LLM_PROMPT_TEMPLATE = """\
You are a personal finance categorization assistant. Classify the following \
bank transaction into exactly one category from the list below.

Transaction:
  Type: {tx_type}
  Counter-party / Description: {description}
  Amount: {amount} CNY

Available categories (format: Category / Sub Category):
{categories}

Rules:
- Reply with ONLY a JSON object, no explanation.
- Use the exact category and sub-category strings from the list.
- If the description looks like a person's name with no other context, \
use 转账 / 朋友转账 for Expense or 其他 / 其他收入 for Income.
- If truly uncertain, use 其他 / 其他.

Reply format: {{"category": "...", "sub_category": "..."}}"""


def llm_categorize_batch(
    transactions: list,
    ollama_url: str,
    model: str,
    verbose: bool,
) -> dict:
    """
    Call Ollama for each transaction that needs categorization.
    Returns a dict mapping index → (category, sub_category).
    Only called for transactions where hard-coded rules returned ("", "").
    """
    results = {}
    api_url = ollama_url.rstrip("/") + "/api/generate"

    for i, tx in transactions:
        prompt = _LLM_PROMPT_TEMPLATE.format(
            tx_type=tx["Type"],
            description=(tx.get("account2_val") or tx.get("raw_desc", "")).strip(),
            amount=tx["Amount"],
            categories=_CATEGORY_OPTIONS.strip(),
        )

        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }).encode()

        try:
            req = urllib.request.Request(
                api_url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
            parsed = json.loads(body["response"])
            cat = parsed.get("category", "").strip()
            sub = parsed.get("sub_category", "").strip()
            if cat and sub:
                results[i] = (cat, sub)
                if verbose:
                    desc = (tx.get("account2_val") or tx.get("raw_desc", ""))
                    print(f"  LLM [{tx['Type']}] '{desc}' → {cat} / {sub}", file=sys.stderr)
            else:
                results[i] = ("其他", "其他")
        except Exception as e:
            if verbose:
                print(f"  LLM error for row {i}: {e}", file=sys.stderr)
            results[i] = ("其他", "其他")

    return results


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
    "转账汇款",
    "定期存款", "定期支取",
    "活期转定期", "定期转活期", "跨行转账", "行内转账",
    "网银转账", "零钱通", "余额宝",
    "ATM取款", "ATM提款", "ATM存款",
]

# Fund companies whose transactions are purchases (Expense) not transfers
FUND_COMPANY_KEYWORDS = [
    "基金管理", "基金公司", "清算专户",
]

# x-coordinate thresholds from PDF layout (units: PDF points)
# Col 0 记账日期: x~36   Col 1 货币: x~98   Col 2 交易金额: x~156
# Col 3 联机余额: x~234  Col 4 交易摘要: x~307  Col 5 对手信息: x~417
X_CURRENCY = 98
X_AMOUNT = 156
X_BALANCE = 234
X_SUMMARY = 307
X_COUNTERPARTY = 390  # threshold: x >= this → counter party column

DATE_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}$")


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

    Two multi-line layouts exist across different CMB statement generations:

    Layout A (newer): summary and cp appear on the date line's y; wrapped cp
      fragments appear on lines between two date rows (gap ~6pt to owner, ~21pt
      to the other). Continuation lines contain only cp-column words (x >= X_CP).

    Layout B (older): the date line carries only date/currency/amount/balance;
      summary and cp appear on the line immediately below (+6pt). A second
      continuation line (+21pt from date) carries a second cp fragment. Both
      continuation lines may contain words in both summary and cp columns.
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

    # -----------------------------------------------------------------------
    # Pass 1: classify each line as "tx" (starts with date) or "cont" (other)
    # -----------------------------------------------------------------------
    parsed_lines = []
    for y in sorted_ys:
        line_words = sorted(lines_by_y[y], key=lambda w: w["x0"])
        first = line_words[0]
        first_text = first["text"].strip()

        if DATE_RE.match(first_text) and first["x0"] < X_CURRENCY:
            # Transaction anchor line — collect tokens by column
            date_tok = currency_tok = amount_tok = ""
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
                    pass  # balance, not used
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
            # Candidate continuation line — skip known headers and page numbers
            combined = " ".join(w["text"] for w in line_words)
            if any(skip in combined for skip in ("记账日期", "Transaction", "Date ", "Currency", "Counter", "招商银行")):
                continue
            if re.match(r"^\d+/\d+$", combined.strip()):
                continue

            # Classify words into summary vs cp columns
            summary_parts = [w["text"] for w in line_words if w["x0"] < X_COUNTERPARTY and w["x0"] >= X_SUMMARY - 10]
            cp_parts = [w["text"] for w in line_words if w["x0"] >= X_COUNTERPARTY - 10]

            # Only keep as a continuation if it has at least some content in
            # the summary or cp column area (ignore stray left-margin words)
            if not summary_parts and not cp_parts:
                continue

            parsed_lines.append({
                "type": "cont",
                "y": y,
                "summary": " ".join(summary_parts),
                "cp": " ".join(cp_parts),
            })

    # -----------------------------------------------------------------------
    # Pass 2: single walk — attach each cont line to the right transaction.
    #
    # Layout B (older PDF): date line has no summary/cp; the cont line(s)
    #   immediately following (gap ≤ 8pt) carry that data inline → absorb.
    # Layout A (newer PDF): cont lines appear between two date rows; use
    #   nearest-neighbour gap rule (≤ 8 → append to prev, > 8 → prefix next).
    # -----------------------------------------------------------------------
    result_txs = []
    pending_cp = ""
    pending_summary = ""
    i = 0
    while i < len(parsed_lines):
        line = parsed_lines[i]
        if line["type"] == "tx":
            tx = dict(line)
            if pending_cp:
                tx["cp"] = (pending_cp + " " + tx["cp"]).strip()
                pending_cp = ""
            if pending_summary:
                tx["summary"] = (pending_summary + " " + tx["summary"]).strip()
                pending_summary = ""
            # Absorb immediately following cont lines within 8pt (Layout B)
            j = i + 1
            while j < len(parsed_lines) and parsed_lines[j]["type"] == "cont":
                if parsed_lines[j]["y"] - tx["y"] > 8:
                    break
                nxt = parsed_lines[j]
                if nxt["summary"]:
                    tx["summary"] = (tx["summary"] + " " + nxt["summary"]).strip()
                if nxt["cp"]:
                    tx["cp"] = (tx["cp"] + " " + nxt["cp"]).strip()
                j += 1
            result_txs.append(tx)
            i = j
        else:
            # Between-rows cont line (Layout A) — keep summary and cp separate
            if result_txs and abs(line["y"] - result_txs[-1]["y"]) <= 8:
                if line["summary"]:
                    result_txs[-1]["summary"] = (result_txs[-1]["summary"] + " " + line["summary"]).strip()
                if line["cp"]:
                    result_txs[-1]["cp"] = (result_txs[-1]["cp"] + " " + line["cp"]).strip()
            else:
                if line["summary"]:
                    pending_summary = (pending_summary + " " + line["summary"]).strip()
                if line["cp"]:
                    pending_cp = (pending_cp + " " + line["cp"]).strip()
            i += 1
    transactions = result_txs

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
              timezone: str, account: str, account2: str,
              use_llm: bool = False, ollama_url: str = "",
              ollama_model: str = "", verbose: bool = False) -> int:
    if output_path == "-":
        f = sys.stdout
        should_close = False
    else:
        f = open(output_path, "w", newline="", encoding="utf-8")
        should_close = True

    try:
        writer = csv.DictWriter(f, fieldnames=EZBOOKKEEPING_COLUMNS)
        writer.writeheader()

        # First pass: build all rows and collect unmatched indices for LLM
        built_rows = []
        llm_needed = []  # list of (index, tx_dict_with_context)

        for tx in transactions:
            tx_type = tx["Type"]
            raw_desc = tx["Description"]

            if tx_type == "Transfer":
                cp_name, cp_id = split_counterparty(raw_desc)
                account2_val = cp_name if cp_name else account2
                description_val = cp_id
                if any(kw in account2_val for kw in FUND_COMPANY_KEYWORDS):
                    tx_type = "Expense"
                    account2_val = ""
                    description_val = raw_desc
            else:
                account2_val = ""
                description_val = raw_desc

            category, sub_category = classify(tx_type, description_val, account2_val)

            row = {
                "Time": tx["Time"],
                "Timezone": timezone,
                "Type": tx_type,
                "Category": category,
                "Sub Category": sub_category,
                "Account": account,
                "Account Currency": tx["Currency"],
                "Amount": tx["Amount"],
                "Account2": account2_val,
                "Account2 Currency": "",
                "Account2 Amount": "",
                "Geographic Location": "",
                "Tags": "",
                "Description": description_val,
            }
            built_rows.append(row)

            if use_llm and not category:
                llm_needed.append((len(built_rows) - 1, {
                    **tx,
                    "Type": tx_type,
                    "account2_val": account2_val,
                    "raw_desc": raw_desc,
                }))

        # Second pass: fill LLM results for unmatched rows
        if llm_needed:
            if verbose:
                print(f"  Sending {len(llm_needed)} unmatched rows to LLM ({ollama_model})...",
                      file=sys.stderr)
            llm_results = llm_categorize_batch(llm_needed, ollama_url, ollama_model, verbose)
            for idx, (cat, sub) in llm_results.items():
                built_rows[idx]["Category"] = cat
                built_rows[idx]["Sub Category"] = sub

        for row in built_rows:
            writer.writerow(row)

    finally:
        if should_close:
            f.close()

    return len(built_rows)


def main():
    parser = argparse.ArgumentParser(
        description="Convert China Merchants Bank PDF statement to ezbookkeeping CSV"
    )
    parser.add_argument("input_pdf", help="Path to CMB PDF bank statement")
    parser.add_argument("output_csv", help="Output CSV path (use - for stdout)")
    parser.add_argument("--account", default="", help='Source account name (e.g. "招商银行储蓄卡")')
    parser.add_argument("--account2", default="", help="Transfer destination account name")
    parser.add_argument("--timezone", default="+08:00", help="Timezone offset (default: +08:00)")
    parser.add_argument("--rules", default="",
                        help="Path to category rules CSV (default: categories.csv next to this script)")
    parser.add_argument("--categorize", action="store_true",
                        help="Use local Ollama LLM to categorize unmatched transactions")
    parser.add_argument("--ollama-url", default="http://localhost:11434",
                        help="Ollama base URL (default: http://localhost:11434)")
    parser.add_argument("--ollama-model", default="qwen2.5:32b",
                        help="Ollama model name (default: qwen2.5:32b)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print per-page progress")
    args = parser.parse_args()

    rules_path = args.rules or os.path.join(os.path.dirname(os.path.abspath(__file__)), "categories.csv")
    if not os.path.exists(rules_path):
        print(f"ERROR: rules file not found: {rules_path}", file=sys.stderr)
        sys.exit(1)
    global INCOME_RULES, EXPENSE_RULES, TRANSFER_RULES
    INCOME_RULES, EXPENSE_RULES, TRANSFER_RULES = load_rules(rules_path)

    transactions = extract_transactions(args.input_pdf, verbose=args.verbose)

    if not transactions:
        print("ERROR: No transactions extracted. Check that the PDF is a CMB statement.", file=sys.stderr)
        sys.exit(1)

    count = write_csv(
        transactions, args.output_csv,
        timezone=args.timezone,
        account=args.account,
        account2=args.account2,
        use_llm=args.categorize,
        ollama_url=args.ollama_url,
        ollama_model=args.ollama_model,
        verbose=args.verbose,
    )
    print(f"Done: {count} transactions written to {args.output_csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
