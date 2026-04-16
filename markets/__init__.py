"""
markets/__init__.py
====================
Loads all market configs so bot.py can import them easily.

Usage in bot.py:
    from markets import get_market_config
    cfg = get_market_config("NQ")
    print(cfg.MIN_RR)
    print(cfg.FULL_NAME)
"""

# Use relative imports to avoid circular import issues
from . import market_NQ, market_GC, market_BTC, market_SOL

MARKET_CONFIGS = {
    "NQ":  market_NQ,
    "GC":  market_GC,
    "BTC": market_BTC,
    "SOL": market_SOL,
}

def get_market_config(market: str):
    """Returns the config module for a given market."""
    cfg = MARKET_CONFIGS.get(market)
    if cfg is None:
        raise ValueError(f"Unknown market: {market}")
    return cfg

def get_all_markets() -> list:
    return list(MARKET_CONFIGS.keys())
