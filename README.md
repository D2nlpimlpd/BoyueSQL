# CoordSQL / BoyueSQL

Enterprise natural-language-to-SQL backend for large Oracle schemas, powered by
a customized RagAnything/LightRAG schema knowledge graph and a local
`qwen3-vl:8b` model.

CoordSQL, named BoyueSQL in the current paper draft, targets production
enterprise databases where the schema is large, domain terminology is highly
specialized, and generated SQL must obey deployment-specific dialect rules. The
current backend is designed for an Oracle 11g health-examination database with
292 dictionary tables, but the core workflow is database-adapter friendly:
replace the catalog query, error mapping, and dictionary loader to move to
another relational database.

## Highlights

- RagAnything-backed schema knowledge graph instead of the earlier FAISS-only
  RAG cache.
- Dictionary-grounded retrieval over table entities, full column markers,
  dictionary keywords, and relation edges.
- Strict local-model execution with `qwen3-vl:8b` through Ollama.
- Oracle-aware SQL generation, static validation, execution feedback, and repair.
- 50-question semantic evaluation with 25 single-table and 25 multi-table cases.
- No committed database password, Oracle client, runtime cache, or local logs.

## Current Evaluation Snapshot

The tracked evaluation summaries were produced on 2026-06-05 with the benchmark
in `semantic_50_benchmark.json`.

| System | Model | Cases | Execution | Result exact match | Semantic pass |
|---|---|---:|---:|---:|---:|
| CoordSQL with final repair | `qwen3-vl:8b` | 50 | 100.00% | 100.00% | 100.00% |
| CoordSQL without final repair | `qwen3-vl:8b` | 50 | 100.00% | 100.00% | 100.00% |
| Schema-only executable fallback | `qwen3-vl:8b` | 50 | 100.00% | 0.00% | 0.00% |
| DIN-SQL-style prompt baseline | `qwen2.5-coder:7b` | 50 | 42.00% | 14.00% | 14.00% |
| Direct prompt baseline | `qwen2.5-coder:7b` | 50 | 28.00% | 6.00% | 6.00% |
| Direct prompt baseline | `sqlcoder:7b` | 50 | 0.00% | 0.00% | 0.00% |

The schema-only fallback result is intentionally included: it shows that merely
returning executable SQL is not enough. The main metric used by this project is
semantic correctness, measured by result-set equality against gold SQL plus
identifier coverage checks.

Detailed summaries:

- `semantic_50_main_results.md`
- `semantic_50_ablation_results.md`
- `semantic_50_sota_qwen25_merged_results.md`
- `semantic_50_sqlcoder_results.md`

## Architecture

```text
User question
    |
    v
RagAnything schema-KG Retriever
    |  table entities, column markers, keyword evidence, relation edges
    v
Optional Planner
    |  deterministic empty plan by default in the qwen3-vl:8b setting
    v
SQL Generator
    |  Oracle 11g rules, dictionary context, retrieved schema evidence
    v
Guard
    |  SELECT-only checks, identifier validation, catalog consistency
    v
Executor
    |  Oracle runtime feedback
    v
Repairer
    |  classified errors, forbidden SQL set, dictionary and catalog evidence
    v
Semantic Evidence Check
    |  execution, exact result match, table/column coverage, no NULL placeholder
    v
Final SQL and result
```

The customized RagAnything logic is implemented mainly in
`third_party/raganything-1.3.1/raganything/sql_dictionary.py`, with the backend
wrapper in `raganything_schema_retriever.py`.

## Repository Layout

| Path | Purpose |
|---|---|
| `main.py` | Flask backend and NL2SQL orchestration entry point. |
| `raganything_schema_retriever.py` | Schema-KG retrieval wrapper used by the backend. |
| `third_party/raganything-1.3.1/` | Vendored RagAnything source with the project-specific dictionary adapter. |
| `data_dictionary.json` | Main schema dictionary consumed by the backend and retriever. |
| `table_names.json`, `table_descriptions.json` | Auxiliary table metadata. |
| `semantic_50_benchmark.json` | 50-question benchmark, 25 single-table and 25 multi-table cases. |
| `semantic_50_eval.py` | Main semantic evaluation runner. |
| `run_sota_models_only.py` | SOTA-style prompt baseline runner. |
| `xiaorong.py` | Ablation experiment runner. |
| `RAGANYTHING_SCHEMA_KG_PROOF.md` | Implementation-level proof notes for the schema-KG method. |
| `templates/`, `static/` | Lightweight Flask UI. |
| `oracle_qa_app/` | Existing Flutter client kept from the original repository. |

## Requirements

- Windows or Linux with Python 3.10 recommended.
- Conda environment named `text2sql`, or an equivalent Python environment.
- Oracle Instant Client 11.2 for the current Oracle deployment.
- Ollama running locally.
- Local model: `qwen3-vl:8b`.
- Access to the target Oracle database for execution and evaluation.

The current repository does not include private database contents or a database
password.

## Installation

```bash
conda activate text2sql
pip install -r requirements.txt
pip install -r requirements-raganything.txt
ollama pull qwen3-vl:8b
```

Start Ollama before running the backend:

```bash
ollama serve
```

If Ollama is already installed as a background service, the command above is not
required.

## Configuration

Copy the template and fill in local-only credentials:

```bash
cp .env.example .env
```

Important variables:

| Variable | Meaning | Default |
|---|---|---|
| `OLLAMA_BASE_URL` | Ollama API endpoint. | `http://localhost:11434` |
| `OLLAMA_SQL_MODEL` | SQL generation model. | `qwen3-vl:8b` |
| `OLLAMA_EMBEDDING_MODEL` | Embedding model used by the backend fallback path. | `qwen3-vl:8b` |
| `STRICT_QWEN3_VL_ONLY` | Keep the backend restricted to qwen3-vl:8b for reproducibility. | `1` |
| `ORACLE_CLIENT_DIR` | Oracle Instant Client directory. | `F:\oracle\instantclient_11_2` |
| `DB_USER` | Oracle user. | `bjzx` |
| `DB_PASSWORD` | Oracle password. Must be set locally. | empty |
| `DB_HOST` | Oracle host. | `127.0.0.1` |
| `DB_PORT` | Oracle listener port. | `1521` |
| `DB_SERVICE_NAME` | Oracle service name. | `oral` |
| `DATA_DICT_PATH` | Dictionary file path. | `data_dictionary.json` |
| `RAGANYTHING_WORKING_DIR` | Local runtime schema-KG storage. | `./raganything_storage` |
| `MAX_ROWS` | Maximum returned rows per request. | `500` |

The backend reads environment variables. To run with `.env` directly, use
`python-dotenv`:

```bash
python -m dotenv run -- python main.py
```

PowerShell users can also set variables explicitly:

```powershell
$env:DB_PASSWORD="your_local_password"
$env:ORACLE_CLIENT_DIR="F:\oracle\instantclient_11_2"
python main.py
```

Never commit `.env`.

## Run the Backend

```bash
conda activate text2sql
python -m dotenv run -- python main.py
```

The Flask service starts on port `5000` by default.

Health check:

```bash
curl http://127.0.0.1:5000/api/health
```

## API Overview

### List Tables

```bash
curl http://127.0.0.1:5000/api/tables
```

### Single-Table NL2SQL

```bash
curl -X POST http://127.0.0.1:5000/api/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Count health examination records by department.",
    "table_name": "EXAM_RECORD",
    "start_date": "2021-01-01",
    "end_date": "2026-03-17",
    "execute": true
  }'
```

### Multi-Table NL2SQL

```bash
curl -X POST http://127.0.0.1:5000/api/ask_multi \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Count abnormal laboratory results by item.",
    "selected_tables": ["EXAM_RECORD", "LAB_RESULT"],
    "start_date": "2021-01-01",
    "end_date": "2026-03-17",
    "execute": true
  }'
```

### Execute SQL

```bash
curl -X POST http://127.0.0.1:5000/api/execute_sql \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT * FROM EXAM_RECORD WHERE ROWNUM <= 5"}'
```

## Rebuild the Schema Knowledge Graph

The current backend should use the RagAnything schema-KG path rather than the
old FAISS-only RAG records. To force a clean local rebuild, delete runtime
storage and restart the backend.

Git Bash:

```bash
rm -rf raganything_storage rag_cache
python -m dotenv run -- python main.py
```

PowerShell:

```powershell
Remove-Item -Recurse -Force .\raganything_storage, .\rag_cache -ErrorAction SilentlyContinue
python -m dotenv run -- python main.py
```

Do not commit the regenerated directories. They are ignored by `.gitignore`
because they are machine-specific runtime state.

## Reproduce the 50-Question Evaluation

Run the main system:

```bash
conda activate text2sql
python -m dotenv run -- python semantic_50_eval.py
```

Run SOTA-style prompt baselines:

```bash
python -m dotenv run -- python run_sota_models_only.py
```

Run ablations:

```bash
python -m dotenv run -- python xiaorong.py
```

The benchmark uses stronger semantic evidence than execution success alone:

- SQL must execute.
- Result rows must exactly match the gold SQL result.
- Required tables and columns must be covered.
- `NULL AS` placeholders are rejected.

## Development Checks

Before committing backend changes, run a syntax check:

```bash
python -m py_compile main.py \
  raganything_schema_retriever.py \
  third_party/raganything-1.3.1/raganything/sql_dictionary.py \
  semantic_50_eval.py \
  run_sota_models_only.py \
  xiaorong.py
```

Check that no private password is staged:

```bash
rg -n --hidden -S "DB_PASSWORD|your_real_password|OPENAI_API_KEY|secret_key" .
git status --short
```

## Common Issues

### Ollama connection refused

Make sure Ollama is running and that `OLLAMA_BASE_URL` points to the correct
host:

```bash
ollama serve
curl http://127.0.0.1:11434/api/tags
```

### `qwen3-vl:8b` is missing

```bash
ollama pull qwen3-vl:8b
```

### Oracle client initialization fails

Check `ORACLE_CLIENT_DIR`. On Windows, it should point to the directory that
contains Oracle Instant Client DLL files, for example:

```text
F:\oracle\instantclient_11_2
```

### Database login fails

Confirm `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, and
`DB_SERVICE_NAME`. The repository intentionally does not store a password.

### Generated schema-KG seems stale

Delete `raganything_storage` and `rag_cache`, then restart the backend. This
forces the retriever to rebuild local runtime state from `data_dictionary.json`.

## Security Notes

- Only local placeholders are tracked in `.env.example`.
- `.env`, logs, runtime caches, Oracle clients, and generated indexes are
  ignored.
- The backend is designed for read-only SQL generation. Deployment database
  users should have the minimum required privileges, preferably SELECT-only.
- Do not publish private database rows or production credentials in issues,
  commits, screenshots, or logs.

## Git Workflow

```bash
git status
git add .
git commit -m "Describe your backend change"
git push origin master
```

If GitHub asks for authentication, use Git Credential Manager or a GitHub
personal access token. Do not write tokens into repository files.

## Paper and Method Notes

For the mathematical rationale behind the customized RagAnything schema-KG,
see `RAGANYTHING_SCHEMA_KG_PROOF.md`. The key idea is that every table chunk
preserves complete dictionary column evidence, while relation-aware retrieval
and lexical keyword evidence reduce schema hallucination before SQL generation.

## Status

This repository is a research prototype for enterprise NL2SQL experiments. The
backend is actively changing with the paper implementation, so pin the commit
hash when reproducing published results.
