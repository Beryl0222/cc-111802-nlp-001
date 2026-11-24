import re
import unicodedata
from difflib import SequenceMatcher


TOKEN_RE = re.compile(r"[㐀-鿿]|[A-Za-z]+(?:['’-][A-Za-z]+)*|\d+(?:\.\d+)?|[^\w\s]", re.UNICODE)

# 用于截取“原文片段”的句子/分句边界。
CLAUSE_BOUNDARY_RE = re.compile(r"[。！？!?；;\n…]")
SOFT_BOUNDARY_RE = re.compile(r"[，,、：:（）()「」“”]")


def normalize_text(text):
    return " ".join(unicodedata.normalize("NFKC", str(text)).split())


def tokenize(text):
    return TOKEN_RE.findall(normalize_text(text))


def annotate(record):
    text = normalize_text(record["text"])
    tokens = TOKEN_RE.findall(text)
    unique = []
    for token in tokens:
        if token not in unique:
            unique.append(token)
    return {
        "tokens": tokens,
        "terms": unique,
        "character_count": len(text),
        "token_count": len(tokens),
    }


def name_key(value):
    """生成用于名称精确比对的键：NFKC、去空白与标点、拉丁字母小写。"""
    text = normalize_text(value).lower()
    return "".join(ch for ch in text if ch.strip() and not _is_separator(ch))


def _is_separator(ch):
    category = unicodedata.category(ch)
    return category.startswith("P") or category.startswith("Z") or category.startswith("C")


def find_spans(text, surface):
    """返回 surface 在 text 中的全部非重叠起止位置（优先精确匹配，再尝试忽略大小写）。"""
    if not surface:
        return []
    needle = normalize_text(surface)
    spans = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        spans.append((index, index + len(needle)))
        start = index + max(1, len(needle))
    if spans:
        return spans
    # 拉丁名称的大小写兜底。
    lowered = text.lower()
    needle_lower = needle.lower()
    if needle_lower and needle_lower != needle:
        start = 0
        while True:
            index = lowered.find(needle_lower, start)
            if index < 0:
                break
            spans.append((index, index + len(needle_lower)))
            start = index + max(1, len(needle_lower))
    return spans


def quote_at(text, start, end, radius=18):
    """截取包含 [start, end) 的原文片段，尽量保持一个完整句子/分句。"""
    if start is None or end is None or start < 0 or end <= start:
        return ""
    length = len(text)
    start = max(0, min(start, length))
    end = max(0, min(end, length))
    if end <= start:
        return ""

    left = 0
    for match in CLAUSE_BOUNDARY_RE.finditer(text, 0, start):
        left = match.end()
    right = length
    match = CLAUSE_BOUNDARY_RE.search(text, end)
    if match:
        right = match.start()

    snippet = text[left:right].strip()
    if len(snippet) <= radius * 3 or (start - left <= radius and right - end <= radius * 2):
        return snippet

    # 句子过长时退回到提及周围的定长窗口。
    left_soft = start
    window_left = max(0, start - radius)
    soft = list(SOFT_BOUNDARY_RE.finditer(text, window_left, start))
    if soft:
        left_soft = soft[-1].end()
    else:
        left_soft = window_left
    right_soft = min(length, end + radius * 2)
    soft = SOFT_BOUNDARY_RE.search(text, end, min(length, end + radius * 2))
    if soft:
        right_soft = soft.start()
    prefix = "…" if left_soft > 0 else ""
    suffix = "…" if right_soft < length else ""
    return prefix + text[left_soft:right_soft].strip() + suffix


def name_similarity(left, right):
    """中文简称/全称之间的保守相似度，仅用于产生候选，不用于自动合并。"""
    a = name_key(left)
    b = name_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    containment = 0.0
    if short and short in long:
        # “滑动变阻器” -> “变阻器”这类简称：短名占长名比例越高越可信。
        containment = 0.55 + 0.45 * (len(short) / len(long))
    return max(ratio, containment)


def slug8(*parts):
    import hashlib

    return hashlib.sha1("|".join("" if part is None else str(part) for part in parts).encode("utf-8")).hexdigest()[:12]
