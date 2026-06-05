# 50-Question Semantic Text-to-SQL Evaluation

- Time: 2026-06-05 02:41:40
- Benchmark: `E:\text2sql\semantic_50_benchmark.json`
- Cases: 50 (single=25, multi=25)
- Gold-table hints: False

## Summary

| System | Model | Cases | Exec | Result exact | Semantic pass | Avg score | Avg time |
|---|---|---:|---:|---:|---:|---:|---:|
| direct_prompt | qwen2.5-coder:7b | 50 | 28.00% | 6.00% | 6.00% | 0.521 | 4.34s |
| dail_sql_style | qwen2.5-coder:7b | 50 | 14.00% | 2.00% | 2.00% | 0.473 | 4.20s |
| din_sql_style | qwen2.5-coder:7b | 50 | 42.00% | 14.00% | 14.00% | 0.583 | 11.55s |
| self_debug_style | qwen2.5-coder:7b | 50 | 28.00% | 6.00% | 6.00% | 0.525 | 9.48s |

## Single vs Multi

| System | Model | Type | Exec | Result exact | Semantic pass | Avg score |
|---|---|---|---:|---:|---:|---:|
| direct_prompt | qwen2.5-coder:7b | multi | 16.00% | 8.00% | 8.00% | 0.498 |
| direct_prompt | qwen2.5-coder:7b | single | 40.00% | 4.00% | 4.00% | 0.544 |
| dail_sql_style | qwen2.5-coder:7b | multi | 4.00% | 4.00% | 4.00% | 0.456 |
| dail_sql_style | qwen2.5-coder:7b | single | 24.00% | 0.00% | 0.00% | 0.490 |
| din_sql_style | qwen2.5-coder:7b | multi | 44.00% | 28.00% | 28.00% | 0.636 |
| din_sql_style | qwen2.5-coder:7b | single | 40.00% | 0.00% | 0.00% | 0.530 |
| self_debug_style | qwen2.5-coder:7b | multi | 16.00% | 8.00% | 8.00% | 0.506 |
| self_debug_style | qwen2.5-coder:7b | single | 40.00% | 4.00% | 4.00% | 0.544 |

## Failure Types

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
