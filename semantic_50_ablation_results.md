# 50-Question Semantic Text-to-SQL Evaluation

- Time: 2026-06-05 02:24:29
- Benchmark: `E:\text2sql\semantic_50_benchmark.json`
- Cases: 50 (single=25, multi=25)
- Gold-table hints: False

## Summary

| System | Model | Cases | Exec | Result exact | Semantic pass | Avg score | Avg time |
|---|---|---:|---:|---:|---:|---:|---:|
| coordsql_with_final | qwen3-vl:8b | 50 | 100.00% | 100.00% | 100.00% | 1.000 | 5.73s |
| coordsql_no_final | qwen3-vl:8b | 50 | 100.00% | 100.00% | 100.00% | 1.000 | 0.12s |
| coordsql_schema_only | qwen3-vl:8b | 50 | 100.00% | 0.00% | 0.00% | 0.544 | 0.07s |

## Single vs Multi

| System | Model | Type | Exec | Result exact | Semantic pass | Avg score |
|---|---|---|---:|---:|---:|---:|
| coordsql_with_final | qwen3-vl:8b | multi | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_with_final | qwen3-vl:8b | single | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_no_final | qwen3-vl:8b | multi | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_no_final | qwen3-vl:8b | single | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_schema_only | qwen3-vl:8b | multi | 100.00% | 0.00% | 0.00% | 0.490 |
| coordsql_schema_only | qwen3-vl:8b | single | 100.00% | 0.00% | 0.00% | 0.598 |

## Failure Types

### coordsql_with_final / qwen3-vl:8b
- no failures

### coordsql_no_final / qwen3-vl:8b
- no failures

### coordsql_schema_only / qwen3-vl:8b
- result_mismatch: 50
