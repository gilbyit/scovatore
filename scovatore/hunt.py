"""Definizione di una caccia (file YAML in cacce/).

Tutto cio' che eBay sa filtrare da solo sta nella sezione `ebay` ed e' un parametro.
Il resto e' testo libero: `ricerca` (per Palantir) e `requisiti_avanzati` (per Groq).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Alias leggibili per conditionIds. Gli ID validi dipendono dalla categoria:
# per l'elettronica sono tipici 1000, 1500, 2000-2500, 3000, 7000.
CONDITION_ALIASES: dict[str, list[int]] = {
    "nuovo": [1000],
    "aperto": [1500],
    "ricondizionato": [2000, 2010, 2020, 2030, 2500],
    "usato": [3000, 4000, 5000, 6000],
    "guasto": [7000],       # "Per ricambi o non funzionante"
    "ricambi": [7000],
}

# Lingua delle query per ciascun marketplace.
MARKETPLACE_LANG: dict[str, str] = {
    "EBAY_IT": "it", "EBAY_DE": "de", "EBAY_AT": "de", "EBAY_CH": "de",
    "EBAY_FR": "fr", "EBAY_BE": "fr", "EBAY_ES": "es", "EBAY_NL": "nl",
    "EBAY_PL": "pl", "EBAY_IE": "en", "EBAY_GB": "en", "EBAY_US": "en",
}

VALID_BUYING = {"FIXED_PRICE", "AUCTION", "BEST_OFFER"}
VALID_REGIONS = {"EUROPEAN_UNION", "CONTINENTAL_EUROPE", "BORDER_COUNTRIES", "WORLDWIDE",
                 "UK_AND_IRELAND", "NORTH_AMERICA", "ASIA"}
VALID_SORT = {"", "price", "-price", "newlyListed", "endingSoonest", "distance"}


class HuntError(ValueError):
    pass


@dataclass
class EbayParams:
    marketplaces: list[str] = field(default_factory=lambda: ["EBAY_IT"])
    valuta: str = "EUR"
    prezzo_min: float | None = None
    prezzo_max: float | None = None
    spedizione_inclusa: bool = True          # il budget vale su prezzo + spedizione
    spedizione_ignota: str = "tieni"          # tieni | scarta: annunci senza costo di spedizione noto
    regione: str | None = "EUROPEAN_UNION"   # itemLocationRegion
    paese: str | None = None                  # itemLocationCountry (alternativo a regione)
    consegna_paese: str | None = "IT"         # deliveryCountry
    condizioni: list[int] = field(default_factory=list)
    formati: list[str] = field(default_factory=list)
    solo_spedizione_gratuita: bool = False
    tipo_venditore: str | None = None         # BUSINESS | INDIVIDUAL
    escludi_venditori: list[str] = field(default_factory=list)
    cerca_in_descrizione: bool = False
    categorie: dict[str, list[str]] = field(default_factory=dict)   # per marketplace
    ordinamento: str = "newlyListed"
    max_risultati_per_query: int = 100
    feedback_minimo: float | None = None      # % feedback venditore, filtro locale


@dataclass
class Hunt:
    nome: str
    ricerca: str
    requisiti_avanzati: str = ""
    ebay: EbayParams = field(default_factory=EbayParams)
    query_extra: list[str] = field(default_factory=list)    # query manuali, aggiunte a quelle di Palantir
    parole_escluse: list[str] = field(default_factory=list)  # scarto locale sul titolo
    screening: bool = True                    # passaggio Palantir sui titoli
    dati_riferimento: str | None = None       # file (CSV/testo) passato a Groq come fonte
    max_query_per_lingua: int = 4
    attiva: bool = True
    ogni_minuti: int = 180
    path: Path | None = None

    @property
    def plan_hash(self) -> str:
        langs = ",".join(sorted(self.languages()))
        src = f"{self.ricerca}\n{langs}\n{self.max_query_per_lingua}"
        return hashlib.sha256(src.encode()).hexdigest()[:16]

    @property
    def verify_hash(self) -> str:
        src = f"{self.requisiti_avanzati}\n{self.ricerca}\n{self.reference_text()}"
        return hashlib.sha256(src.encode()).hexdigest()[:16]

    def languages(self) -> list[str]:
        langs = {MARKETPLACE_LANG.get(m, "en") for m in self.ebay.marketplaces}
        langs.add("en")  # per l'hardware l'inglese rende su tutti i marketplace
        return sorted(langs)

    def reference_text(self) -> str:
        if not self.dati_riferimento:
            return ""
        p = Path(self.dati_riferimento)
        if not p.is_absolute() and self.path is not None:
            p = self.path.parent / p
        if not p.exists():
            raise HuntError(f"{self.nome}: dati_riferimento non trovato: {p}")
        return p.read_text(encoding="utf-8")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _conditions(raw) -> list[int]:
    out: list[int] = []
    for c in raw or []:
        if isinstance(c, int) or (isinstance(c, str) and c.isdigit()):
            out.append(int(c))
        elif isinstance(c, str) and c.lower() in CONDITION_ALIASES:
            out.extend(CONDITION_ALIASES[c.lower()])
        else:
            raise HuntError(f"condizione sconosciuta: {c!r} (usa un ID numerico o {sorted(CONDITION_ALIASES)})")
    return sorted(set(out))


def parse_hunt(data: dict, path: Path | None = None) -> Hunt:
    if not isinstance(data, dict):
        raise HuntError("il file della caccia deve essere un dizionario YAML")
    if not data.get("ricerca"):
        raise HuntError("manca il campo 'ricerca'")

    e = dict(data.get("ebay") or {})
    known = set(EbayParams.__dataclass_fields__)
    unknown = set(e) - known
    if unknown:
        raise HuntError(f"parametri ebay sconosciuti: {sorted(unknown)}")
    e["condizioni"] = _conditions(e.get("condizioni"))
    e["formati"] = [f.upper() for f in (e.get("formati") or [])]
    if e.get("categorie") is not None and not isinstance(e["categorie"], dict):
        raise HuntError("ebay.categorie deve essere {MARKETPLACE: [id, ...]}")
    ep = EbayParams(**e)

    bad = set(ep.formati) - VALID_BUYING
    if bad:
        raise HuntError(f"formati non validi: {sorted(bad)}")
    if ep.regione and ep.paese:
        raise HuntError("regione e paese sono alternativi: eBay rifiuta entrambi insieme")
    if ep.regione and ep.regione not in VALID_REGIONS:
        raise HuntError(f"regione non valida: {ep.regione}")
    if ep.ordinamento not in VALID_SORT:
        raise HuntError(f"ordinamento non valido: {ep.ordinamento}")
    if ep.spedizione_ignota not in ("tieni", "scarta"):
        raise HuntError("spedizione_ignota deve essere 'tieni' o 'scarta'")
    if ep.tipo_venditore and ep.tipo_venditore not in ("BUSINESS", "INDIVIDUAL"):
        raise HuntError("tipo_venditore deve essere BUSINESS o INDIVIDUAL")
    ep.marketplaces = [m.upper() for m in ep.marketplaces]
    if not 1 <= ep.max_risultati_per_query <= 1000:
        raise HuntError("max_risultati_per_query deve stare fra 1 e 1000")

    top_known = set(Hunt.__dataclass_fields__) - {"path", "ebay"}
    unknown_top = set(data) - top_known - {"ebay"}
    if unknown_top:
        raise HuntError(f"campi sconosciuti: {sorted(unknown_top)}")

    fields = {k: v for k, v in data.items() if k in top_known}
    fields["nome"] = _slug(str(data.get("nome") or (path.stem if path else "caccia")))
    return Hunt(ebay=ep, path=path, **fields)


def load_hunt(path: str | Path) -> Hunt:
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    try:
        return parse_hunt(data, p)
    except HuntError as exc:
        raise HuntError(f"{p.name}: {exc}") from exc


def load_all(hunts_dir: Path) -> list[Hunt]:
    return [load_hunt(p) for p in sorted(hunts_dir.glob("*.y*ml"))]
