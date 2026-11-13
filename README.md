---
title: Ask CGIAR Research
emoji: 🌾
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
pinned: true
license: apache-2.0
short_description: MCP server to search and cite CGIAR research publications
tags:
  - mcp
  - mcp-server
  - agriculture
  - food-security
  - retrieval
  - cgiar
---

# Ask CGIAR Research

A public [Model Context Protocol](https://modelcontextprotocol.io) server that lets AI assistants
search **29,556 openly licensed CGIAR research publications** (CC-BY, CC-BY-SA, CC0; English, Spanish,
French, Portuguese) and answer agrifood questions with citations.

**Server URL:** `https://cgiar-ask-cgiar-research.hf.space/mcp` (streamable HTTP, no sign-in)

**Code:** the main copy is the [Hugging Face Space](https://huggingface.co/spaces/CGIAR/ask-cgiar-research),
which runs the service. [github.com/cgiar/ask-cgiar-research](https://github.com/cgiar/ask-cgiar-research) is a
mirror for browsing, issues and installing the plugin.

> **Invitation-only pilot.** Until the public launch, the server requires a pilot key from the CGIAR team
> (`X-Pilot-Key` header or `?key=` query parameter).

## Tools

| Tool | What it does |
|---|---|
| `search_cgiar_publications` | Hybrid search (BM25 keyword + multilingual `BAAI/bge-m3` embeddings, reciprocal rank fusion) over ~1.0M passages, with country, region, year, language and full-text filters |
| `get_cgiar_document` | Metadata, abstract and passages around a hit, or the whole document |

## Connect

**Claude Code, plugin with search guidance (recommended):**

```bash
claude plugin marketplace add https://huggingface.co/spaces/CGIAR/ask-cgiar-research.git
claude plugin install ask-cgiar-research@cgiar
```

**Claude Code, server only:**

```bash
claude mcp add --transport http cgiar-research https://cgiar-ask-cgiar-research.hf.space/mcp
```

**Claude.ai / Claude Desktop:**
1. Go to Customize → Connectors → Add custom connector, paste the server URL, and choose no sign-in.
2. Optionally upload [`plugin/ask-cgiar-research-skill.zip`](plugin/ask-cgiar-research-skill.zip) under Customize → Skills.

**Other MCP clients:** add the URL as a remote streamable-HTTP MCP server.

## Data

The index is built from the CGIAR agrifood research corpus (GARDIAN-indexed CGIAR publications),
restricted to documents whose license allows redistribution: CC-BY 27,771, CC-BY-SA 1,630 and CC0 155.
Of these, 24,779 are full text and 4,777 are abstracts. The text was extracted from PDFs, and tables and
figure text were removed. Each result carries its document's license and source URL; reuse must follow
that license.

Answers generated with this tool are **not official CGIAR positions**. Coverage is uneven: when nothing
is found, that is not evidence that no CGIAR research exists on the topic.

## Privacy and usage data

For reporting, each tool call is logged with:
- the time and the tool;
- the search query after automated removal of personal information (names, emails, phone numbers,
  URLs, ID numbers, precise coordinates);
- the filters, the documents returned and the response time;
- the client application name;
- a session id that rotates daily.

IP addresses and raw queries are not stored. Logs are kept in a private CGIAR dataset for 24 months,
then deleted automatically. Automated removal is imperfect, so please don't put personal details in
questions.

## Repository layout

```
app.py            MCP server (tools, landing page, /health)
search_core.py    hybrid search over SQLite FTS5 + dense vectors
pii.py            query scrubbing before logging
usage_log.py      scrubbed usage logs -> private HF dataset, 24-month retention
static/           landing page
build/            scripts that build the index from the corpus
plugin/           Claude Code plugin + claude.ai skill
```

**Configuration** (Space variables and secrets):

| Name | Purpose |
|---|---|
| `HF_TOKEN` | secret, fine-grained: read `INDEX_REPO`, write `LOG_REPO` |
| `INDEX_REPO` | default `CGIAR/ask-cgiar-research-index` |
| `LOG_REPO` | e.g. `CGIAR/ask-cgiar-research-logs` (private) |
| `RATE_PER_MIN` | default 60 |
| `PILOT_KEY` | secret; comma-separated pilot keys. When set, `/mcp` requires one. Remove at public launch |

## Contact

Questions and feedback: Jawoo Koo, via the [IFPRI contact form](https://www.ifpri.org/contact-us/).

## Credits

Owned and operated by **CGIAR**. Developed under the **GAIA project**, funded by the **Gates Foundation**
and **UK International Development (FCDO)**, with the support of the **CGIAR Digital Transformation
Accelerator** program. Code licensed under Apache-2.0.
