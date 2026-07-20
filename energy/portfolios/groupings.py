"""
Portfolio groupings — defines which commodities belong to each named portfolio.

Add new groupings here as strategies expand. Each entry is a plain list of
commodity names that must match the keys used in the strategy results dicts.
"""

# ── NGL complex ───────────────────────────────────────────────────────────────
NGL_MEMBERS = ["Propane", "Butane", "Ethane"]

# ── Core 6 (liquid energy benchmarks) ────────────────────────────────────────
CORE6_MEMBERS = ["WTI", "Brent", "Natgas", "RBOB", "ULSD", "Gasoil"]

# ── Combined (all nine) ───────────────────────────────────────────────────────
ALL_MEMBERS = CORE6_MEMBERS + NGL_MEMBERS
