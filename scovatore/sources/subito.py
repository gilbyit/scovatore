"""Fonte Subito.it: la pagina dei risultati contiene l'elenco degli annunci come JSON
(blocco `__NEXT_DATA__` delle pagine Next.js), che e' piu' stabile dell'HTML visibile.

Non e' un'API pubblica e il formato puo' cambiare: gli annunci si cercano nel JSON in modo
tollerante (qualunque oggetto con `subject` e `urls`/`urn`/`features`) e, se la pagina ha
annunci ma non si riesce a leggerli, la ricerca segnala "formato cambiato" invece di
restituire un elenco vuoto.

Il filtro di prezzo e' solo locale: nell'URL di Subito `ps`/`pe` sono indici di fasce, non
euro. La spedizione non e' nell'elenco: il totale e' il prezzo. L'area si sceglie con
`regione` (uno slug come `piemonte`): molti annunci sono solo a ritiro.
"""
from __future__ import annotations

import logging
import math
import re

from ..ebay import Listing, html_to_text
from ..hunt import Hunt, SubitoParams
from .base import SourceError, WebSource, find_dicts, looks_blocked, next_data, page_head, safe_url, to_float

log = logging.getLogger(__name__)

BASE = "https://www.subito.it"
PAGE_SIZE = 30            # annunci per pagina di risultati (circa)


def _is_ad(d: dict) -> bool:
    return isinstance(d.get("subject"), str) and any(k in d for k in ("urls", "urn", "features"))


def _features(d: dict) -> dict[str, dict]:
    """Caratteristiche dell'annuncio per URI ("/price", ...), sia in forma di dizionario che di lista."""
    f = d.get("features")
    out: dict[str, dict] = {}
    if isinstance(f, dict):
        for k, v in f.items():
            if isinstance(v, dict):
                out[str(k)] = v
    elif isinstance(f, list):
        for v in f:
            if isinstance(v, dict):
                out[str(v.get("uri") or v.get("id") or v.get("label") or len(out))] = v
    return out


def _first_value(feat: dict | None) -> dict:
    vals = (feat or {}).get("values")
    return vals[0] if isinstance(vals, list) and vals and isinstance(vals[0], dict) else {}


def _image(images) -> str:
    """Prima immagine https trovata nell'elenco immagini, qualunque sia la struttura."""
    stack = [images]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, str) and cur.startswith("http"):
            return cur
        if isinstance(cur, dict):
            for k in ("secureuri", "uri", "url", "cdnBaseUrl"):
                if isinstance(cur.get(k), str) and cur[k].startswith("http"):
                    return cur[k]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


class SubitoSource(WebSource):
    name = "subito"
    label = "Subito"

    def __init__(self, params: SubitoParams, user_agent: str, delay: float = 2.0, transport=None):
        super().__init__(user_agent, delay, transport)
        self.params = params

    # --- interfaccia comune ---------------------------------------------
    def scopes(self, hunt: Hunt) -> list[str]:
        return [self.params.regione]

    def scope_lang(self, scope: str) -> str:
        return "it"

    def max_searches(self, hunt: Hunt) -> int:
        return self.params.max_ricerche

    def describe(self, hunt: Hunt) -> str:
        lo, hi = hunt.price_limits("subito")
        return (f"regione {self.params.regione}, categoria {self.params.categoria}, "
                f"prezzo (locale) {lo if lo is not None else ''}..{hi if hi is not None else ''}")

    # --- ricerca ---------------------------------------------------------
    def search(self, query: str, scope: str, hunt: Hunt) -> list[Listing]:
        p = self.params
        url = f"{BASE}/annunci-{scope}/vendita/{p.categoria}/"
        pages = math.ceil(p.max_risultati_per_query / PAGE_SIZE)
        out: list[Listing] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            params = {"q": query, "order": p.ordinamento}
            if page > 1:
                params["o"] = str(page)
            r = self._get(url, params=params, accept="text/html,application/xhtml+xml")
            if r.status_code == 404:
                raise SourceError(f"Subito: pagina non trovata, controlla regione '{scope}' e categoria "
                                  f"'{p.categoria}'")
            if r.status_code >= 400:
                raise SourceError(f"Subito: la ricerca ha risposto {r.status_code}", blocked=looks_blocked(r.text))
            data = next_data(r.text)
            if data is None:
                raise SourceError("Subito: nella pagina manca il JSON dei risultati (pagina di blocco o "
                                  "formato cambiato?)", blocked=looks_blocked(r.text))
            ads = self._ads(data)
            if not ads and '"subject"' in r.text:
                raise SourceError("Subito: la pagina ha annunci ma non si riescono a leggere (formato cambiato?)")
            new = 0
            for raw in ads:
                l = self.parse_ad(raw, query)
                if l is not None and l.legacy_id not in seen:
                    seen.add(l.legacy_id)
                    out.append(l)
                    new += 1
            if not new or len(ads) < PAGE_SIZE // 2:
                break
        return out[: p.max_risultati_per_query]

    @staticmethod
    def _ads(data) -> list[dict]:
        """L'elenco principale se si trova al posto atteso, altrimenti tutti gli annunci della pagina."""
        try:
            lst = data["props"]["pageProps"]["initialState"]["items"]["list"]
            ads = find_dicts(lst, _is_ad)
            if ads:
                return ads
        except (KeyError, TypeError):
            pass
        return find_dicts(data, _is_ad)

    def parse_ad(self, d: dict, query: str) -> Listing | None:
        urls = d.get("urls") if isinstance(d.get("urls"), dict) else {}
        url = safe_url(urls.get("default") or d.get("url"), "subito.it")
        if not url:            # senza un link valido a subito.it l'annuncio non si puo' ne' aprire ne' scaricare
            log.debug("Subito: annuncio %r senza URL valido, saltato", d.get("urn"))
            return None
        m = re.search(r"id:ad:(\d+)", str(d.get("urn") or "")) or re.search(r"-(\d{6,})\.htm", url)
        if not m:
            return None
        aid = m.group(1)
        feats = _features(d)
        pv = _first_value(feats.get("/price"))
        price = to_float(pv.get("key"))
        if price is None:
            price = to_float(pv.get("value"))
        if price is None:
            log.debug("Subito: annuncio %s senza prezzo, saltato", aid)
            return None
        cond = ""
        for uri, f in feats.items():
            if uri.endswith("condition"):
                cond = str(_first_value(f).get("value") or "")
        geo = d.get("geo") if isinstance(d.get("geo"), dict) else {}

        def gv(k):
            v = geo.get(k)
            return str(v.get("value") or v.get("label") or "") if isinstance(v, dict) else ""
        city, region = gv("town") or gv("city"), gv("region")
        cat = d.get("category") if isinstance(d.get("category"), dict) else {}
        adv = d.get("advertiser") if isinstance(d.get("advertiser"), dict) else {}
        aspects = {k: v for k, v in (
            ("Luogo", f"{city} ({region})" if city and region else city or region),
            ("Categoria", str(cat.get("label") or "")),
            ("Venditore", ("Azienda" if adv.get("company") else "Privato") if "company" in adv else "")) if v}
        return Listing(
            item_id=aid, legacy_id=f"subito:{aid}", marketplace="SUBITO",
            title=str(d.get("subject") or ""), url=url, price=price,
            currency=self.params.valuta, shipping=None, condition=cond, condition_id="",
            buying_options=["FIXED_PRICE"], country="IT", seller=str(adv.get("name") or ""),
            seller_feedback_pct=None, seller_feedback_score=None, image=_image(d.get("images")),
            end_date="", query=query, description=html_to_text(str(d.get("body") or "")),
            aspects=aspects, source="subito")

    # --- dettaglio -------------------------------------------------------
    def enrich(self, listing: Listing, desc_max_chars: int) -> Listing:
        """Descrizione completa e caratteristiche dalla pagina dell'annuncio."""
        if not safe_url(listing.url, "subito.it"):
            raise SourceError(f"Subito: URL dell'annuncio {listing.item_id} non valido, dettaglio saltato")
        r = self._get(listing.url, accept="text/html,application/xhtml+xml")
        if r.status_code >= 400:
            raise SourceError(f"Subito: pagina dell'annuncio {listing.item_id} ha risposto {r.status_code}",
                              blocked=looks_blocked(r.text))
        data = next_data(r.text)
        detail = None
        if data is not None:
            for d in find_dicts(data, lambda d: _is_ad(d) and isinstance(d.get("body"), str)):
                if str(d.get("urn") or "").find(listing.item_id) >= 0 or detail is None:
                    detail = d
        if detail is not None:
            listing.description = html_to_text(detail["body"])[:desc_max_chars]
            for uri, f in _features(detail).items():
                label, val = f.get("label"), _first_value(f).get("value")
                if label and val and uri != "/price" and label not in listing.aspects:
                    listing.aspects[str(label)] = str(val)
        else:
            meta, _ = page_head(r.text)
            listing.description = html_to_text(meta.get("og:description") or meta.get("description") or "")[
                :desc_max_chars]
        return listing
