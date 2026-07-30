# LLM Pricing Table

`model_prices.json` here is a byte-for-byte vendored copy of LiteLLM's
`model_prices_and_context_window.json`, pinned to the same version as the
`litellm==` line in `legacy/plugins/bundles/requirements.txt`.

Current version: **`litellm-v1.83.14-stable`** (see `_PRICE_TABLE_VERSION` in
`model_pricing.py`).

## Refresh procedure

Refresh in the same PR that bumps `litellm==` — otherwise our cost estimates
drift from the LiteLLM proxy's actual pricing.

1. Check the LiteLLM version pinned in `legacy/plugins/bundles/requirements.txt`
   (e.g. `litellm[proxy,extra_proxy]==1.83.14`).
2. Fetch the matching tag (LiteLLM tag naming is `v<VERSION>-stable`; if that
   404s, try `v<VERSION>-stable.patch.<N>` with the highest N, or
   `v<VERSION>.rc.1` as a last resort):

   ```bash
   curl -sSL https://raw.githubusercontent.com/BerriAI/litellm/v1.83.14-stable/model_prices_and_context_window.json \
     -o legacy/plugins/tracing/utils/model_prices.json
   ```

3. Bump `_PRICE_TABLE_VERSION` in `model_pricing.py` to the new tag name.
4. Run the tracing test suite:

   ```bash
   cd legacy/plugins/tracing && python3 -m pytest utils/tests/ -v
   ```

5. Commit both files in one commit.

## Scope

`compute_llm_cost()` in `model_pricing.py` uses only these keys:

- `input_cost_per_token`
- `output_cost_per_token`
- `cache_read_input_token_cost` (falls back to `input_cost_per_token`)
- `cache_creation_input_token_cost` (falls back to `input_cost_per_token`)

**Deliberately skipped:**

- `input_cost_per_token_above_200k_tokens` and similar tier pricing — requires
  cumulative context knowledge that isn't available at per-call time.
- `input_cost_per_token_batches` — batch pricing, not applicable to our
  chat/agent flows today.
- `input_cost_per_token_priority` — priority-tier pricing, not routed today.

If any of the above become relevant, extend `compute_llm_cost()` and add a
regression test with a known model that has those keys.

## Known biases

- **Cache token accounting convention.** `compute_llm_cost()` treats
  `input_tokens` as already-net-of-cache. This is correct for Langfuse's
  normalized shape (where `"input"` excludes cache-read tokens). It is a
  slight over-count for the OpenAI-bypass shape (where `prompt_tokens`
  includes cached tokens): those cached tokens are billed at the full input
  rate rather than the cache-read discount rate. Not a large bias in
  practice — cache-heavy Anthropic workloads route through the normalized
  shape where this is correct — but flagged here for auditability.
- **Tier boundaries not modeled** (see Scope above).

## What happens on an unknown model

`compute_llm_cost()` returns `(None, None)` and logs a WARN once per unknown
model name. Watch pylon_indexer logs for `model_pricing: no entry for ...` —
that's your signal to refresh the vendored JSON.

Costs are always ESTIMATES. The `cost_source` column on `audit_events`
distinguishes `observed` (from the wire) from `estimated:<version>` (computed
here). UI must label estimated cost as such.
