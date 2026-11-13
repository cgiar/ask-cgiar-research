"""Build the SQLite FTS5 search index used by the ask-cgiar-research skill.

Input:  agrifood-research-corpus layout (<src>/<org>/<text-type>/<license>/*.parquet)
Output: <out>.sqlite with
        docs(gardian_id, title, title_en, abstract, year, url, rights, license_category,
             language, text_type, resource_type, countries, regions, crops, livestock, n_chunks, first_rowid)
        chunks  FTS5(text, title, gardian_id UNINDEXED, chunk_no UNINDEXED)

Usage: build_ask_index.py build_agrifood ask_index/cgiar.sqlite
"""
import glob, os, re, sqlite3, sys

import pyarrow.parquet as pq

CHUNK_CHARS = 1500
MIN_CHARS = 200    # merge a short tail chunk into the previous one
COLS = ["gardian_id", "title", "ai_en_title", "description", "ai_en_description", "release_year",
        "resource_url", "source", "rights", "license_category", "language", "text_type",
        "resource_type", "geography", "crops", "livestock", "content"]


def chunks(text):
    """Pack paragraphs into ~CHUNK_CHARS chunks; split over-long paragraphs on sentences."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]
    out, cur = [], ""
    for p in paras:
        pieces = [p] if len(p) <= CHUNK_CHARS else re.split(r"(?<=[.!?])\s+", p)
        for s in pieces:
            if cur and len(cur) + len(s) + 2 > CHUNK_CHARS:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n\n{s}" if cur else s
    if cur:
        if out and len(cur) < MIN_CHARS:
            out[-1] += "\n\n" + cur
        else:
            out.append(cur)
    return out


def main():
    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    db = sqlite3.connect(out)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE docs(gardian_id TEXT PRIMARY KEY, title TEXT, title_en TEXT, abstract TEXT,
            year INTEGER, url TEXT, rights TEXT, license_category TEXT, language TEXT,
            text_type TEXT, resource_type TEXT, countries TEXT, regions TEXT, crops TEXT,
            livestock TEXT, n_chunks INTEGER, first_rowid INTEGER);
        CREATE VIRTUAL TABLE chunks USING fts5(text, title, gardian_id UNINDEXED, chunk_no UNINDEXED,
            tokenize='porter unicode61 remove_diacritics 2');
    """)
    n_docs = n_chunks = 0
    for path in sorted(glob.glob(f"{src}/*/*/*/*.parquet")):
        for r in pq.read_table(path, columns=COLS).to_pylist():
            # abstract-only records carry the abstract in content; keep description as the abstract
            parts = chunks(r["content"])
            title = r["title"] or ""
            title_en = r["ai_en_title"] or ""
            geo = r["geography"] or {}
            year = int(r["release_year"]) if (r["release_year"] or "").isdigit() else None
            db.execute("INSERT OR IGNORE INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["gardian_id"], title, title_en, r["description"] or r["ai_en_description"] or "",
                year, r["resource_url"] or r["source"], r["rights"], r["license_category"],
                r["language"], r["text_type"], r["resource_type"],
                "; ".join(geo.get("countries") or []), "; ".join(geo.get("regions") or []),
                "; ".join((r["crops"] or {}).get("values") or []),
                "; ".join((r["livestock"] or {}).get("values") or []), len(parts), n_chunks + 1))
            db.executemany("INSERT INTO chunks(rowid, text, title, gardian_id, chunk_no) VALUES (?,?,?,?,?)",
                           [(n_chunks + 1 + i, c, f"{title} {title_en}".strip(), r["gardian_id"], i)
                            for i, c in enumerate(parts)])
            n_docs += 1
            n_chunks += len(parts)
        db.commit()
        print(f"{n_docs:7d} docs {n_chunks:9d} chunks  {path}", flush=True)
    db.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
    db.execute("CREATE INDEX docs_year ON docs(year)")
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"done: {n_docs} docs, {n_chunks} chunks, {os.path.getsize(out) / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
