"""Strip personally identifiable information from search queries before they are logged.

Two layers:
- regexes for structured identifiers: emails, URLs, IP addresses, phone numbers, precise
  coordinates, and long ID-like numbers;
- a multilingual named-entity model (en/es/fr/pt and others) that replaces person names.
Years, quantities, places and organisations are kept; they are needed for usage reporting.
Scrubbing is automated and imperfect, so callers must fail closed: if scrub() raises, log nothing.
"""
import re

NER_MODEL = "Davlan/distilbert-base-multilingual-cased-ner-hrl"
PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,}\d(?![\w.])")
PATTERNS = [
    ("[EMAIL]", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("[URL]", re.compile(r"\b(?:https?://|www\.)\S+", re.I)),
    ("[IP]", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("[COORDINATES]", re.compile(r"-?\d{1,3}\.\d{3,}\s*°?\s*[NS]?\s*[,;/ ]\s*-?\d{1,3}\.\d{3,}\s*°?\s*[EW]?", re.I)),
    ("[ID]", re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9-]{8,}\b")),
    ("[NUMBER]", re.compile(r"\b\d{6,}\b")),
]


class Scrubber:
    def __init__(self, device=-1):
        from transformers import pipeline
        self.ner = pipeline("token-classification", model=NER_MODEL, aggregation_strategy="max",
                            device=device)

    def scrub(self, text):
        for tag, pat in PATTERNS:
            text = pat.sub(f" {tag} ", text)
        # phone numbers have >= 9 digits; this spares year ranges such as 2015-2020
        text = PHONE.sub(lambda m: " [PHONE] " if sum(c.isdigit() for c in m.group()) >= 9 else m.group(), text)
        ents = self.ner(text)
        other = [(e["start"], e["end"]) for e in ents if e["entity_group"] != "PER"]
        spans = []
        for e in ents:
            if e["entity_group"] != "PER" or e["score"] < 0.5:
                continue
            a, b = e["start"], e["end"]  # widen to whole words
            while a > 0 and text[a - 1].isalnum():
                a -= 1
            while b < len(text) and text[b].isalnum():
                b += 1
            # the model often misses surnames: absorb up to 3 following capitalized words
            # that it did not tag as a place or organisation
            for _ in range(3):
                m = re.match(r"\s+([A-ZÀ-Ý][\w'’-]*)", text[b:])
                if not m or any(s <= b + m.start(1) < t for s, t in other):
                    break
                b += m.end()
            if spans and not text[spans[-1][1]:a].strip():  # merge "first last"
                spans[-1][1] = b
            else:
                spans.append([a, b])
        for a, b in reversed(spans):
            text = text[:a] + "[NAME]" + text[b:]
        return re.sub(r"\s+", " ", text).replace(" ,", ",").strip()
