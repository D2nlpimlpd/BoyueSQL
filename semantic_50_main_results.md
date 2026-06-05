# 50-Question Semantic Text-to-SQL Evaluation

- Time: 2026-06-05 01:41:59
- Benchmark: `E:\text2sql\semantic_50_benchmark.json`
- Cases: 50 (single=25, multi=25)
- Gold-table hints: False

## Summary

| System | Model | Cases | Exec | Result exact | Semantic pass | Avg score | Avg time |
|---|---|---:|---:|---:|---:|---:|---:|
| coordsql_with_final | qwen3-vl:8b | 50 | 100.00% | 100.00% | 100.00% | 1.000 | 0.22s |

## Single vs Multi

| System | Model | Type | Exec | Result exact | Semantic pass | Avg score |
|---|---|---|---:|---:|---:|---:|
| coordsql_with_final | qwen3-vl:8b | multi | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_with_final | qwen3-vl:8b | single | 100.00% | 100.00% | 100.00% | 1.000 |

## Failure Types

### coordsql_with_final / qwen3-vl:8b
- no failures
