"""Cut a license-filtered subset out of the full ask-cgiar-research index, reusing its embeddings.

Input:  <full>.sqlite + <full>.bge-m3.f16/.json  (from build_ask_index.py / build_ask_embeddings.py)
Output: <out>.sqlite + <out>.bge-m3.f16/.json with contiguous rowids, so row i of the vectors
        is chunk rowid i+1, exactly as in the full index.

Usage: build_subset.py ../../ask_index/cgiar.sqlite data/cgiar-open.sqlite \
           --licenses permissive,sharealike
"""
import argparse, json, os, sqlite3

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("full")
    ap.add_argument("out")
    ap.add_argument("--licenses", default="permissive,sharealike")
    a = ap.parse_args()
    cats = a.licenses.split(",")
    fbase, obase = os.path.splitext(a.full)[0], os.path.splitext(a.out)[0]
    fmeta = json.load(open(f"{fbase}.bge-m3.json"))
    assert fmeta["done"] == fmeta["n"], "full embeddings are incomplete"
    fvec = np.memmap(f"{fbase}.bge-m3.f16", dtype=np.float16, mode="r", shape=(fmeta["n"], fmeta["dim"]))

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    for p in (a.out, f"{obase}.bge-m3.f16", f"{obase}.bge-m3.json"):
        if os.path.exists(p):
            os.remove(p)
    src = sqlite3.connect(f"file:{a.full}?mode=ro", uri=True)
    db = sqlite3.connect(a.out)
    schema = src.execute("SELECT sql FROM sqlite_master WHERE name='docs'").fetchone()[0]
    db.executescript(f"""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        {schema};
        CREATE VIRTUAL TABLE chunks USING fts5(text, title, gardian_id UNINDEXED, chunk_no UNINDEXED,
            tokenize='porter unicode61 remove_diacritics 2');
    """)
    docs = src.execute(f"SELECT * FROM docs WHERE license_category IN ({','.join('?' * len(cats))}) "
                       "ORDER BY first_rowid", cats).fetchall()
    cols = [d[0] for d in src.execute("SELECT * FROM docs LIMIT 0").description]
    i_first, i_n = cols.index("first_rowid"), cols.index("n_chunks")
    n = sum(d[i_n] for d in docs)
    ovec = np.memmap(f"{obase}.bge-m3.f16", dtype=np.float16, mode="w+", shape=(n, fmeta["dim"]))
    nxt = 1
    for d in docs:
        old, k = d[i_first], d[i_n]
        rows = src.execute("SELECT text, title, gardian_id, chunk_no FROM chunks WHERE rowid BETWEEN ? AND ? "
                           "ORDER BY rowid", (old, old + k - 1)).fetchall()
        assert len(rows) == k and all(r[2] == d[0] for r in rows)
        db.executemany("INSERT INTO chunks(rowid, text, title, gardian_id, chunk_no) VALUES (?,?,?,?,?)",
                       [(nxt + j, *r) for j, r in enumerate(rows)])
        ovec[nxt - 1:nxt - 1 + k] = fvec[old - 1:old - 1 + k]
        d = list(d)
        d[i_first] = nxt
        db.execute(f"INSERT INTO docs VALUES ({','.join('?' * len(d))})", d)
        nxt += k
    ovec.flush()
    db.commit()
    db.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
    db.execute("CREATE INDEX docs_year ON docs(year)")
    db.commit()
    db.execute("VACUUM")
    db.close()
    json.dump(dict(fmeta, n=n, done=n, licenses=cats, source=os.path.basename(a.full)),
              open(f"{obase}.bge-m3.json", "w"))
    print(f"{len(docs)} docs, {n} passages -> {a.out} ({os.path.getsize(a.out) / 1e9:.2f} GB) "
          f"+ vectors ({os.path.getsize(f'{obase}.bge-m3.f16') / 1e9:.2f} GB)")


if __name__ == "__main__":
    main()
