"""Embed every passage of the ask-cgiar-research index for semantic search.

Input:  <index>.sqlite from build_ask_index.py
Output: <index>.bge-m3.f16   float16 memmap [n_chunks, 1024], row i = chunks rowid i+1,
                             L2-normalized "title\\npassage" embeddings
        <index>.bge-m3.json  {model, dim, n, done}; `done` makes the build resumable

Usage: build_ask_embeddings.py ask_index/cgiar.sqlite [--block 20000] [--batch 128]
"""
import argparse, json, os, sqlite3, time

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

MODEL, DIM = "BAAI/bge-m3", 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--block", type=int, default=20000)
    ap.add_argument("--batch", type=int, default=128)
    a = ap.parse_args()

    base = os.path.splitext(a.index)[0]
    vec_path, meta_path = f"{base}.bge-m3.f16", f"{base}.bge-m3.json"
    db = sqlite3.connect(f"file:{a.index}?mode=ro", uri=True)
    n = db.execute("SELECT max(rowid) FROM chunks").fetchone()[0]
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else dict(model=MODEL, dim=DIM, n=n, done=0)
    assert meta["n"] == n, "index changed since embeddings were started; delete the .f16/.json files"
    vecs = np.memmap(vec_path, dtype=np.float16, mode="r+" if os.path.exists(vec_path) else "w+", shape=(n, DIM))

    model = SentenceTransformer(MODEL, device="cuda", model_kwargs={"torch_dtype": torch.float16})
    model.max_seq_length = 512
    t0, start = time.time(), meta["done"]
    while meta["done"] < n:
        lo = meta["done"]
        hi = min(lo + a.block, n)
        rows = db.execute("SELECT rowid, title, text FROM chunks WHERE rowid BETWEEN ? AND ? ORDER BY rowid",
                          (lo + 1, hi)).fetchall()
        assert [r[0] for r in rows] == list(range(lo + 1, hi + 1)), "rowids are not contiguous"
        vecs[lo:hi] = model.encode([f"{t}\n{x}" for _, t, x in rows], batch_size=a.batch,
                                   normalize_embeddings=True, convert_to_numpy=True)
        vecs.flush()
        meta["done"] = hi
        json.dump(meta, open(meta_path, "w"))
        rate = (hi - start) / (time.time() - t0)
        print(f"{hi:8d}/{n}  {rate:.0f}/s  eta {(n - hi) / rate / 60:.0f} min", flush=True)
    print("done")


if __name__ == "__main__":
    main()
