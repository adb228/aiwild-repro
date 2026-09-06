# Model-2 deviations

Luna (`gpt-5.6-luna`) is a different model and is never pooled with Sonnet. Science numbers stay in the model-2 analysis tables. This file is the process deviation register.

| Item | Status | n | Notes |
|---|---|---:|---|
| `reasoning_tokens` | estimated | 3071 | Confirmed-from-API 0 / estimated 3071 / missing-estimate 0. Response IDs are absent on confirmatory `model_call` events; retrieve was not attempted. Residual = `output_tokens − tokenizer(assistant_text + persisted worker tool_input)`, clamped at 0. |
| `reasoning.effort` | missing on events | 3071 | Process-level launch pin `effort=high` only; not stored per confirmatory request. |
| Temperature omitted at API | disclosed | — | Reasoning models reject sampling temperature when `reasoning.effort` is set. Episode field remains `0.0`. |
| Spreadsheet binding caps | disclosed | — | 9/14 vs Sonnet 40/100; output safety 77550/24760 vs 64000; wall kept 1200. |
| SWE binding caps | disclosed | — | Worker 92/108 vs 115/110; parent 83/123 vs 100/100. |
| SWE output-token safety | disclosed | — | 138590/166320 vs 36000/41000 (10× observed). |
| SWE wall safety | disclosed | — | 5290/3328 vs 1800. |
| SWE observation interpolation | disclosed | — | 80/160 interpolated into **pilot** prompts; hash guard compared builders at frozen 115/100. Confirmatory prompt files in `prompts/` are the frozen hashes. |
| 429 TPM handling | disclosed | — | Client wait 8× / 4–90s; concurrency drop 3→2 on SWE pilot; main launched at 3. |
| Prompt cache | disclosed | — | `prompt_cache_options` mode=`explicit`, ttl=`30m`. `previous_response_id` chaining with `store=true`. `max_output_tokens=128000`. |
| Cost card | disclosed | — | Luna list rates in `config/study/pricing.yaml`. Spreadsheet main total `$1.89720902`. |

Second-model runs used the model2 branch of the same code. The OpenAI client is `src/delegation_study/providers/model_client_openai.py`.
