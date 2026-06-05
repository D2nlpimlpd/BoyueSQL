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
| direct_prompt | qwen2.5-coder:7b | 50 | 28.00% | 6.00% | 6.00% | 0.521 | 4.34s |
| dail_sql_style | qwen2.5-coder:7b | 50 | 14.00% | 2.00% | 2.00% | 0.473 | 4.20s |
| din_sql_style | qwen2.5-coder:7b | 50 | 42.00% | 14.00% | 14.00% | 0.583 | 11.55s |
| self_debug_style | qwen2.5-coder:7b | 50 | 28.00% | 6.00% | 6.00% | 0.525 | 9.48s |
| direct_prompt | sqlcoder:7b | 50 | 0.00% | 0.00% | 0.00% | 0.002 | 3.30s |
| self_debug_style | sqlcoder:7b | 50 | 0.00% | 0.00% | 0.00% | 0.011 | 8.36s |

## Single vs Multi

| System | Model | Type | Exec | Result exact | Semantic pass | Avg score |
|---|---|---|---:|---:|---:|---:|
| coordsql_with_final | qwen3-vl:8b | multi | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_with_final | qwen3-vl:8b | single | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_no_final | qwen3-vl:8b | multi | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_no_final | qwen3-vl:8b | single | 100.00% | 100.00% | 100.00% | 1.000 |
| coordsql_schema_only | qwen3-vl:8b | multi | 100.00% | 0.00% | 0.00% | 0.490 |
| coordsql_schema_only | qwen3-vl:8b | single | 100.00% | 0.00% | 0.00% | 0.598 |
| direct_prompt | qwen2.5-coder:7b | multi | 16.00% | 8.00% | 8.00% | 0.498 |
| direct_prompt | qwen2.5-coder:7b | single | 40.00% | 4.00% | 4.00% | 0.544 |
| dail_sql_style | qwen2.5-coder:7b | multi | 4.00% | 4.00% | 4.00% | 0.456 |
| dail_sql_style | qwen2.5-coder:7b | single | 24.00% | 0.00% | 0.00% | 0.490 |
| din_sql_style | qwen2.5-coder:7b | multi | 44.00% | 28.00% | 28.00% | 0.636 |
| din_sql_style | qwen2.5-coder:7b | single | 40.00% | 0.00% | 0.00% | 0.530 |
| self_debug_style | qwen2.5-coder:7b | multi | 16.00% | 8.00% | 8.00% | 0.506 |
| self_debug_style | qwen2.5-coder:7b | single | 40.00% | 4.00% | 4.00% | 0.544 |
| direct_prompt | sqlcoder:7b | multi | 0.00% | 0.00% | 0.00% | 0.000 |
| direct_prompt | sqlcoder:7b | single | 0.00% | 0.00% | 0.00% | 0.004 |
| self_debug_style | sqlcoder:7b | multi | 0.00% | 0.00% | 0.00% | 0.011 |
| self_debug_style | sqlcoder:7b | single | 0.00% | 0.00% | 0.00% | 0.010 |

## Failure Types

### coordsql_with_final / qwen3-vl:8b
- no failures

### coordsql_no_final / qwen3-vl:8b
- no failures

### coordsql_schema_only / qwen3-vl:8b
- result_mismatch: 50

### direct_prompt / qwen2.5-coder:7b
- DatabaseError: 36
- result_mismatch: 11

### dail_sql_style / qwen2.5-coder:7b
- DatabaseError: 43
- result_mismatch: 6

### din_sql_style / qwen2.5-coder:7b
- DatabaseError: 29
- result_mismatch: 14

### self_debug_style / qwen2.5-coder:7b
- DatabaseError: 36
- result_mismatch: 11

### direct_prompt / sqlcoder:7b
- RuntimeError: 48
- DatabaseError: 2

### self_debug_style / sqlcoder:7b
- RuntimeError: 48
- DatabaseError: 2
