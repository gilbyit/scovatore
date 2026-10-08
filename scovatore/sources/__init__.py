"""Fonti di annunci oltre a eBay. Ogni fonte espone la stessa interfaccia (vedi WebSource):

    scopes(hunt)         dove cercare (domini Vinted, regione Subito)
    scope_lang(scope)    lingua delle query per quello scope
    max_searches(hunt)   tetto di ricerche per giro
    search(q, scope, hunt) -> list[Listing]
    enrich(listing, max_chars)   descrizione e specifiche, per la verifica
    describe(hunt)       riga di log con i filtri applicati
    calls                richieste HTTP fatte

eBay resta nel suo modulo (usa la Browse API ufficiale, con filtri e budget propri).
"""
from __future__ import annotations

from ..config import Config
from ..hunt import SOURCE_LABELS, SOURCES, Hunt
from .base import SourceError, WebSource
from .subito import SubitoSource
from .vinted import VintedSource

__all__ = ["SourceError", "WebSource", "VintedSource", "SubitoSource", "build_sources",
           "SOURCES", "SOURCE_LABELS"]


def build_sources(hunt: Hunt, cfg: Config, names: list[str], transport=None) -> dict[str, WebSource]:
    """Crea i client delle fonti web richieste (tutte tranne eBay)."""
    out: dict[str, WebSource] = {}
    for n in names:
        if n == "vinted":
            out[n] = VintedSource(hunt.vinted, cfg.user_agent, cfg.scrape_delay, transport)
        elif n == "subito":
            out[n] = SubitoSource(hunt.subito, cfg.user_agent, cfg.scrape_delay, transport)
    return out
