"""LLM cost estimation, sourced from the `costs` catalog table.

Prices live in `centry.model_prices` (custom overrides + daily-refreshed base
prices), owned and written exclusively by the pylon_main `costs` plugin. This
module is a read-only consumer: it seeds a process-local cache directly from
that table and reseeds daily. On any DB failure it falls back to the bundled
canonical dump (`prices_seed.json`) so estimation still works air-gapped or
before the catalog table is reachable.

Reading the table directly (rather than an RPC) works uniformly in every pylon,
including pylon_indexer/pylon_auth which have no cross-pylon RPC transport and
no `from tools import db`; only the `POSTGRES_*` env vars are required.

Lookup mirrors the catalog cache: exact -> lowercase -> strip common provider
prefixes, then per-entry aliases. `compute_llm_cost` keeps its original
signature so `audit_processor` is unaffected.
"""

import json
import os
import time
import threading

from pylon.core.tools import log

_COST_SOURCE_TAG = "estimated:costs-catalog"
_SEED_TTL = 86400  # reseed at most once/day
_SEED_PATH = os.path.join(os.path.dirname(__file__), "prices_seed.json")
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "centry")

# Provider prefixes stripped when the exact model_name isn't found. Region
# prefixes (us./eu./apac./ca.) are the Bedrock inference-profile convention.
_STRIP_PREFIXES = (
    "openai/", "azure/", "anthropic/", "bedrock/", "vertex_ai/", "gemini/",
    "us.", "eu.", "apac.", "ca.",
)

_lock = threading.Lock()
_engine = None         # lazily-built read-only SQLAlchemy engine
_by_name = None        # {model_name: price_dict}
_alias_index = None    # {alias: model_name}
_last_seeded = 0.0
_unknown_models = set()  # dedupe per-model WARN logs


def configure(rpc_manager=None):
    """Kept for call-site compatibility; the DB reader needs no rpc_manager."""
    return None


def prime():
    """Eagerly seed the cache in the parent process before workers fork.

    Seeding here lets forked workers inherit the populated cache (incl. custom
    prices) copy-on-write instead of each opening its own DB connection or
    falling back to the bundled dump. The DB read is synchronous and reliable,
    so no retry thread is needed; a bundled seed covers any DB failure.
    """
    _ensure_seeded()
    # Dispose the engine so forked workers can't inherit an open socket; each
    # child lazily rebuilds its own on the next reseed.
    global _engine
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
        _engine = None


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    from sqlalchemy import create_engine
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ["POSTGRES_HOST"]
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = os.environ["POSTGRES_DB"]
    url = f"postgresql://{user}:{password}@{host}:{port}/{database}"
    _engine = create_engine(url, pool_size=1, max_overflow=1, pool_pre_ping=True)
    return _engine


def _seed_from_db():
    """Read the effective price map straight from `centry.model_prices`."""
    from sqlalchemy import text
    engine = _get_engine()
    by_name = {}
    alias_index = {}
    query = text(
        "SELECT model_name, provider, mode, input_cost_per_token, "
        "output_cost_per_token, cache_read_input_token_cost, "
        "cache_creation_input_token_cost, aliases, is_custom "
        f"FROM {_SCHEMA}.model_prices"
    )
    with engine.connect() as conn:
        for row in conn.execute(query).mappings():
            name = row["model_name"]
            if not name:
                continue
            by_name[name] = {
                "model_name": name,
                "provider": row["provider"],
                "mode": row["mode"],
                "input_cost_per_token": _f(row["input_cost_per_token"]),
                "output_cost_per_token": _f(row["output_cost_per_token"]),
                "cache_read_input_token_cost": _f(row["cache_read_input_token_cost"]),
                "cache_creation_input_token_cost": _f(row["cache_creation_input_token_cost"]),
                "is_custom": row["is_custom"],
            }
            for alias in (row["aliases"] or []):
                if alias:
                    alias_index[alias] = name
    if not by_name:
        return None
    log.info("model_pricing: seeded %d models from costs catalog table", len(by_name))
    return by_name, alias_index


def _f(value):
    return float(value) if value is not None else None


def _seed_from_bundled():
    try:
        with open(_SEED_PATH, "r") as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        log.error("model_pricing: failed to load %s: %s", _SEED_PATH, e)
        return {}, {}
    by_name = {}
    alias_index = {}
    for entry in payload.get("entries", []):
        name = entry.get("model_name")
        if not name:
            continue
        by_name[name] = entry
        for alias in (entry.get("aliases") or []):
            if alias:
                alias_index[alias] = name
    log.info("model_pricing: seeded %d models from bundled dump", len(by_name))
    return by_name, alias_index


def _ensure_seeded():
    global _by_name, _alias_index, _last_seeded
    now = time.time()
    if _by_name is not None and (now - _last_seeded) < _SEED_TTL:
        return
    with _lock:
        if _by_name is not None and (time.time() - _last_seeded) < _SEED_TTL:
            return
        seeded = None
        try:
            seeded = _seed_from_db()
        except Exception as e:
            log.warning("model_pricing: DB seed failed, using bundled: %r", e)
        if not seeded:
            seeded = _seed_from_bundled()
        by_name, alias_index = seeded
        # Never blank a good cache on a failed reseed.
        if by_name or _by_name is None:
            _by_name, _alias_index = by_name, alias_index
        # Only advance the TTL clock when data actually loaded, so a total
        # failure retries on the next call instead of being stuck for a day.
        if by_name:
            _last_seeded = time.time()


def _lookup(model_name):
    """Exact -> lowercase -> strip provider prefixes -> alias."""
    if not model_name:
        return None
    _ensure_seeded()
    if model_name in _by_name:
        return _by_name[model_name]
    lowered = model_name.lower()
    if lowered in _by_name:
        return _by_name[lowered]
    for prefix in _STRIP_PREFIXES:
        if model_name.startswith(prefix):
            stripped = model_name[len(prefix):]
            if stripped in _by_name:
                return _by_name[stripped]
        if lowered.startswith(prefix):
            stripped = lowered[len(prefix):]
            if stripped in _by_name:
                return _by_name[stripped]
    resolved = _alias_index.get(model_name) or _alias_index.get(lowered)
    if resolved:
        return _by_name.get(resolved)
    return None


def compute_llm_cost(model_name, input_tokens, output_tokens,
                     cache_read_input_tokens=0,
                     cache_creation_input_tokens=0):
    """Estimate LLM cost in USD from token counts.

    Returns (cost_usd, cost_source_tag) or (None, None) if the model is
    unpriced. Cache-read/creation tokens are priced separately when the
    entry has those keys; otherwise they fall back to the base input rate.
    """
    if not model_name or (not input_tokens and not output_tokens):
        return None, None

    entry = _lookup(model_name)
    if not entry or not isinstance(entry, dict):
        if model_name not in _unknown_models:
            _unknown_models.add(model_name)
            log.warning("model_pricing: no entry for %r", model_name)
        return None, None

    input_price = entry.get("input_cost_per_token")
    output_price = entry.get("output_cost_per_token")
    if input_price is None and output_price is None:
        # Embeddings/image/audio-only entries — not billable as chat.
        return None, None

    input_price = input_price or 0.0
    output_price = output_price or 0.0
    cache_read_price = entry.get("cache_read_input_token_cost")
    cache_create_price = entry.get("cache_creation_input_token_cost")
    if cache_read_price is None:
        cache_read_price = input_price
    if cache_create_price is None:
        cache_create_price = input_price

    cost = (
        (input_tokens or 0) * input_price
        + (output_tokens or 0) * output_price
        + (cache_read_input_tokens or 0) * cache_read_price
        + (cache_creation_input_tokens or 0) * cache_create_price
    )
    return cost, _COST_SOURCE_TAG
