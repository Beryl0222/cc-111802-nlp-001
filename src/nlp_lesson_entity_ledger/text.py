import re
import unicodedata


TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z]+(?:['’-][A-Za-z]+)*|\d+(?:\.\d+)?|[^\w\s]", re.UNICODE)
SENTENCE_RE = re.compile(r"[^。！？!?\n；;]+[。！？!?\n；;]?")
QUOTED_RE = re.compile(r"「([^」]+)」|『([^』]+)』|《([^》]+)》|“([^”]+)”")


def normalize_text(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def tokenize(text):
    return TOKEN_RE.findall(normalize_text(text))


def annotate(record):
    tokens = tokenize(record["text"])
    unique = []
    for token in tokens:
        if token not in unique:
            unique.append(token)
    return {
        "tokens": tokens,
        "terms": unique,
        "character_count": len(normalize_text(record["text"])),
        "token_count": len(tokens),
    }


def sentence_spans(text):
    return [(match.start(), match.end()) for match in SENTENCE_RE.finditer(text) if match.group().strip()]


def _fragment_at(spans, text, position):
    for start, end in spans:
        if start <= position < end:
            return text[start:end].strip()
    return ""


def extract_mentions(record):
    """从记录中定位提及。

    记录可携带上游文本标注给出的 ``mentions`` 名称列表;缺省时回退到
    「」『』《》“”括起的名称。每个提及给出在正文中的出现序号和所在原句,
    被更长名称包含的出现(如“稳压电源”中的“电源”)不单独算作提及。
    """
    text = record.get("text", "")
    raw = record.get("mentions") or []
    surfaces = []
    for item in raw:
        surface = item.get("surface") if isinstance(item, dict) else item
        surface = normalize_text(str(surface or "")).strip()
        if surface and surface not in surfaces:
            surfaces.append(surface)
    if not surfaces:
        for match in QUOTED_RE.finditer(text):
            surface = next(group for group in match.groups() if group)
            surface = surface.strip()
            if surface and surface not in surfaces:
                surfaces.append(surface)
    occurrences = []
    for surface in surfaces:
        for match in re.finditer(re.escape(surface), text):
            occurrences.append((match.start(), match.end(), surface))

    def contained(item):
        start, end, surface = item
        for other_start, other_end, other_surface in occurrences:
            if other_surface != surface and other_start <= start and end <= other_end \
                    and (other_end - other_start) > (end - start):
                return True
        return False

    occurrences = sorted(item for item in occurrences if not contained(item))
    spans = sentence_spans(text)
    mentions = []
    counters = {}
    for start, _end, surface in occurrences:
        index = counters.get(surface, 0)
        counters[surface] = index + 1
        mentions.append({"surface": surface, "occurrence": index, "fragment": _fragment_at(spans, text, start)})
    for surface in surfaces:
        if surface not in counters:
            mentions.append({"surface": surface, "occurrence": 0, "fragment": ""})
    return mentions
