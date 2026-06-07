# cmb-to-ezbookkeeping

Converts a **China Merchants Bank (招商银行) PDF transaction statement** into a CSV file that can be directly imported into [ezbookkeeping](https://github.com/mayswind/ezbookkeeping).

## Requirements

- Python 3.9+
- [pdfplumber](https://github.com/jsvine/pdfplumber)

```bash
pip install -r requirements.txt
```

## Usage

```bash
python3 pdf_to_ezbookkeeping.py <input.pdf> <output.csv> [options]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--rules FILE` | `categories.csv` (next to script) | Path to the category rules CSV file |
| `--account NAME` | *(blank)* | Your account name as it appears in ezbookkeeping (e.g. `"招商银行储蓄卡"`) |
| `--account2 NAME` | *(blank)* | Fallback destination account name for Transfer rows that have no counter-party info |
| `--categorize` | off | Use local Ollama LLM to categorize transactions not matched by built-in rules |
| `--ollama-url URL` | `http://localhost:11434` | Ollama API base URL |
| `--ollama-model MODEL` | `qwen2.5:14b` | Ollama model to use |
| `--timezone OFFSET` | `+08:00` | Timezone string written to every row |
| `-v`, `--verbose` | off | Print per-page progress to stderr |

### LLM-assisted categorization

The tool has ~300 built-in keyword rules covering most known merchants. For the remaining unmatched transactions (typically person-to-person transfers and unfamiliar merchants), pass `--categorize` to call a local [Ollama](https://ollama.com) instance as a fallback:

```bash
# 1. Install and start Ollama, then pull a model
brew install ollama
ollama pull qwen2.5:14b   # ~9 GB, fits in 16 GB VRAM

# 2. Run with LLM fallback
python3 pdf_to_ezbookkeeping.py statement.pdf out.csv \
    --account "招商银行储蓄卡" \
    --categorize \
    --verbose
```

The LLM is only called for transactions the built-in rules could not classify (typically <50 out of 1000+). Built-in rules always take priority over the LLM.

```bash
python3 pdf_to_ezbookkeeping.py transaction_records_20250425.pdf out.csv \
    --account "招商银行储蓄卡" \
    --verbose
```

## Customizing Categories

Category rules are stored in `categories.csv` alongside the script. Each row has four columns:

| Column | Values | Description |
|---|---|---|
| `type` | `income`, `expense`, `transfer` | Which transaction type this rule applies to |
| `keyword` | any string or regex | Matched as a substring against the Description field |
| `category` | e.g. `餐饮` | Top-level category |
| `sub_category` | e.g. `餐厅` | Sub-category |

Rules are tested in order; the first match wins. You can edit `categories.csv` directly to add merchants or adjust categories without touching the Python code. Use `--rules /path/to/my_rules.csv` to load a different file.

## Importing into ezbookkeeping

1. Open ezbookkeeping → **Transactions** → **Import**
2. Select file format: **ezbookkeeping CSV**
3. Upload the generated CSV file
4. Review the preview and confirm import

## Output Format

The tool produces an ezbookkeeping native CSV with 14 columns:

| Column | Source |
|---|---|
| Time | 记账日期 |
| Timezone | `--timezone` arg |
| Type | Derived from amount sign + 交易摘要 keyword |
| Category | Built-in rules; LLM fallback if `--categorize` |
| Sub Category | Built-in rules; LLM fallback if `--categorize` |
| Account | `--account` arg |
| Account Currency | 货币 (always CNY) |
| Amount | \|交易金额\| |
| Account2 | Counter-party name (Transfer rows only) |
| Account2 Currency | *(blank)* |
| Account2 Amount | *(blank)* |
| Geographic Location | *(blank)* |
| Tags | *(blank)* |
| Description | 对手信息 (full text for Expense/Income; account ID only for Transfer) |

### Transaction Type Classification

| Type | Condition |
|---|---|
| `Transfer` | 交易摘要 contains a transfer keyword (see below) |
| `Income` | Amount is positive (and no transfer keyword matched) |
| `Expense` | Amount is negative (and no transfer keyword matched) |

Transfer keywords: `转账汇款`, `信用卡还款`, `信用卡自动还款`, `还款`, `基金赎回`, `基金申购`, `定期存款`, `定期支取`, `活期转定期`, `定期转活期`, `跨行转账`, `行内转账`, `网银转账`, `零钱通`, `余额宝`, `理财`, `ATM取款`, `ATM提款`, `ATM存款`

### Counter-Party Splitting (Transfer rows)

The PDF's 对手信息 column contains two parts: a name and an account/reference number (e.g. `张三 6214850000000001`). For Transfer rows these are split:

- **Account2** ← `张三` (used as the destination account in ezbookkeeping)
- **Description** ← `6214850000000001` (kept as a reference)

For Expense/Income rows the full text is kept in Description unchanged.

## Notes

- The tool uses PDF word-position analysis (not raw text parsing) to correctly handle merchant names that wrap across multiple lines in the PDF.
- All transactions from a multi-page CMB statement are extracted in seconds.
- Output is UTF-8 with BOM (`utf-8-sig`) so it opens correctly in Excel on Windows.
