"""Ask CGIAR Research: a public MCP server for searching CGIAR publications.

Tools: search_cgiar_publications, get_cgiar_document. Endpoint: /mcp (streamable HTTP).
Landing page at /, readiness at /health.

Environment:
  INDEX_REPO  HF dataset holding the index files (default CGIAR/ask-cgiar-research-index)
  INDEX_NAME  base name of the index files (default cgiar-open)
  DATA_DIR    where index files are downloaded (default /tmp/index)
  HF_TOKEN    token that can read INDEX_REPO and write LOG_REPO (Space secret)
  LOG_REPO    private HF dataset for usage logs; see usage_log.py
  RATE_PER_MIN  tool calls per minute per client address (default 60)
  PILOT_KEY   if set, /mcp requires one of these comma-separated keys, sent as the X-Pilot-Key
              header or the ?key= query parameter (Space secret; unset at public launch)
"""
import hmac, os, threading, time
from urllib.parse import parse_qs
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from pii import Scrubber
from search_core import Index, SearchError
from mcp.server.mcpserver.exceptions import ToolError
from usage_log import UsageLog

INDEX_REPO = os.environ.get("INDEX_REPO", "CGIAR/ask-cgiar-research-index")
INDEX_NAME = os.environ.get("INDEX_NAME", "cgiar-open")
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/index")
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "60"))
PILOT_KEYS = [k.strip() for k in os.environ.get("PILOT_KEY", "").split(",") if k.strip()]
VERSION = "0.1.0"
HERE = Path(__file__).parent

INSTRUCTIONS = """\
Search ~29,600 openly licensed CGIAR research publications (CC-BY, CC-BY-SA, CC0; English,
Spanish, French, Portuguese) and read their passages. Use it to answer agriculture, food
systems, nutrition, climate and livelihoods questions from CGIAR evidence.

How to answer well:
- Run several searches: the question in plain language first, then specific terms, synonyms,
  narrower/broader framings, and es/fr/pt terms for Latin America, Francophone Africa or Brazil.
- Read surrounding passages with get_cgiar_document before relying on a finding.
- Answer only from retrieved passages and cite each claim with title, year and URL.
- Attribute findings a document reports from other studies as secondary evidence.
- Do not imply causality from correlational studies or generalize beyond the study context.
- For policy questions, present evidence, trade-offs and uncertainty rather than recommendations.
- If nothing relevant is found, say so; absence here is not absence of evidence.
- Answers are not official CGIAR positions.
Usage is logged without IP addresses; search queries are stored after automated removal of
personal information. Do not include personal details in queries.
"""

state = dict(index=None, scrubber=None, error=None)
log = UsageLog()
calls = defaultdict(deque)


def load():
    try:
        from huggingface_hub import hf_hub_download
        for ext in (".sqlite", ".bge-m3.f16", ".bge-m3.json"):
            if os.path.exists(f"{DATA_DIR}/{INDEX_NAME}{ext}"):
                continue
            hf_hub_download(INDEX_REPO, f"{INDEX_NAME}{ext}", repo_type="dataset", local_dir=DATA_DIR,
                            token=os.environ.get("HF_TOKEN"))
        index, scrubber = Index(f"{DATA_DIR}/{INDEX_NAME}"), Scrubber()
        index.search("drought tolerant maize adoption", k=3)  # warm up model, page cache and NER
        scrubber.scrub("warm up")
        state["index"], state["scrubber"] = index, scrubber
    except Exception as e:
        state["error"] = type(e).__name__  # details go to the Space logs, not the public /health
        raise


threading.Thread(target=load, daemon=True).start()

mcp = MCPServer(name="ask-cgiar-research", title="Ask CGIAR Research", version=VERSION,
                instructions=INSTRUCTIONS, website_url="https://huggingface.co/spaces/CGIAR/ask-cgiar-research")


def check(ctx):
    """Readiness and per-address rate limit. Addresses are held in memory only, never logged."""
    if state["error"]:
        raise ToolError("The search service failed to start. Please try again later.")
    if not (state["index"] and state["scrubber"]):
        raise ToolError("The search index is still loading after a restart. Please retry in a minute.")
    h = ctx.headers or {}
    addr = (h.get("x-forwarded-for") or "").split(",")[0].strip() or "local"
    q, now = calls[addr], time.time()
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        raise ToolError(f"Rate limit: at most {RATE_PER_MIN} calls per minute. Please slow down.")
    q.append(now)


def client(ctx):
    try:
        ci = ctx.session.client_params.client_info
        return f"{ci.name}/{ci.version}"
    except Exception:
        return None


def record(ctx, tool, started, query=None, **fields):
    """Write a usage event. The query is scrubbed; if scrubbing fails, it is dropped (fail closed)."""
    event = dict(tool=tool, client=client(ctx), latency_ms=int((time.time() - started) * 1000),
                 session=log.session_hash((ctx.headers or {}).get("mcp-session-id")), **fields)
    if query is not None:
        try:
            event["query"] = state["scrubber"].scrub(query[:500])
        except Exception:
            event["query"] = None
            event["query_dropped"] = True
    log.write(event)


def cite(h):
    title = h["title"] + (f" [EN: {h['title_en']}]" if h.get("title_en") and h["title_en"] != h["title"] else "")
    return f"{title} ({h['year'] or 'n.d.'}). {h['url']}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def search_cgiar_publications(
    query: Annotated[str, Field(description="Question or keywords. Natural language works best in hybrid mode.", max_length=500)],
    ctx: Context,
    k: Annotated[int, Field(description="Number of passages to return", ge=1, le=20)] = 8,
    mode: Annotated[Literal["hybrid", "semantic", "keyword"], Field(description=(
        "hybrid (default) fuses keyword and semantic ranking; semantic matches meaning across "
        "en/es/fr/pt; keyword is BM25 for exact names, acronyms, varieties and phrases"))] = "hybrid",
    country: Annotated[str | None, Field(description="Only documents tagged with this country, e.g. 'Zambia'")] = None,
    region: Annotated[str | None, Field(description="Only documents tagged with this region, e.g. 'Western Africa'")] = None,
    year_from: Annotated[int | None, Field(description="Earliest publication year")] = None,
    year_to: Annotated[int | None, Field(description="Latest publication year")] = None,
    language: Annotated[Literal["en", "es", "fr", "pt"] | None, Field(description="Document language (labels are ~1-2% wrong)")] = None,
    full_text_only: Annotated[bool, Field(description="Exclude abstract-only records")] = False,
    per_doc: Annotated[int, Field(description="Maximum passages per document", ge=1, le=5)] = 2,
    raw: Annotated[bool, Field(description="Pass the query to SQLite FTS5 unchanged (AND/OR/NOT, \"phrases\")")] = False,
) -> str:
    """Search CGIAR research publications and return the most relevant passages with citations.

    Each result has the document title, year, URL, license, countries, a doc_id and passage
    number (for get_cgiar_document), and the passage text. Passages that look like reference
    lists are ranked last. Cite results by title, year and URL.
    """
    started = time.time()
    check(ctx)
    filters = {k_: v for k_, v in dict(country=country, region=region, year_from=year_from, year_to=year_to,
                                         language=language, full_text_only=full_text_only or None).items() if v}
    try:
        hits = await anyio.to_thread.run_sync(lambda: state["index"].search(
            query, k=k, mode=mode, country=country, region=region, year_from=year_from, year_to=year_to,
            language=language, full_text_only=full_text_only, per_doc=per_doc, raw=raw))
    except SearchError as e:
        await anyio.to_thread.run_sync(lambda: record(ctx, "search", started, query, mode=mode, filters=filters,
                                                      error=str(e)[:200]))
        raise ToolError(str(e)) from e
    await anyio.to_thread.run_sync(lambda: record(
        ctx, "search", started, query, mode=mode, filters=filters, k=k, n_results=len(hits),
        doc_ids=[h["doc_id"] for h in hits]))
    if not hits:
        return "No matching passages. Try other terms, synonyms, another language, or fewer filters."
    out = []
    for i, h in enumerate(hits, 1):
        out.append(f"=== [{i}] {cite(h)}\n"
                   f"doc_id={h['doc_id']} passage={h['passage']} lang={h['language']} {h['text_type']} "
                   f"license={h['license']} countries={h['countries'] or '-'}\n{h['text']}")
    return "\n\n".join(out)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_cgiar_document(
    doc_id: Annotated[str, Field(description="doc_id from search results", max_length=64)],
    ctx: Context,
    around: Annotated[int | None, Field(description="Show passages around this passage number", ge=1)] = None,
    window: Annotated[int, Field(description="Passages either side of 'around'", ge=0, le=5)] = 2,
    all_passages: Annotated[bool, Field(description="Return the whole document (truncated at ~30k characters)")] = False,
) -> str:
    """Read a CGIAR publication: metadata and abstract, plus passages around a search hit or the whole text.

    Use it to check the context, methods, sample, country and year behind a finding before citing it.
    """
    started = time.time()
    check(ctx)
    try:
        d = await anyio.to_thread.run_sync(lambda: state["index"].document(doc_id, around, window, all_passages))
    except SearchError as e:
        raise ToolError(str(e)) from e
    await anyio.to_thread.run_sync(lambda: record(ctx, "document", started, doc_id=doc_id, around=around,
                                                  all_passages=all_passages or None))
    lines = [f"{k}: {d[k]}" for k in ("title", "title_en", "year", "url", "rights", "language", "text_type",
                                       "resource_type", "countries", "regions", "crops", "livestock", "n_chunks")
             if d.get(k)]
    if d["abstract"]:
        lines.append(f"\nabstract:\n{d['abstract']}")
    for p in d["passages"]:
        lines.append(f"\n--- passage {p['passage']}/{d['n_chunks']} ---\n{p['text']}")
    if d["truncated"]:
        lines.append("\n[... truncated; use 'around' to read further]")
    return "\n".join(lines)


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def home(request: Request):
    return HTMLResponse((HERE / "static" / "index.html").read_text())


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request):
    idx = state["index"]
    return JSONResponse(dict(ready=bool(idx and state["scrubber"]), error=state["error"], version=VERSION,
                             documents=idx.n_docs if idx else None,
                             passages=idx.meta["n"] if idx else None,
                             licenses=idx.licenses if idx else None))


def pilot_gate(inner):
    """During the invitation-only pilot, reject /mcp requests without a valid key (403, not 401,
    so clients don't start an OAuth flow). The landing page and /health stay open."""
    async def gate(scope, receive, send):
        if PILOT_KEYS and scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope["headers"])
            given = headers.get(b"x-pilot-key", b"").decode("latin-1") or \
                (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("key") or [""])[0]
            if not any(hmac.compare_digest(given.encode(), k.encode()) for k in PILOT_KEYS):
                response = JSONResponse({"error": "Ask CGIAR Research is in an invitation-only pilot. "
                                                  "A valid pilot key is required."}, status_code=403)
                return await response(scope, receive, send)
        return await inner(scope, receive, send)
    return gate


app = pilot_gate(mcp.streamable_http_app(host="0.0.0.0"))

if __name__ == "__main__":
    import uvicorn
    # No access log: it would record client addresses and ?key= query strings.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), access_log=False)
