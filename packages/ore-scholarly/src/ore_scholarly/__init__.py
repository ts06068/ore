"""Independent scholarly adapters and Rune packs for ORE (Python 3.12+)."""
from .common import ScholarlyError, decode_cursor, fingerprint, session, validate
from .registry import list_sources, source_config
from .resolvers import RESOLVERS
from .searchers import SEARCHERS
from .packs import load_rune, list_runes

__version__ = "0.1.0"
__all__ = ["ScholarlyError", "list_sources", "search", "resolve", "load_rune", "list_runes"]


async def search(source, query, *, year_from=None, year_to=None, journals=None,
                 limit=20, cursor=None, config=None) -> dict:
    source, values = source_config(source, config)
    if source not in SEARCHERS:
        raise ScholarlyError("operation_unsupported", f"{source} has no metadata search API in this release.", source=source)
    validate(query, year_from, year_to, journals, limit)
    signature = fingerprint(source, query, year_from, year_to, journals, limit, values)
    state = decode_cursor(cursor, source, signature)
    async with session(values) as client:
        return await SEARCHERS[source](client, query, year_from, year_to, journals, limit, state, signature, values)


async def resolve(source, identifier, config=None) -> dict:
    source, values = source_config(source, config)
    if source not in RESOLVERS:
        raise ScholarlyError("operation_unsupported", f"{source} has no full-text resolver in this release.", source=source)
    async with session(values) as client:
        return await RESOLVERS[source](client, identifier, values)
