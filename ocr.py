from __future__ import annotations

import gc
import re
import unicodedata

import cv2
import numpy as np
import pytesseract
from PIL import Image

# Reject absurdly large images before decoding them. Bank-transfer slips are phone
# screenshots (a few megapixels); a highly-compressible multi-hundred-megapixel PNG/WebP
# would otherwise decode into multiple GB of RAM here (and get upscaled 2x in pass 4),
# OOM-killing the worker. Probing the header via PIL is cheap (no full decode).
MAX_OCR_PIXELS = 40_000_000  # 40 MP
Image.MAX_IMAGE_PIXELS = MAX_OCR_PIXELS


# ─── OCR preprocessing ──────────────────────────────────────────────────────

def _preprocess_passes(image_path: str) -> list[np.ndarray]:
    try:
        with Image.open(image_path) as _probe:
            _pw, _ph = _probe.size
        if _pw * _ph > MAX_OCR_PIXELS:
            raise ValueError(f"image too large for OCR: {_pw}x{_ph} pixels")
    except (OSError, ValueError) as exc:
        # Re-raise size violations; ignore probe failures (cv2 may still handle the file).
        if isinstance(exc, ValueError):
            raise
    img = cv2.imread(image_path)
    if img is None:
        img_pil = Image.open(image_path)
        img = cv2.cvtColor(np.array(img_pil.convert("RGB")), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Pass 1: OTSU binarization
    _, p1 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Pass 2: adaptive threshold (handles gradient / uneven lighting)
    p2 = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 31, 10)

    # Pass 3: CLAHE + OTSU (low-contrast decorative slips)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    _, p3 = cv2.threshold(clahe.apply(gray), 0, 255,
                          cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Pass 4: 2x upscale + adaptive (small text on decorative backgrounds)
    h, w = gray.shape
    gray_up = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)
    p4 = cv2.adaptiveThreshold(gray_up, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 31, 10)

    return [p1, p2, p3, p4]


def _ocr_all_passes(image_path: str) -> list[list[str]]:
    candidates = []
    for arr in _preprocess_passes(image_path):
        raw = pytesseract.image_to_string(Image.fromarray(arr),
                                          lang="tha+eng", config="--psm 4")
        candidates.append([line for line in raw.splitlines() if line.strip()])
        gc.collect()
    return candidates


# ─── Scoring ────────────────────────────────────────────────────────────────

def _score(lines: list[str]) -> int:
    """Higher = better OCR result."""
    full = " ".join(lines)
    score = len(lines)
    if any(kw in full for kw in ["เลขที่รายการ", "เลขทีรายการ", "รหัสอ้างอิง",
                                  "รหัสอฮ้างอิง", "รหัสอฮ้างฮิง", "ref", "REF"]):
        score += 10
    if re.search(r'[0-9A-Za-z]{15,}', full):
        score += 20
    # Penalize garbage box-drawing / non-Thai non-ASCII chars
    garbage = sum(1 for c in full if ord(c) > 0x2500 and not '฀' <= c <= '๿')
    if garbage > len(full) * 0.05:
        score -= 15
    return score


# ─── Bank detection ─────────────────────────────────────────────────────────

APP_BRANDS = [
    ("KBANK",     ["K+", "K PLUS", "KPLUS", "i+", "กสิกร"]),
    ("KTB",       ["Krungthai", "กรุงไทย"]),
    ("PAOTANG",   ["เป๋าตัง", "G Wallet"]),
    ("SCB",       ["SCB", "ไทยพาณิชย์"]),
    ("KRS",       ["krungsri", "Krungsri", "กรุงศรี"]),
    ("TTB",       ["ttb", "ทหารไทยธนชาต", "ทีเอ็มบี", "ทีทีบี"]),
    ("GSB",       ["ออมสิน", "GSB", "MyMo"]),
    ("BBL",       ["Bangkok Bank"]),
    ("KKP",       ["เกียรตินาคิน", "KKP"]),
    ("CIMB",      ["CIMB", "ซีไอเอ็มบี"]),
    ("UOB",       ["UOB", "ยูโอบี"]),
    ("LHBANK",    ["LH Bank", "แลนด์"]),
    ("TISCO",     ["TISCO", "ทิสโก้"]),
    ("TRUEMONEY", ["TrueMoney", "ทรูมันนี่", "True Money"]),
    ("PROMPTPAY", ["พร้อมเพย์", "PromptPay"]),
]


def _detect_bank(lines: list[str], full_text: str) -> str | None:
    top_text = " ".join(lines[:6])
    for code, keywords in APP_BRANDS:
        for kw in keywords:
            if kw in top_text or kw.lower() in top_text.lower():
                return code
    for code, keywords in APP_BRANDS:
        for kw in keywords:
            if kw in full_text or kw.lower() in full_text.lower():
                return code
    return None


# ─── Ref extraction ─────────────────────────────────────────────────────────

REF_KEYWORDS = [
    "เลขที่รายการ", "เลขทีรายการ", "เลขที่อ้างอิง", "เลขทีอ้างอิง",
    "รหัสอ้างอิง", "รหัสอฮ้างอิง", "รหัสอฮ้างฮิง", "หมายเลขอ้างอิง", "รหัสรายการ",
    "ref no", "reference",
    "transaction id", "transaction ref",
]

MEMO_KEYWORDS = ["บันทึกช่วยจำ", "บันทึก ช่วยจำ", "memo", "หมายเหตุ", "note"]

OCR_CHAR_MAP = str.maketrans({
    "[": "E", "]": "J", "|": "I", "!": "I",
    "{": "C", "}": "J", "\\": "I",
})

KBANK_SUFFIXES = {"BOR", "COR", "DOR", "AOR", "DTF", "BOF", "COF", "DOF", "AOF"}

# Digit → letter substitutions for recovering KBank suffix misreads
_DIGIT_TO_LETTER: dict[str, str] = {
    "0": "O", "6": "G", "8": "B", "3": "E", "1": "I", "5": "S",
}


def _strip_thai(s: str) -> str:
    return re.sub(r'[฀-๿]', '', s)


def _clean_ocr(s: str) -> str:
    return _strip_thai(s.translate(OCR_CHAR_MAP).replace(" ", ""))


def _clean_ref(s: str) -> str:
    """Like _clean_ocr but also strips non-alphanumeric noise (_, —, », etc.)."""
    s = _strip_thai(s.translate(OCR_CHAR_MAP).replace(" ", ""))
    return re.sub(r'[^0-9A-Za-z]', '', s)


def _hex_normalize(s: str) -> str:
    """Normalize common OCR misreads that appear in hex refs (l/L/i→1, O/o→0)."""
    return s.replace("l", "1").replace("L", "1").replace("i", "1").replace("O", "0").replace("o", "0")


def _looks_like_ref(s: str) -> bool:
    stripped = _strip_thai(s)
    if stripped != s and len(stripped) >= 10:
        s = stripped
    if len(s) < 10:
        return False
    if not re.match(r'^[0-9A-Za-z]+$', s):
        return False
    if re.match(r'^[a-z]+$', s):
        return False
    digit_ratio = sum(1 for c in s if c.isdigit()) / len(s)
    # Mostly letters: accept only pure hex or near-hex (l→1, O→0 are common OCR errors on KTB teal bg)
    return digit_ratio >= 0.3 or bool(re.match(r'^[A-Fa-f0-9]+$', _hex_normalize(s)))


def _ref_quality(s: str) -> int:
    """Score a ref candidate — higher = more likely to be the real ref."""
    if not s:
        return -1
    s_norm = _hex_normalize(s)
    score = len(s_norm)
    letter_count = sum(1 for c in s_norm if c.isalpha())
    digit_count = sum(1 for c in s_norm if c.isdigit())
    if re.match(r'^[A-Fa-f0-9]+$', s_norm):
        # Valid hex ref — letter-starting (KTB style) gets a big boost
        score += 15 if (14 <= len(s_norm) <= 20 and s_norm[0].isalpha()) else 5
    elif letter_count >= 2 and digit_count >= 8:
        score += 10
    if len(s_norm) >= 18 and s_norm[-3:].upper() in KBANK_SUFFIXES:
        score += 20
    return score


def _fix_kbank_ref(ref: str) -> str:
    """Try to recover a KBank ref whose suffix letters were misread as digits."""
    if len(ref) < 18 or ref[-3:].upper() in KBANK_SUFFIXES:
        return ref
    suffix = "".join(_DIGIT_TO_LETTER.get(ch, ch) for ch in ref[-3:])
    if suffix.upper() in KBANK_SUFFIXES:
        return ref[:-3] + suffix
    return ref


def _extract_ref(lines: list[str], full_text: str) -> str | None:
    memo_indices: set[int] = set()
    for i, line in enumerate(lines):
        if any(kw in line for kw in MEMO_KEYWORDS):
            memo_indices.update({i, i + 1, i + 2})

    for i, line in enumerate(lines):
        if i in memo_indices:
            continue
        line_clean = line.lower().replace(" ", "")
        if any(kw.replace(" ", "") in line_clean for kw in REF_KEYWORDS):
            parts = re.split(r'[:\s]+', line)
            for part in parts:
                cleaned = _clean_ref(part.strip())
                if _looks_like_ref(cleaned):
                    return cleaned
            if i + 1 < len(lines) and (i + 1) not in memo_indices:
                for part in re.split(r'[:\s]+', lines[i + 1]):
                    next_cleaned = _clean_ref(part.strip())
                    if _looks_like_ref(next_cleaned):
                        return next_cleaned

    combined = _clean_ocr(full_text)

    kbank_match = re.search(r'(\d{12,}[A-Za-z]{2,}[A-Za-z0-9]{3,})', combined)
    if kbank_match:
        return kbank_match.group(1)

    scb_match = re.search(r'(20\d{4,}[A-Za-z][A-Za-z0-9]{8,})', combined)
    if scb_match:
        return scb_match.group(1)

    ksa_match = re.search(r'(KSA[0-9]{10,})', combined, re.IGNORECASE)
    if ksa_match:
        return ksa_match.group(1)

    mn_match = re.search(r'([A-Z]{2}\d{15,})', combined)
    if mn_match:
        return mn_match.group(1)

    hex_match = re.search(r'([A-Fa-f][0-9a-fA-F]{12,})', combined)
    if hex_match:
        return hex_match.group(1)

    for i, line in enumerate(lines):
        if i in memo_indices:
            continue
        cleaned = _clean_ref(line)
        if _looks_like_ref(cleaned) and len(cleaned) >= 15:
            return cleaned

    long_num = re.search(r'(\d{18,})', combined)
    if long_num:
        return long_num.group(1)

    return None


def _extract_best_ref(candidates: list[list[str]]) -> str | None:
    """Try ref extraction on every OCR pass; return the highest-quality result."""
    best_ref: str | None = None
    best_score = -1
    for lines in candidates:
        ref = _extract_ref(lines, " ".join(lines))
        if ref:
            # If the ref becomes valid hex after normalization, apply it now
            # (handles l→1 and O→0 misreads on KTB teal background)
            norm = _hex_normalize(ref)
            if norm != ref and re.match(r'^[A-Fa-f0-9]+$', norm) and len(norm) >= 14:
                ref = norm
            score = _ref_quality(ref)
            if score > best_score:
                best_score = score
                best_ref = ref
    return best_ref


# ─── Slip validation ────────────────────────────────────────────────────────

SLIP_KEYWORDS = [
    "โอนเงิน", "สำเร็จ", "ทำรายการสำเร็จ", "รายการสำเร็จ",
    "เลขที่รายการ", "รหัสอ้างอิง", "หมายเลขอ้างอิง", "รหัสรายการ",
    "จำนวน", "จำนวนเงิน", "บาท", "thb",
    "สแกนตรวจสอบ", "ค่าธรรมเนียม",
    "ผู้รับเงิน", "ไปยัง", "บันทึกช่วยจำ",
    "ตรวจสอบสถานะ",
    "transfer completed", "transfer successful",
    "transaction id", "transaction ref", "transaction no",
    "scan for verify", "verify slip",
    "baht", "fee:",
]

NAME_PREFIXES = [
    "นาย", "นาง", "น.ส.", "น.ส", "นางสาว",
    "ด.ช.", "ด.ช", "ด.ญ.", "ด.ญ",
    "MR.", "MR ", "MRS.", "MRS ", "MISS ", "MS.",
    "Mr.", "Mrs.", "Miss ", "Ms.",
]


def _nfc(s: str) -> str:
    # Tesseract splits ำ (U+0E33 SARA AM) into ํ (U+0E4D) + า (U+0E32) — normalize back
    s = s.replace('ํา', 'ำ').replace('าํ', 'ำ')
    return unicodedata.normalize("NFC", s)


def _extract_amount(lines: list[str], full_text: str) -> float | None:
    nlines = [_nfc(line) for line in lines]
    ntext = _nfc(full_text)

    # P1: จำนวน keyword with decimal amount (X.XX)
    m = re.search(r'(?:จำนวนเงิน|จำนวน)[:\s]*(\d{1,3}(?:,\d{3})*\.\d{2})', ntext)
    if m:
        val = float(m.group(1).replace(",", ""))
        if val > 0:
            return val

    # P2: จำนวน keyword with space-separated decimal (399 00 → 399.00)
    m = re.search(r'(?:จำนวนเงิน|จำนวน)[:\s]*(\d{1,3}(?:,\d{3})*)\s+(\d{2})\b', ntext)
    if m:
        val = float(m.group(1).replace(",", "") + "." + m.group(2))
        if val > 0:
            return val

    # P3: จำนวน keyword — prefer decimal (X.XX) first, then integer fallback
    for i, line in enumerate(nlines):
        line_clean = line.replace("!", "").replace("|", "").replace(" ", "")
        if "จำนวน" in line_clean or "จำนวนเงิน" in line_clean:
            # Decimal amount first (avoids picking up single-digit OCR noise before real amount)
            m = re.search(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
            if m:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    return val
            # Integer fallback — require >= 2 digits and > 10 to skip noise
            m = re.search(r'(\d{2,}(?:,\d{3})*)', line)
            if m:
                val = float(m.group(1).replace(",", ""))
                if val > 10:
                    return val
            if i + 1 < len(nlines):
                m2 = re.search(r'(\d+(?:,\d{3})*(?:\.\d{2})?)', nlines[i + 1])
                if m2:
                    val = float(m2.group(1).replace(",", ""))
                    if val > 0:
                        return val

    # P4: first non-zero X.XX บาท/baht/THB
    for m in re.finditer(r'(\d{1,3}(?:,\d{3})*\.\d{2})\s*(?:บาท|baht|THB)', ntext, re.IGNORECASE):
        val = float(m.group(1).replace(",", ""))
        if val > 0:
            return val

    # P5: first non-zero integer บาท/THB
    for m in re.finditer(r'(\d+(?:,\d{3})*)\s+(?:บาท|THB)', ntext):
        val = float(m.group(1).replace(",", ""))
        if val > 0:
            return val

    # P6: first decimal > 1 anywhere (KBank gradient slips often lack บาท keyword nearby)
    for m in re.finditer(r'(\d{1,4}(?:,\d{3})*\.\d{2})', ntext):
        val = float(m.group(1).replace(",", ""))
        if val > 1:
            return val

    return None


def _extract_date(full_text: str) -> str | None:
    thai = re.search(
        r'(\d{1,2}\s*(?:ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\s*\d{2,4})',
        full_text
    )
    if thai:
        return thai.group(1)
    num = re.search(r'(\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4})', full_text)
    if num:
        return num.group(1).replace(" ", "")
    return None


def _extract_sender(lines: list[str]) -> str | None:
    for line in lines:
        for prefix in NAME_PREFIXES:
            if prefix in line:
                return line.strip()
    for i, line in enumerate(lines):
        if ("จาก" in line or "from" in line.lower()) and i + 1 < len(lines) and len(lines[i + 1].strip()) > 3:
            return lines[i + 1].strip()
    return None


# ─── Public API ─────────────────────────────────────────────────────────────

def extract_text(image_path: str) -> list[str]:
    """Return the best OCR line-list across all preprocessing passes."""
    return max(_ocr_all_passes(image_path), key=_score)


def extract_slip_data(image_path: str) -> dict:
    candidates = _ocr_all_passes(image_path)   # single call — reused by all downstream
    lines = max(candidates, key=_score)
    full_text = " ".join(lines)
    full_lower = full_text.lower()

    data: dict = {
        "is_slip": False,
        "ref_number": None,
        "amount": None,
        "date": None,
        "sender": None,
        "bank": None,
        "raw_text": lines,
    }

    full_lower_nfc = _nfc(full_lower)
    match_count = sum(1 for kw in SLIP_KEYWORDS if _nfc(kw).lower() in full_lower_nfc)
    data["is_slip"] = match_count >= 2

    ref = _extract_best_ref(candidates)        # reuses candidates — no extra OCR
    if ref:
        ref = ref.upper()
        ref = _fix_kbank_ref(ref)
    data["ref_number"] = ref

    data["amount"] = _extract_amount(lines, full_text)

    if not data["is_slip"] and data["ref_number"] and data["amount"]:
        data["is_slip"] = True

    data["date"] = _extract_date(full_text)
    # Check all passes for bank detection — header may only be legible in one pass
    all_text_for_bank = " ".join(" ".join(c) for c in candidates)
    data["bank"] = _detect_bank(lines, all_text_for_bank)
    data["sender"] = _extract_sender(lines)

    # KTB (Krungthai) refs are pure hex — apply full hex normalization as safety net
    if data["bank"] == "KTB" and data["ref_number"]:
        data["ref_number"] = _hex_normalize(data["ref_number"])

    return data
