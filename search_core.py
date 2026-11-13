"""Hybrid search over the CGIAR publications index (BM25 via SQLite FTS5 + bge-m3 dense vectors).

The index is built by build/build_ask_index.py, build/build_ask_embeddings.py and
build/build_subset.py: <base>.sqlite, <base>.bge-m3.f16 (float16 [n, 1024], row i = chunk
rowid i+1) and <base>.bge-m3.json.
"""
import json, re, sqlite3, threading

import numpy as np

POOL = 200   # candidates per retriever before fusion
RRF_K = 60
STOP = set("""a an the and or of in on at to for from by with without is are was were be been being
this that these those it its as into about than then what which who whom whose how why when where
do does did can could should would may might will shall not no yes my our your their there here
i we you they he she them us me more most less least very much many some any all each other such
de la le les el los las del des du un una une et y o e en para por con sur dans que qui est son
sus ses da do dos das em na no nas nos um uma com ao à é se como mais""".split())
YEAR = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
REF_MARK = re.compile(r"doi|https?:|et al|journal|\bvol\.|\bpp\.|\d+\s*\(\d+\)|\d+\s*[-–]\s*\d+\.?\s*$"
                      r"|^\s*-?\s*[A-Z][\w'’-]+,?\s+(?:[A-Z]{1,3}\b|[A-Z]\.)", re.I | re.M)
DOC_COLS = ("title", "title_en", "year", "url", "rights", "license_category", "language", "text_type",
            "resource_type", "countries", "regions", "crops", "livestock", "n_chunks")


class SearchError(ValueError):
    pass


def is_references(text):
    """Bibliography passages: most lines look like reference entries."""
    lines = [l for l in text.split("\n") if len(l.strip()) >= 20]
    refs = sum(len(l) <= 250 and bool(YEAR.search(l) or REF_MARK.search(l)) for l in lines)
    return refs >= 4 and refs >= 0.5 * len(lines)


def fts_query(q):
    phrases = re.findall(r'"([^"]+)"', q)
    rest = re.sub(r'"[^"]+"', " ", q)
    terms = [t for t in re.findall(r"\w+", rest.lower()) if t not in STOP and len(t) > 1]
    parts = [f'"{p.replace(chr(34), "")}"' for p in phrases] + [f'"{t}"' for t in dict.fromkeys(terms)]
    if not parts:
        raise SearchError("No searchable terms in query.")
    return " OR ".join(parts)


def semantic_text(q):
    return re.sub(r"\s+", " ", re.sub(r'\b(?:AND|OR|NOT|NEAR)\b|["()*^]', " ", q)).strip()


class Index:
    def __init__(self, base, model_name=None, device="cpu"):
        from sentence_transformers import SentenceTransformer
        self.path = f"{base}.sqlite"
        meta = json.load(open(f"{base}.bge-m3.json"))
        self.meta = meta
        vecs = np.memmap(f"{base}.bge-m3.f16", dtype=np.float16, mode="r", shape=(meta["n"], meta["dim"]))
        self.emb = np.empty(vecs.shape, dtype=np.float32)
        for i in range(0, meta["n"], 100_000):
            self.emb[i:i + 100_000] = vecs[i:i + 100_000]
        self.model = SentenceTransformer(model_name or meta["model"], device=device)
        self.lock = threading.Lock()
        db = self.db()
        self.n_docs = db.execute("SELECT count(*) FROM docs").fetchone()[0]
        self.licenses = [r[0] for r in db.execute("SELECT DISTINCT rights FROM docs ORDER BY 1")]

    def db(self):
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)

    def dense(self, query, k, ranges):
        with self.lock:
            q = self.model.encode([query], normalize_embeddings=True)[0].astype(np.float32)
        scores = self.emb @ q
        if ranges is not None:
            mask = np.zeros(len(scores), dtype=bool)
            for first, n in ranges:
                mask[first - 1:first - 1 + n] = True
            scores = np.where(mask, scores, -2.0)
        top = np.argpartition(-scores, min(k, len(scores) - 1))[:k]
        top = top[np.argsort(-scores[top])]
        return [int(i) + 1 for i in top if scores[i] > -2.0]

    def search(self, query, k=8, mode="hybrid", country=None, region=None, year_from=None, year_to=None,
               language=None, full_text_only=False, per_doc=2, raw=False, include_refs=False):
        """Return a list of passage dicts, best first."""
        if mode not in ("hybrid", "keyword", "semantic"):
            raise SearchError("mode must be hybrid, keyword or semantic")
        filters, fparams = [], []
        for col, val in (("countries", country), ("regions", region)):
            if val:
                filters.append(f"d.{col} LIKE ?")
                fparams.append(f"%{val}%")
        if year_from:
            filters.append("d.year >= ?"); fparams.append(int(year_from))
        if year_to:
            filters.append("d.year <= ?"); fparams.append(int(year_to))
        if language:
            filters.append("d.language = ?"); fparams.append(language)
        if full_text_only:
            filters.append("d.text_type = 'full_text'")

        db = self.db()
        ranked = {}
        if mode in ("hybrid", "semantic"):
            ranges = None
            if filters:
                ranges = db.execute(f"SELECT first_rowid, n_chunks FROM docs d WHERE {' AND '.join(filters)}",
                                    fparams).fetchall()
            text = semantic_text(query)
            if text:
                for rank, rowid in enumerate(self.dense(text, POOL, ranges)):
                    ranked[rowid] = ranked.get(rowid, 0) + 1 / (RRF_K + rank)
        if mode in ("hybrid", "keyword"):
            where = ["chunks MATCH ?"] + filters
            params = [query if raw else fts_query(query)] + fparams
            try:
                kw = db.execute(f"""SELECT c.rowid FROM chunks c JOIN docs d ON d.gardian_id = c.gardian_id
                                    WHERE {' AND '.join(where)} ORDER BY bm25(chunks, 1.0, 2.0) LIMIT ?""",
                                params + [POOL]).fetchall()
            except sqlite3.OperationalError as e:
                raise SearchError(f"Invalid keyword query ({e}). Check FTS5 syntax or drop raw=true.")
            for rank, (rowid,) in enumerate(kw):
                ranked[rowid] = ranked.get(rowid, 0) + 1 / (RRF_K + rank)

        order = sorted(ranked, key=ranked.get, reverse=True)
        byid = {}
        for i in range(0, len(order), 500):
            part = order[i:i + 500]
            for r in db.execute(f"""
                    SELECT c.rowid, c.gardian_id, c.chunk_no, c.text, d.title, d.title_en, d.year, d.url,
                           d.rights, d.language, d.text_type, d.countries
                    FROM chunks c JOIN docs d ON d.gardian_id = c.gardian_id
                    WHERE c.rowid IN ({','.join('?' * len(part))})""", part):
                byid[r[0]] = r
        rows = [byid[r] for r in order if r in byid]
        if not include_refs:  # demote, don't drop: the heuristic has false positives
            rows = sorted(rows, key=lambda r: is_references(r[3]))
        seen, hits = {}, []
        for r in rows:
            if seen.get(r[1], 0) < per_doc:
                seen[r[1]] = seen.get(r[1], 0) + 1
                hits.append(dict(doc_id=r[1], passage=int(r[2]) + 1, text=r[3].strip(), title=r[4],
                                 title_en=r[5], year=r[6], url=r[7], license=r[8], language=r[9],
                                 text_type=r[10], countries=r[11]))
            if len(hits) >= k:
                break
        return hits

    def document(self, doc_id, around=None, window=2, all_passages=False, max_chars=30000):
        db = self.db()
        db.row_factory = sqlite3.Row
        d = db.execute("SELECT * FROM docs WHERE gardian_id = ?", (doc_id,)).fetchone()
        if not d:
            raise SearchError(f"No document with id {doc_id}")
        out = dict(doc_id=doc_id, abstract=(d["abstract"] or "").strip(), passages=[], truncated=False,
                   **{k: d[k] for k in DOC_COLS})
        if not (around or all_passages):
            return out
        n = d["n_chunks"]
        lo, hi = (0, n - 1) if all_passages else (around - 1 - window, around - 1 + window)
        lo, hi = max(lo, 0), min(hi, n - 1)
        used = 0
        for cno, text in db.execute("SELECT rowid - ?, text FROM chunks WHERE rowid BETWEEN ? AND ? ORDER BY rowid",
                                    (d["first_rowid"], d["first_rowid"] + lo, d["first_rowid"] + hi)):
            if used + len(text) > max_chars:
                out["truncated"] = True
                break
            out["passages"].append(dict(passage=cno + 1, text=text.strip()))
            used += len(text)
        return out
