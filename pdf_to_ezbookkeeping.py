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

# ---------------------------------------------------------------------------
# Classification rules
#
# Each rule is (keyword, category, sub_category).
# Rules are tested against the Description field in order; first match wins.
# Keywords are matched as substrings (case-insensitive).
# ---------------------------------------------------------------------------

# Income rules
INCOME_RULES = [
    # Salary
    ("招银网络科技", "工资", "工资"),
    ("思爱普", "工资", "工资"),
    # Fund redemption / investment returns
    ("代销理财快赎", "理财", "基金赎回"),
    ("待清算电子汇差", "理财", "基金赎回"),
    # Bank interest
    ("应付利息", "理财", "利息"),
    ("自动计提", "理财", "利息"),
    # Income – refunds from clothing/shopping
    ("GLYNNA", "购物", "退款"),
    ("看幸服饰", "购物", "退款"),
    ("朵朵棉", "购物", "退款"),
    ("艾优儿", "购物", "退款"),
    ("亚克西", "购物", "退款"),
    # Income – rail refunds
    ("中国铁路", "出行", "退款"),
    ("中铁网络", "出行", "退款"),
    # Income – platform refunds (美团/嘀嘀/etc already covered below after Income block)
    ("盒马", "购物", "退款"),
    ("京东", "购物", "退款"),
    ("拼多多", "购物", "退款"),
    ("格物致品", "购物", "退款"),
    ("美团", "餐饮", "退款"),
    ("嘀嘀", "出行", "退款"),
    ("高德", "出行", "退款"),
    ("滴滴", "出行", "退款"),
    ("中铁", "出行", "退款"),
    ("铁旅", "出行", "退款"),
    # Tax refund
    ("待报解中央与地方共享预算收入", "其他", "税费退款"),
    # Individual transfers received / misc
    ("微信转账", "转账", "微信转账"),
    ("网银在线", "转账", "网银转账"),
    ("贝壳找房", "其他", "其他收入"),
    # Misc
    ("特约商户", "其他", "其他收入"),
]

# Expense rules
EXPENSE_RULES = [
    # Transport
    ("嘀嘀", "出行", "网约车"),
    ("北京嘀嘀", "出行", "网约车"),
    ("高德打车", "出行", "网约车"),
    ("高德信息技术", "出行", "网约车"),
    ("滴滴顺风车", "出行", "网约车"),
    ("滴滴出行", "出行", "网约车"),
    ("天府通", "出行", "公共交通"),
    ("中国铁路", "出行", "火车"),
    ("中铁网络", "出行", "火车"),
    ("铁旅科技", "出行", "火车"),
    ("携程", "出行", "机票/酒店"),
    ("酒店", "出行", "住宿"),
    # Food & dining
    ("盒马", "餐饮", "超市/外卖"),
    ("舞东风", "餐饮", "超市/外卖"),
    ("红旗连锁", "餐饮", "超市/外卖"),
    ("乡村基", "餐饮", "餐厅"),
    ("吉牛旺", "餐饮", "餐厅"),
    ("月嫂家", "餐饮", "餐厅"),
    ("格外餐饮", "餐饮", "餐厅"),
    ("食在宣", "餐饮", "餐厅"),
    ("亚惠餐饮", "餐饮", "餐厅"),
    ("兴红得聪餐饮", "餐饮", "餐厅"),
    ("锦味臻鲜", "餐饮", "餐厅"),
    ("绵羊米粉", "餐饮", "餐厅"),
    ("绵阳米粉", "餐饮", "餐厅"),
    ("护国寺小吃", "餐饮", "餐厅"),
    ("老麻本味", "餐饮", "餐厅"),
    ("郑立强内江牛肉面", "餐饮", "餐厅"),
    ("一道菜", "餐饮", "餐厅"),
    ("幸福小串", "餐饮", "餐厅"),
    ("蜀户", "餐饮", "餐厅"),
    ("吉牛旺鸭血面", "餐饮", "餐厅"),
    ("早餐店", "餐饮", "餐厅"),
    ("烧烤", "餐饮", "餐厅"),
    ("抄手", "餐饮", "餐厅"),
    ("米粉", "餐饮", "餐厅"),
    ("面包", "餐饮", "烘焙/咖啡"),
    ("上海东客面包", "餐饮", "烘焙/咖啡"),
    ("柒一拾壹", "餐饮", "便利店"),
    ("罗森", "餐饮", "便利店"),
    ("肯德基", "餐饮", "快餐"),
    ("美团平台商户", "餐饮", "外卖"),
    ("拉扎斯", "餐饮", "外卖"),        # 饿了么母公司
    ("北京三快", "餐饮", "外卖"),       # 美团母公司
    ("食堂记", "餐饮", "餐厅"),
    # More dining keywords (catch-all patterns)
    ("串串", "餐饮", "餐厅"),
    ("火锅", "餐饮", "餐厅"),
    ("烤肉", "餐饮", "餐厅"),
    ("烤鸭", "餐饮", "餐厅"),
    ("烤牛", "餐饮", "餐厅"),
    ("麻辣烫", "餐饮", "餐厅"),
    ("麻辣串", "餐饮", "餐厅"),
    ("钵钵鸡", "餐饮", "餐厅"),
    ("牛肉面", "餐饮", "餐厅"),
    ("燃面", "餐饮", "餐厅"),
    ("豌杂面", "餐饮", "餐厅"),
    ("荤豆花", "餐饮", "餐厅"),
    ("肥肠", "餐饮", "餐厅"),
    ("卤煮", "餐饮", "餐厅"),
    ("卤", "餐饮", "餐厅"),
    ("奶茶", "餐饮", "饮料/甜品"),
    ("糖水", "餐饮", "饮料/甜品"),
    ("酸奶", "餐饮", "饮料/甜品"),
    ("冰粉", "餐饮", "饮料/甜品"),
    ("好利来", "餐饮", "烘焙/咖啡"),
    ("锅盔", "餐饮", "小吃"),
    ("蛋烘糕", "餐饮", "小吃"),
    ("包子", "餐饮", "小吃"),
    ("小吃", "餐饮", "小吃"),
    ("餐饮", "餐饮", "餐厅"),
    ("饭店", "餐饮", "餐厅"),
    ("食堂", "餐饮", "餐厅"),
    ("父母食堂", "餐饮", "餐厅"),
    ("小馆", "餐饮", "餐厅"),
    ("面馆", "餐饮", "餐厅"),
    ("德克士", "餐饮", "快餐"),
    ("百胜", "餐饮", "快餐"),          # 肯德基/必胜客母公司
    # Groceries / fresh produce
    ("好多水果", "餐饮", "生鲜/水果"),
    ("鲜诚多果蔬", "餐饮", "生鲜/水果"),
    ("满彭菜场", "餐饮", "生鲜/水果"),
    ("生活馆", "餐饮", "生鲜/水果"),
    ("个体黄娜鲜鸡", "餐饮", "生鲜/水果"),
    ("水果店", "餐饮", "生鲜/水果"),
    # Shopping – online
    ("京东商城", "购物", "网购"),
    ("拼多多", "购物", "网购"),
    ("格物致品", "购物", "网购"),       # 优衣库线上
    ("迅销", "购物", "网购"),           # 优衣库母公司
    ("网银在线", "购物", "网购"),       # 财付通/微信支付收款
    ("GLYNNA", "购物", "网购"),
    ("朵朵棉", "购物", "网购"),
    ("艾优儿", "购物", "网购"),
    ("欧莱雅", "购物", "美妆"),
    ("戴可思", "购物", "美妆"),
    ("安踏", "购物", "服装"),
    ("启东市亿任", "购物", "网购"),
    ("丰华烟酒", "购物", "烟酒"),
    # Housing / utilities
    ("国网四川省电力", "居家", "电费"),
    ("花样年.*物业", "居家", "物业费"),
    ("生活缴费", "居家", "生活缴费"),
    # Telecom
    ("中国移动", "通讯", "手机话费"),
    ("中移动金融", "通讯", "手机话费"),
    # Medical
    ("华西第二医院", "医疗", "医院"),
    ("爱生健康", "医疗", "健康管理"),
    ("安琪儿妇产", "医疗", "医院"),
    # Entertainment / sharing economy
    ("街电", "生活", "共享充电"),
    ("怪兽充电", "生活", "共享充电"),
    ("哈啰", "出行", "共享单车/租车"),
    ("杭州哈行", "出行", "共享单车/租车"),
    ("永信智慧", "生活", "停车"),
    ("动幻网吧", "娱乐", "网吧"),
    ("哔哩哔哩", "娱乐", "视频/游戏"),
    ("miHoYo", "娱乐", "视频/游戏"),
    ("小红书", "娱乐", "社交"),
    ("电子票务", "娱乐", "门票"),
    ("公园", "娱乐", "门票"),
    # Medical (expanded)
    ("华西第二医院", "医疗", "医院"),
    ("爱生健康", "医疗", "健康管理"),
    ("安琪儿妇产", "医疗", "医院"),
    ("省骨科医院", "医疗", "医院"),
    ("四川省骨科", "医疗", "医院"),
    ("高新区妇女儿童医院", "医疗", "医院"),
    ("高新区.*门诊", "医疗", "医院"),
    ("艾嘉综合门诊", "医疗", "医院"),
    ("华西妇幼", "医疗", "医院"),
    ("大药房", "医疗", "药店"),
    ("一心堂", "医疗", "药店"),
    ("药房", "医疗", "药店"),
    # Shopping – in-store / supermarket
    ("沃尔玛", "购物", "超市"),
    ("伊藤洋华堂", "购物", "超市"),
    ("宜享佳超市", "购物", "超市"),
    ("悠涵超市", "购物", "超市"),
    ("梓潼鲜雨汶超市", "购物", "超市"),
    ("百乐购超市", "购物", "超市"),
    ("都江堰市纽北超市", "购物", "超市"),
    ("见福便利", "餐饮", "便利店"),
    ("福满家便利", "餐饮", "便利店"),
    ("渣渣便利", "餐饮", "便利店"),
    # Shopping – clothing / personal care
    ("看幸服饰", "购物", "服装"),
    ("义乌市苏润服饰", "购物", "服装"),
    ("义乌市奥珑针织", "购物", "服装"),
    ("胜道运动", "购物", "运动"),
    ("想成为美女的店", "购物", "服装"),
    ("MinMin小个子", "购物", "服装"),
    ("梦回千屿", "购物", "服装"),
    ("南通唯林舍", "购物", "服装"),
    ("南昌雪菲俪", "购物", "服装"),
    ("歌家贸易", "购物", "服装"),
    ("娅靖品牌", "购物", "服装"),
    ("相宜云商", "购物", "美妆"),
    ("福瑞达生物", "购物", "美妆"),
    ("远想生物", "购物", "美妆"),
    ("璞致岱玛", "购物", "美妆"),
    ("广东省蔬果园生物", "购物", "美妆"),
    ("一佳造型", "购物", "美发/美容"),
    # Shopping – baby / maternity
    ("朵朵棉", "购物", "母婴"),
    ("艾优儿", "购物", "母婴"),
    ("台州市黄岩皓诚婴童", "购物", "母婴"),
    ("惠民妇儿", "购物", "母婴"),
    ("童季科技", "购物", "母婴"),
    # Shopping – home / electronics
    ("奥尔电气", "购物", "家电"),
    ("乐其网络", "购物", "数码"),
    ("长颈猫智能", "购物", "数码"),
    ("李琳电子", "购物", "数码"),
    ("傲域电子", "购物", "数码"),
    ("甄严选家具", "购物", "家居"),
    ("浙睿玩具", "购物", "玩具"),
    # Shopping – wine / tobacco
    ("广州裕鼎隆酒业", "购物", "烟酒"),
    ("梓潼老庙黄金", "购物", "金饰"),
    ("金饰", "购物", "金饰"),
    # Housing
    ("晶通汇", "居家", "物业费"),       # 成都晶通汇 = property management
    ("左邻先生", "居家", "物业费"),
    ("永信智慧", "居家", "停车"),
    # Travel / scenic spots
    ("希望叠松旅游", "出行", "旅游"),
    ("旅游便利店", "出行", "旅游"),
    ("所见所得", "娱乐", "旅游/景点"),
    # Misc individual payees — generic fallback for person names
    ("特约商户", "其他", "其他"),
    ("亚克西", "购物", "网购"),
    # Generic food keywords to catch long-tail merchants
    ("食品", "餐饮", "餐厅"),
    ("海鲜", "餐饮", "餐厅"),
    ("生鲜", "餐饮", "生鲜/水果"),
    ("菜场", "餐饮", "生鲜/水果"),
    ("超市", "购物", "超市"),
    ("便利店", "餐饮", "便利店"),
    ("啫火啫啫煲", "餐饮", "餐厅"),
    ("九锅一堂", "餐饮", "餐厅"),
    ("商户", "其他", "其他"),          # 商户许建荣 / 商户_冉燕妮 etc.
    ("圆明园", "娱乐", "门票"),
    ("灵感之茶", "餐饮", "饮料/甜品"),
    ("掌门土豆", "餐饮", "餐厅"),
    ("全棉时代", "购物", "服装"),
    ("光耀盛达", "购物", "网购"),
    ("宇洁商贸", "购物", "网购"),
    ("爱达乐", "餐饮", "餐厅"),
    ("协盛隆", "购物", "超市"),
    ("快鱼连锁", "餐饮", "餐厅"),
    ("点心铺", "餐饮", "小吃"),
    ("厚文信龙食品", "餐饮", "餐厅"),
    ("焖烧", "餐饮", "餐厅"),
    ("回香园", "餐饮", "餐厅"),
    ("本物之味", "餐饮", "餐厅"),
    ("莫光头牛肉", "餐饮", "餐厅"),
    ("有红鸡毛店", "餐饮", "餐厅"),
    ("老花溪", "餐饮", "餐厅"),
    ("隆府", "餐饮", "餐厅"),
    ("民族团结.*羊肉串", "餐饮", "餐厅"),
    ("白果树", "餐饮", "生鲜/水果"),
    ("黑味美川藏黑猪", "餐饮", "生鲜/水果"),
    ("鲜牛肉", "餐饮", "生鲜/水果"),
    ("馅太野", "餐饮", "餐厅"),
    ("环保新零售", "购物", "其他"),
    ("西堂圣物", "购物", "其他"),
    ("西北杂粮筐", "餐饮", "餐厅"),
    ("喜识", "购物", "其他"),
    # Transfers / social
    ("微信转账", "转账", "微信转账"),
    ("微信红包", "转账", "微信红包"),
    ("群收款", "转账", "微信转账"),
    ("扫二维码付款", "餐饮", "餐厅"),   # generic QR — default to dining
    # Investments
    ("代销理财", "理财", "理财产品"),
]

# Transfer rules (Account2 name → sub category)
TRANSFER_RULES = [
    ("王珮雯", "转账", "家人转账"),
    ("杨新雨", "转账", "本人转账"),
]


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
        f = open(output_path, "w", newline="", encoding="utf-8")
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

            category, sub_category = classify(tx_type, tx["Description"], account2_val)

            writer.writerow({
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
