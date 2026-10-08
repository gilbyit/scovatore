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
import logging

log = logging.getLogger("scovatore")

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

# Marketplace eBay con sede nella UE. Non esiste un eBay per ogni paese: un venditore lituano,
# ceco o sloveno pubblica su uno di questi (di solito ebay.de). Il filtro sulla provenienza
# lo fa itemLocationRegion + il controllo locale su UE27, non la scelta dei marketplace.
EU_MARKETPLACES: list[str] = ["EBAY_IT", "EBAY_DE", "EBAY_FR", "EBAY_ES", "EBAY_NL",
                              "EBAY_BE", "EBAY_AT", "EBAY_IE", "EBAY_PL"]

# Stati membri UE (ISO 3166-1 alpha-2): unione doganale, niente dazi ne' IVA all'import.
# Regno Unito, Svizzera e Norvegia NON ci sono, anche se eBay a volte li mette in "Europa".
EU27: frozenset[str] = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
})

# Lingue delle query quando si cerca "in tutta la UE" da un solo marketplace: su ebay.it
# compaiono anche gli annunci dei venditori esteri che spediscono in Italia, ma con il titolo
# nella loro lingua. Una query tedesca lanciata su ebay.it trova il venditore di Berlino.
EU_LANGS: list[str] = ["it", "en", "de", "fr", "es", "nl", "pl"]

# Fonti di annunci attivabili da una caccia (campo `fonti`). eBay usa la Browse API ufficiale;
# Vinted e Subito non hanno API pubbliche e si leggono dai loro endpoint web (vedi sources/).
SOURCES = ("ebay", "vinted", "subito")
SOURCE_LABELS = {"ebay": "eBay", "vinted": "Vinted", "subito": "Subito"}
SOURCE_ALIASES = {"subito.it": "subito", "vinted.it": "vinted", "ebay.it": "ebay"}

# Domini Vinted -> (suffisso del sito, lingua delle query)
VINTED_DOMAINS: dict[str, tuple[str, str]] = {
    "it": ("it", "it"), "fr": ("fr", "fr"), "de": ("de", "de"), "es": ("es", "es"),
    "nl": ("nl", "nl"), "pl": ("pl", "pl"), "be": ("be", "fr"), "at": ("at", "de"),
    "lu": ("lu", "fr"), "pt": ("pt", "en"), "lt": ("lt", "en"), "cz": ("cz", "en"),
}
# Stato dell'oggetto su Vinted: alias leggibili -> status_ids
VINTED_STATUS: dict[str, list[int]] = {
    "nuovo_cartellino": [6], "nuovo": [6, 1], "ottimo": [2], "buono": [3], "discreto": [4],
}
VINTED_ORDER = {"newest_first", "relevance", "price_low_to_high", "price_high_to_low"}
SUBITO_ORDER = {"datedesc", "priceasc", "pricedesc"}

VALID_BUYING = {"FIXED_PRICE", "AUCTION", "BEST_OFFER"}
VALID_REGIONS = {"EUROPEAN_UNION", "CONTINENTAL_EUROPE", "BORDER_COUNTRIES", "WORLDWIDE",
                 "UK_AND_IRELAND", "NORTH_AMERICA", "ASIA"}
VALID_SORT = {"", "price", "-price", "newlyListed", "endingSoonest", "distance"}


class HuntError(ValueError):
    pass


@dataclass
class EbayParams:
    marketplaces: list[str] = field(default_factory=lambda: ["EBAY_IT"])  # dove si cerca; "ue" = tutti i siti UE
    valuta: str = "EUR"
    prezzo_min: float | None = None
    prezzo_max: float | None = None
    spedizione_inclusa: bool = True          # il budget vale su prezzo + spedizione
    spedizione_ignota: str = "scarta_estero"  # tieni | scarta | scarta_estero: annunci senza costo di spedizione noto
    spedizione_max: float | None = None       # scarta se la spedizione costa di piu' (controllo locale)
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
    paesi_ammessi: list[str] | None = None    # filtro locale sul paese dell'oggetto; default UE27
    max_ricerche: int = 150                   # tetto di ricerche (query x marketplace) per giro

    def allowed_countries(self) -> frozenset[str] | None:
        """Paesi accettati dal filtro locale, None = nessun filtro."""
        if self.paesi_ammessi is None:
            return EU27
        return frozenset(self.paesi_ammessi) or None


@dataclass
class VintedParams:
    """Parametri della fonte Vinted (sezione `vinted` della caccia). Tutto facoltativo."""
    domini: list[str] = field(default_factory=lambda: ["it"])   # vinted.it, vinted.fr, ...
    valuta: str = "EUR"
    prezzo_min: float | None = None           # se omessi valgono quelli della sezione `ebay`
    prezzo_max: float | None = None
    spedizione_stimata: float | None = None   # sommata al prezzo per il budget; None = si guarda solo il prezzo
    condizioni: list[int] = field(default_factory=list)   # status_ids (alias: nuovo, ottimo, buono, discreto)
    categorie: list[int] = field(default_factory=list)    # catalog_ids di Vinted
    ordinamento: str = "newest_first"
    max_risultati_per_query: int = 48
    max_ricerche: int = 20                    # tetto di ricerche (query x dominio) per giro


@dataclass
class SubitoParams:
    """Parametri della fonte Subito.it (sezione `subito` della caccia). Tutto facoltativo."""
    regione: str = "italia"                   # slug nell'URL: italia, piemonte, lombardia, ...
    categoria: str = "usato"                  # slug nell'URL: usato, informatica, audio-video, ...
    valuta: str = "EUR"
    prezzo_min: float | None = None           # se omessi valgono quelli della sezione `ebay`
    prezzo_max: float | None = None           # il filtro prezzo e' sempre locale
    ordinamento: str = "datedesc"
    max_risultati_per_query: int = 50
    max_ricerche: int = 12


@dataclass
class Hunt:
    nome: str
    ricerca: str
    requisiti_avanzati: str = ""
    fonti: list[str] = field(default_factory=lambda: ["ebay"])   # quali ricerche attivare
    ebay: EbayParams = field(default_factory=EbayParams)
    vinted: VintedParams = field(default_factory=VintedParams)
    subito: SubitoParams = field(default_factory=SubitoParams)
    query_extra: list[str] = field(default_factory=list)    # query manuali, aggiunte a quelle di Palantir
    parole_escluse: list[str] = field(default_factory=list)  # scarto locale sul titolo
    screening: bool = True                    # passaggio Palantir sui titoli
    dati_riferimento: str | None = None       # file (CSV/testo) passato a Groq come fonte
    max_query_per_lingua: int = 4
    lingue: list[str] | None = None           # lingue delle query; default: tutte quelle UE (EU_LANGS)
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
        langs = set(self.lingue if self.lingue else EU_LANGS)
        langs |= {MARKETPLACE_LANG.get(m, "en") for m in self.ebay.marketplaces}
        if "vinted" in self.fonti:
            langs |= {VINTED_DOMAINS[d][1] for d in self.vinted.domini}
        if "subito" in self.fonti:
            langs.add("it")
        langs.add("en")  # per l'hardware l'inglese rende ovunque
        return sorted(langs)

    def uses(self, source: str) -> bool:
        return source in self.fonti

    def price_limits(self, source: str) -> tuple[float | None, float | None]:
        """(min, max) del budget per una fonte: la sezione della fonte, altrimenti quella di eBay."""
        p = getattr(self, source)
        lo = p.prezzo_min if p.prezzo_min is not None else self.ebay.prezzo_min
        hi = p.prezzo_max if p.prezzo_max is not None else self.ebay.prezzo_max
        return lo, hi

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


def _marketplaces(raw) -> list[str]:
    """Accetta una lista o l'alias 'ue' (anche dentro la lista), senza duplicati."""
    if raw is None:
        return ["EBAY_IT"]
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for m in items:
        m = str(m).strip().upper()
        expanded = EU_MARKETPLACES if m in ("UE", "EU") else [m]
        for x in expanded:
            if x not in MARKETPLACE_LANG:
                raise HuntError(f"marketplace sconosciuto: {x} (validi: {sorted(MARKETPLACE_LANG)} o 'ue')")
            if x not in out:
                out.append(x)
    if not out:
        raise HuntError("serve almeno un marketplace")
    return out


def _languages(raw) -> list[str] | None:
    """'ue' -> tutte le lingue UE; altrimenti lista di codici (it, de, ...)."""
    from .prompts import LANG_NAMES
    if raw is None:
        return None
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for l in items:
        l = str(l).strip().lower()
        for x in (EU_LANGS if l in ("ue", "eu") else [l]):
            if x not in LANG_NAMES:
                raise HuntError(f"lingua non supportata: {x!r} (valide: {sorted(LANG_NAMES)} o 'ue')")
            if x not in out:
                out.append(x)
    return out or None


def _countries(raw) -> list[str] | None:
    """'ue' -> None (default UE27), 'tutti' / [] -> nessun filtro, altrimenti lista ISO; 'ue' si espande."""
    if raw is None:
        return None
    items = [raw] if isinstance(raw, str) else list(raw)
    out: set[str] = set()
    for c in items:
        c = str(c).strip().upper()
        if c in ("UE", "EU"):
            out |= EU27
        elif c in ("TUTTI", "*"):
            return []
        elif re.fullmatch(r"[A-Z]{2}", c):
            out.add(c)
        else:
            raise HuntError(f"paese non valido in paesi_ammessi: {c!r} (codice ISO a 2 lettere, 'ue' o 'tutti')")
    return sorted(out)


def _sources(raw) -> list[str]:
    """`fonti`: lista (o stringa) di eBay / Vinted / Subito. Default: solo eBay."""
    if raw is None:
        return ["ebay"]
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for s in items:
        s = str(s).strip().lower()
        s = SOURCE_ALIASES.get(s, s)
        if s not in SOURCES:
            raise HuntError(f"fonte sconosciuta: {s!r} (valide: {', '.join(SOURCES)})")
        if s not in out:
            out.append(s)
    if not out:
        raise HuntError("fonti e' vuoto: attiva almeno una fonte (ebay, vinted, subito)")
    return out


def _section(data: dict, key: str, cls):
    """Legge una sezione di parametri (`vinted`, `subito`) controllando i nomi."""
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HuntError(f"{key} deve essere una sezione di parametri")
    unknown = set(raw) - set(cls.__dataclass_fields__)
    if unknown:
        raise HuntError(f"parametri {key} sconosciuti: {sorted(unknown)}")
    return dict(raw)


def _check_limits(name: str, p) -> None:
    if p.prezzo_min is not None and p.prezzo_max is not None and p.prezzo_min > p.prezzo_max:
        raise HuntError(f"{name}: prezzo_min e' maggiore di prezzo_max")
    if not 1 <= p.max_ricerche <= 500:
        raise HuntError(f"{name}.max_ricerche deve stare fra 1 e 500")
    if not 1 <= p.max_risultati_per_query <= 500:
        raise HuntError(f"{name}.max_risultati_per_query deve stare fra 1 e 500")


def _vinted(data: dict) -> VintedParams:
    v = _section(data, "vinted", VintedParams)
    raw_dom = v.get("domini")
    if raw_dom is not None:
        items = [raw_dom] if isinstance(raw_dom, str) else list(raw_dom)
        doms: list[str] = []
        for d in items:
            d = str(d).strip().lower().removeprefix("vinted.").removeprefix("www.")
            if d not in VINTED_DOMAINS:
                raise HuntError(f"dominio Vinted sconosciuto: {d!r} (validi: {', '.join(sorted(VINTED_DOMAINS))})")
            if d not in doms:
                doms.append(d)
        if not doms:
            raise HuntError("vinted.domini e' vuoto")
        v["domini"] = doms
    conds: list[int] = []
    for c in v.get("condizioni") or []:
        if isinstance(c, int) or (isinstance(c, str) and c.isdigit()):
            conds.append(int(c))
        elif isinstance(c, str) and c.lower() in VINTED_STATUS:
            conds.extend(VINTED_STATUS[c.lower()])
        else:
            raise HuntError(f"condizione Vinted sconosciuta: {c!r} (usa un ID o {sorted(VINTED_STATUS)})")
    v["condizioni"] = sorted(set(conds))
    v["categorie"] = [int(c) for c in v.get("categorie") or []]
    vp = VintedParams(**v)
    if vp.ordinamento not in VINTED_ORDER:
        raise HuntError(f"vinted.ordinamento non valido: {vp.ordinamento} (validi: {sorted(VINTED_ORDER)})")
    _check_limits("vinted", vp)
    return vp


def _subito(data: dict) -> SubitoParams:
    s = _section(data, "subito", SubitoParams)
    for key in ("regione", "categoria"):
        if key in s:
            slug = _slug(str(s[key]))
            if not slug:
                raise HuntError(f"subito.{key} non valido: {s[key]!r}")
            s[key] = slug
    sp = SubitoParams(**s)
    if sp.ordinamento not in SUBITO_ORDER:
        raise HuntError(f"subito.ordinamento non valido: {sp.ordinamento} (validi: {sorted(SUBITO_ORDER)})")
    _check_limits("subito", sp)
    return sp


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
    e["marketplaces"] = _marketplaces(e.get("marketplaces"))
    if "paesi_ammessi" in e:
        e["paesi_ammessi"] = _countries(e["paesi_ammessi"])
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
    if ep.spedizione_ignota not in ("tieni", "scarta", "scarta_estero"):
        raise HuntError("spedizione_ignota deve essere 'tieni', 'scarta' o 'scarta_estero'")
    if ep.tipo_venditore and ep.tipo_venditore not in ("BUSINESS", "INDIVIDUAL"):
        raise HuntError("tipo_venditore deve essere BUSINESS o INDIVIDUAL")
    if ep.paese and ep.paesi_ammessi is None:
        ep.paesi_ammessi = [ep.paese.upper()]   # paese esplicito: il filtro locale lo segue
    if not 1 <= ep.max_ricerche <= 2000:
        raise HuntError("max_ricerche deve stare fra 1 e 2000")
    if not 1 <= ep.max_risultati_per_query <= 1000:
        raise HuntError("max_risultati_per_query deve stare fra 1 e 1000")
    if ep.spedizione_max is not None and ep.spedizione_max < 0:
        raise HuntError("spedizione_max non puo' essere negativa")

    nested = {"path", "ebay", "vinted", "subito"}
    top_known = set(Hunt.__dataclass_fields__) - nested
    unknown_top = set(data) - top_known - nested
    if unknown_top:
        raise HuntError(f"campi sconosciuti: {sorted(unknown_top)}")

    fields = {k: v for k, v in data.items() if k in top_known}
    if "lingue" in fields:
        fields["lingue"] = _languages(fields["lingue"])
    fields["fonti"] = _sources(data.get("fonti"))
    fields["nome"] = _slug(str(data.get("nome") or (path.stem if path else "caccia")))
    return Hunt(ebay=ep, vinted=_vinted(data), subito=_subito(data), path=path, **fields)


def load_hunt(path: str | Path) -> Hunt:
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    try:
        return parse_hunt(data, p)
    except HuntError as exc:
        raise HuntError(f"{p.name}: {exc}") from exc


def load_all(hunts_dir: Path) -> list[Hunt]:
    hunts: list[Hunt] = []
    for p in sorted(hunts_dir.glob("*.y*ml")):
        try:
            hunts.append(load_hunt(p))
        except Exception:
            log.exception("caccia %s non caricata, la salto", p.name)
    return hunts
    
