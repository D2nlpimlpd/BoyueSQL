# CoordSQL / BoyueSQL Backend

CoordSQL, also referred to as BoyueSQL in the current paper draft, is an
enterprise NL2SQL backend for Oracle 11g. The current implementation uses a
modified RagAnything/LightRAG schema knowledge graph, dictionary-grounded schema
retrieval, online catalog validation, iterative SQL repair, and a local
`qwen3-vl:8b` model served by Ollama.

This repository keeps the backend code, the database dictionary artifacts needed
to reproduce the schema-KG, and the 50-question semantic evaluation scripts and
results. Runtime caches, Oracle clients, database passwords, and local logs are
intentionally excluded from Git.

## Included Backend Files

- `main.py`: Flask backend and NL2SQL orchestration entry point.
- `raganything_schema_retriever.py`: BoyueSQL schema-KG retriever wrapper.
- `third_party/raganything-1.3.1/raganything/sql_dictionary.py`: customized
  RagAnything dictionary adapter for table, column, and relationship evidence.
- `data_dictionary.json`, `table_names.json`, `table_descriptions.json`: schema
  dictionary artifacts derived from the enterprise database dictionary.
- `semantic_50_eval.py`: 25 single-table and 25 multi-table semantic benchmark.
- `run_sota_models_only.py`: SOTA-style baseline runner.
- `xiaorong.py`: ablation runner.
- `semantic_50_*.md`, `semantic_50_benchmark.json`: reproduced benchmark
  questions and result summaries.
- `RAGANYTHING_SCHEMA_KG_PROOF.md`: implementation-level proof notes for the
  schema-KG retrieval method.
- `templates/`, `static/`: lightweight Flask UI assets.

## Safety

Do not commit a real database password. Copy `.env.example` to `.env` locally
and fill in private credentials only on the deployment machine.

```bash
cp .env.example .env
```

GitHub rejects password-based Git authentication. If `git push` asks for
credentials, sign in through Git Credential Manager or use a GitHub personal
access token with repository write permission.

## Setup

```bash
conda activate text2sql
pip install -r requirements.txt
pip install -r requirements-raganything.txt
ollama pull qwen3-vl:8b
```

Install Oracle Instant Client 11.2 locally and set `ORACLE_CLIENT_DIR` in
`.env`. The default local model is intentionally `qwen3-vl:8b`; keep
`STRICT_QWEN3_VL_ONLY=1` when reproducing the reported experiments.

## Run Backend

```bash
conda activate text2sql
python main.py
```

The Flask service starts on port `5000` by default.

Common endpoints:

- `GET /api/health`: service and configuration health check.
- `GET /api/tables`: list dictionary tables.
- `POST /api/ask`: single-table NL2SQL.
- `POST /api/ask_multi`: multi-table NL2SQL.
- `POST /api/execute_sql`: execute a supplied SQL statement.
- `POST /api/recommend_tables`: retrieve schema-KG candidate tables.

## Rebuild Schema-KG

The current backend no longer relies on the old FAISS-only RAG cache. The
retriever builds a persistent RagAnything/LightRAG-style schema-KG from
`data_dictionary.json`. To force a clean rebuild, remove only local runtime
storage and restart the backend:

```bash
rm -rf raganything_storage rag_cache
python main.py
```

Do not commit the regenerated storage directory; it is machine-specific runtime
state and is ignored by `.gitignore`.

## Reproduce Evaluation

```bash
conda activate text2sql
python semantic_50_eval.py
python run_sota_models_only.py
python xiaorong.py
```

The tracked result summaries document the current 50-question semantic
evaluation, ablation study, and SOTA-style baseline comparison.
