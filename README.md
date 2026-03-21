# main.py - NL2SQL Multi-Agent System

An enterprise-grade Natural Language to SQL system based on multi-agent coordination framework, optimized for Oracle 11g databases.

## 📋 System Overview

### Core Architecture

```
User Question
    ↓
[Retrieve] → Multi-granularity RAG retrieval (table/column/relation three layers)
    ↓
[Plan] → Query plan generation (optional)
    ↓
[Generate] → SQL generation (LLM)
    ↓
[Guard] → Static validation
    ↓
[Execute] → Oracle execution
    ↓
[Repair] → Iterative error correction (loop on failure)
    ↓
Final SQL Result
```

### Key Features

- **Multi-granularity RAG Retrieval**: Table-level, column-level, and relation-level FAISS indexes
- **Iterative Error Correction**: Full history transmission, intelligent temperature scheduling
- **Automatic Code Table Mapping**: CODE fields automatically linked to code tables for Chinese names
- **Multi-table JOIN Intelligence**: Automatic table relationship inference, JOIN hint generation
- **GPU Acceleration**: FAISS index building and vector search support GPU
- **Parallel Embedding**: 32-thread parallel computation with caching support
- **Oracle Dialect Adaptation**: TO_DATE, ROWNUM, Chinese alias double-quoting, etc.

---

## 🚀 Quick Start

### Requirements

```bash
Python 3.8+
Oracle Instant Client 11.2+
Ollama (local LLM service)
CUDA 11.8+ (optional, for GPU acceleration)
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Configure Environment Variables

Create `.env` file or set system environment variables:

```bash
# Ollama Configuration
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_SQL_MODEL=qwen2.5-coder:7b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest

# Oracle Configuration
ORACLE_CLIENT_DIR=/path/to/instantclient_11_2
DB_USER=bjzx
DB_PASSWORD=your_password
DB_HOST=127.0.0.1
DB_PORT=1521
DB_SERVICE_NAME=oral

# Data Dictionary Path
DATA_DICT_PATH=data_dictionary.json
RAG_CACHE_DIR=./rag_cache
MAX_ROWS=500
```

### Start Service

```bash
python main.py
```

Service will start at `http://0.0.0.0:5000`

---

## 📚 Core Modules

### ① Basic Configuration (Lines 1-50)

- Flask application initialization
- Ollama API configuration
- Oracle client initialization
- System parameter setup

### ② JSON Serialization (Lines 51-60)

Handles special types like Decimal and datetime for JSON serialization

### ③ Ollama Calls (Lines 61-200)

**Key Functions:**

- `ollama_embed(texts, batch_size=100, use_cache=True)` - Text vectorization
  - Supports parallel processing (32 threads)
  - Automatic caching mechanism
  - Automatic fallback to SentenceTransformer on Ollama failure

- `ollama_chat(prompt, model, temperature, max_tokens, timeout, num_ctx)` - LLM invocation
  - Global semaphore limits concurrency (prevents Ollama overload)
  - Customizable num_ctx (default 8192)
  - Automatic empty string return on timeout

- `extract_sql(text)` - SQL extraction
  - Removes Markdown code blocks
  - Cleans comments
  - Fixes Chinese aliases (adds double quotes)

### ④ Oracle Connection (Lines 201-250)

- `make_dsn(cfg)` - Build Oracle connection string
- `get_conn_from_session()` - Get connection from Session
- `run_sql(sql, max_rows, timeout)` - Execute SQL and return results

### ⑤ Data Dictionary Loading (Lines 251-280)

- `load_data_dictionary()` - Load table structure from JSON
- Supports main tables and code tables classification
- Automatic in-memory caching

### ⑥ Schema Utility Functions (Lines 281-350)

- `get_table_info(table)` - Get table information
- `get_table_columns(table)` - Get table column information
- `is_code_table(table)` - Check if table is a code table

### ⑦ Code Table Mapping (Lines 351-450)

**Core Logic:**

```
CODE field → Code table → NAME field → Chinese name
```

- `_build_code_map()` - Build CODE→code table mapping
- `get_code_table_hint(main_tables)` - Generate code table JOIN hints
- `expand_with_code_tables(main_tables)` - Auto-expand code tables

### ⑧ Multi-granularity Schema RAG Index (Lines 451-700)

**Class: `MultiGranularitySchemaIndex`**

Three-layer index:
1. **Table-level Index** - Table name + Chinese name + description
2. **Column-level Index** - Table.column + Chinese name + description
3. **Relation Index** - JOIN relationships (same-name key fields)

**Key Methods:**

- `build_entries()` - Build index entries
- `_build_faiss()` - Build FAISS index (GPU support)
- `build_or_load()` - Build or load from cache
- `search(query, topk_table, topk_col, topk_rel)` - Vector search

**Caching Mechanism:**
- Based on data_dictionary.json file size and modification time
- Cache path: `./rag_cache/schema_idx_*.faiss`

### ⑨ Schema Text Generation (Lines 701-750)

- `schema_text_for_tables(tables, max_cols_each)` - Generate table structure text
- `join_hints_text_for_tables(tables)` - Generate JOIN relationship hints

### ⑩ Retrieve (Lines 751-800)

**Function: `retrieve_schema(question, user_selected_tables)`**

Process:
1. Multi-granularity RAG search (table/column/relation)
2. Candidate table deduplication and sorting
3. Auto-detect if code tables are needed
4. Auto-expand code tables

Returns:
```python
{
    "tables_hits": [(score, entry), ...],
    "cols_hits": [(score, entry), ...],
    "rels_hits": [(score, entry), ...],
    "candidate_tables": ["TABLE1", "TABLE2", ...],
    "need_name": bool
}
```

### ⑪ Multi-table Query Core Loop (Lines 801-1000)

**Function: `generate_with_repair()`**

Iterative error correction flow (max 5 rounds):

```
Round 1: temperature=0.0 (greedy)
Round 2: temperature=0.5 (low)
Round 3: temperature=0.7 (medium)
Round 4: temperature=0.85 (high)
Round 5: temperature=0.95 (very high)
```

**Duplicate SQL Detection:**
- Force strategy change on consecutive duplicates
- Inject "change approach" instruction
- Aggressively increase temperature

**History Accumulation Mechanism:**
- Each round failure record: `{"round": i, "sql": "...", "error": "..."}`
- Full history passed to LLM
- LLM analyzes each round error one by one

**Final Fallback Repair:**
- Call `_final_repair()` after 5 failed rounds
- Pass complete schema, all history, banned table list
- temperature=0.3 for deep repair

### ⑫ Single-table Query (Lines 1001-1100)

**Function: `generate_single_table_with_repair()`**

Similar to multi-table flow, but:
- Only operates on single main table
- Optional JOIN with code tables
- Supports simple queries (SELECT *) and complex queries (grouping/aggregation)

### ⑬ Oracle Error Analysis (Lines 1101-1200)

**Function: `_analyze_oracle_error(error_msg, table_name, valid_columns, sql)`**

Supported error types:
- ORA-00904: Invalid identifier (field not found)
- ORA-00933: SQL command not properly ended
- ORA-00979: Not a GROUP BY expression
- ORA-00942: Table or view does not exist
- ORA-01722: Invalid number
- ORA-01843: Not a valid month
- ORA-00907: Missing right parenthesis
- ORA-01747: Invalid column reference

### ⑭ Intelligent Table Name Matching (Lines 1201-1300)

**Function: `find_best_matching_table(user_input)`**

Matching strategy:
1. Exact match (English table name)
2. Exact match (Chinese table name)
3. Similarity match (vector search)

Returns: `(table_name, similarity_score, match_type)`

### ⑮ Flask API Routes (Lines 1301-1600)

#### Single-table Query
```
POST /api/ask
{
    "question": "Query exam records",
    "table_name": "EXAM_RECORD",
    "start_date": "2021-01-01",
    "end_date": "2021-12-31",
    "execute": true
}
```

#### Multi-table Query
```
POST /api/ask_multi
{
    "question": "Statistics of abnormal lab results for domestic and foreign examinees",
    "selected_tables": ["EXAM_RECORD", "LAB_RESULT"],
    "start_date": "2021-01-01",
    "end_date": "2021-12-31",
    "execute": true
}
```

#### Other Endpoints
- `GET /api/tables` - Get all tables list
- `POST /api/table_columns` - Get table column information
- `POST /api/execute_sql` - Execute SQL directly
- `POST /api/recommend_tables` - Recommend related tables
- `GET /api/models` - Available models list
- `GET /api/health` - Health check
- `POST /api/clear_cache` - Clear cache

---

## 🔧 Configuration Tuning

### Performance Optimization

#### 1. Parallel Embedding
```python
# Default 32 threads, adjustable
max_workers = min(32, len(texts_to_process))
```

#### 2. Caching Strategy
```python
# Enable cache (default True)
ollama_embed(texts, use_cache=True)

# Clear cache
POST /api/clear_cache
```

#### 3. GPU Acceleration
```python
# Auto-detect GPU
if hasattr(faiss, 'StandardGpuResources'):
    # Use GPU to build index
```

#### 4. Context Window
```python
# Adjust num_ctx (default 8192)
ollama_chat(prompt, num_ctx=16384)
```

### Error Correction Parameter Tuning

```python
# Modify max error correction rounds
sql = generate_with_repair(..., rounds=7)  # default 5

# Modify temperature schedule
temp_map = {0: 0.0, 1: 0.5, 2: 0.7, 3: 0.85}
```

### Retrieval Parameter Tuning

```python
# Modify RAG retrieval count
tables, cols, rels = SCHEMA_INDEX.search(
    query,
    topk_table=6,   # default 6
    topk_col=12,    # default 12
    topk_rel=10     # default 10
)
```

---

## 📊 Data Dictionary Format

```json
{
  "main_tables": {
    "EXAM_RECORD": {
      "table_cn": "Exam Records",
      "is_code_table": false,
      "short_description": "...",
      "detail_description": "...",
      "columns": [
        {
          "name": "EXAM_NO",
          "cn": "Exam Number",
          "data_type": "VARCHAR2",
          "type_str": "VARCHAR2(50)",
          "full_description": "..."
        }
      ]
    }
  },
  "code_tables": {
    "AA_BM_MEDICAL_DEPT": {
      "table_cn": "Department Code Table",
      "is_code_table": true,
      "columns": [
        {
          "name": "DEPT_CODE",
          "cn": "Department Code"
        },
        {
          "name": "DEPT_NAME",
          "cn": "Department Name"
        }
      ]
    }
  }
}
```

---

## 🐛 Frequently Asked Questions

### Q1: Ollama Connection Failed
```
❌ LLM call failed: Connection refused
```
**Solution:** Ensure Ollama service is running
```bash
ollama serve
```

### Q2: Oracle Connection Failed
```
❌ ORA-12514: TNS:listener does not currently know of service
```
**Solution:** Check SERVICE_NAME and listener configuration

### Q3: FAISS Index Building Failed
```
⚠️ GPU build failed, using CPU
```
**Solution:** Auto-fallback to CPU, or check CUDA version

### Q4: Generated SQL Still Has Errors
```
⚠️ Still failed after 5 rounds, entering final fallback repair
```
**Solution:**
- Check if data dictionary is complete
- Increase error correction rounds
- Manually adjust temperature parameters

### Q5: Chinese Alias Reports ORA-00923
```
❌ ORA-00923: FROM keyword not found where expected
```
**Solution:** Auto-fix is enabled, check for special characters

---

## 📈 Performance Metrics

### Typical Response Times

| Operation | Time |
|-----------|------|
| Embedding (100 texts) | 2-5s |
| FAISS search | 50-100ms |
| Plan generation | 5-10s |
| SQL generation (round 1) | 7-15s |
| SQL execution | 1-5s |
| **Total time (single-table)** | **15-30s** |
| **Total time (multi-table)** | **30-60s** |

### Resource Usage

| Resource | Usage |
|----------|-------|
| Memory (startup) | 2-3GB |
| Memory (running) | 4-6GB |
| GPU memory (optional) | 2-4GB |
| Cache size | 100-500MB |

---

## 🔐 Security

### SQL Injection Prevention

- Only SELECT statements allowed
- DML/DDL keywords prohibited
- Parameterized queries (Oracle bind variables)

### Permission Management

- Database user permissions minimized
- Only SELECT permission granted
- No table structure modification allowed

### Logging

All SQL executions are logged to stdout for audit purposes

---

## 📝 Development Guide

### Adding New Error Types

Edit `_analyze_oracle_error()` function:

```python
elif "ORA-XXXXX" in error_msg:
    return "Error description and fix suggestion"
```

### Customizing Temperature Schedule

Edit `temp_map` in `generate_with_repair()`:

```python
temp_map = {0: 0.0, 1: 0.3, 2: 0.6, 3: 0.9}
```

### Extending Code Table Mapping

Edit matching rules in `_build_code_map()`:

```python
code_col_candidates = []
for cn in ct_col_names.keys():
    if cn.endswith("_CODE") or ...:  # Add new rules
        code_col_candidates.append(cn)
```

---

## 📞 Support

- Bug Reports: Submit Issue
- Feature Requests: Submit PR
- Documentation Feedback: Edit README

---

## 📄 License

MIT License

---

## 🙏 Acknowledgments

- Ollama - Local LLM service
- FAISS - Vector search library
- SentenceTransformers - Text embedding model
- Oracle - Enterprise database

---

**Last Updated: March 2026**
