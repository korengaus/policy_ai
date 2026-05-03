import html
import re

try:
    import ftfy
except Exception:
    ftfy = None


MOJIBAKE_MARKERS = (
    "ë",
    "ì",
    "ê",
    "í",
    "Ã",
    "Â",
    "ð",
    "챙",
    "챠",
    "챘",
    "횂",
    "占",
)


def has_mojibake(text: str) -> bool:
    return isinstance(text, str) and any(marker in text for marker in MOJIBAKE_MARKERS)


def _hangul_count(text: str) -> int:
    return len(re.findall(r"[\uac00-\ud7a3]", text or ""))


def _readability_score(text: str) -> int:
    if not text:
        return -10000
    hangul = _hangul_count(text)
    replacement = text.count("\ufffd") + text.count("占")
    mojibake = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    readable = len(re.findall(r"[\uac00-\ud7a3A-Za-z0-9]", text))
    return hangul * 8 + readable - replacement * 80 - mojibake * 30


def repair_mojibake(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text or ""

    candidates = [text]

    if ftfy is not None:
        try:
            candidates.append(ftfy.fix_text(text))
        except Exception:
            pass

    for source_encoding in ("latin1", "cp1252"):
        try:
            candidates.append(text.encode(source_encoding, errors="strict").decode("utf-8", errors="strict"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    for source_encoding in ("latin1", "cp1252"):
        try:
            once = text.encode(source_encoding, errors="strict").decode("utf-8", errors="strict")
            candidates.append(once.encode(source_encoding, errors="strict").decode("utf-8", errors="strict"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    return max(candidates, key=_readability_score)


def sanitize_text(text: str) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        return text
    repaired = repair_mojibake(html.unescape(text))
    repaired = re.sub(r"[\u200b-\u200f\ufeff]", "", repaired)
    repaired = repaired.replace("\ufffd", "")
    repaired = re.sub(r"[ \t\r\f\v]+", " ", repaired)
    repaired = re.sub(r"\n{3,}", "\n\n", repaired)
    return repaired.strip()


def sanitize_data(value):
    if isinstance(value, dict):
        return {key: sanitize_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_data(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def decode_response_text(response, fallback_encodings=None) -> tuple[str, str]:
    fallback_encodings = fallback_encodings or ["utf-8", "cp949", "euc-kr"]
    candidates = []

    apparent = getattr(response, "apparent_encoding", None)
    declared = getattr(response, "encoding", None)

    for encoding in [apparent, "utf-8", declared, *fallback_encodings]:
        if not encoding:
            continue
        try:
            decoded = response.content.decode(encoding, errors="strict")
        except Exception:
            try:
                decoded = response.content.decode(encoding, errors="replace")
            except Exception:
                continue
        candidates.append((sanitize_text(decoded), encoding))

    if not candidates:
        return sanitize_text(getattr(response, "text", "") or ""), declared or "unknown"

    return max(candidates, key=lambda item: _readability_score(item[0]))
