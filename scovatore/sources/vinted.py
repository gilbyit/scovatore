"""Fonte Vinted: endpoint JSON del catalogo, lo stesso che usa il sito.

Funzionamento (non e' un'API pubblica, puo' cambiare):
- si apre la home del dominio per ottenere il cookie di sessione `access_token_web`;
- la ricerca e' `GET /api/v2/catalog/items`, con prezzo e stato filtrati da Vinted;
- l'endpoint JSON del singolo annuncio e' bloccato dall'anti-bot (403), quindi il dettaglio
  (descrizione) si legge dai metadati della pagina pubblica dell'annuncio.
Il prezzo e' `total_item_price` quando c'e': e' quello che paga l'acquirente, con la
protezione acquirenti inclusa. La spedizione dipende dal corriere scelto e non e' nel
catalogo: vale `spedizione_stimata` della caccia, se indicata.
"""
from __future__ import annotations

import logging
import math

from ..ebay import Listing, html_to_text
from ..hunt import VINTED_DOMAINS, Hunt, VintedParams
from .base import SourceError, WebSource, find_dicts, looks_blocked, page_head, safe_url, to_float

log = logging.getLogger(__name__)

SESSION_COOKIE = "access_token_web"
MAX_PER_PAGE = 96


class VintedSource(WebSource):
    name = "vinted"
    label = "Vinted"

    def __init__(self, params: VintedParams, user_agent: str, delay: float = 2.0, transport=None):
        super().__init__(user_agent, delay, transport)
        self.params = params
        self._ready: set[str] = set()

    # --- interfaccia comune ---------------------------------------------
    def scopes(self, hunt: Hunt) -> list[str]:
        return list(self.params.domini)

    def scope_lang(self, scope: str) -> str:
        return VINTED_DOMAINS[scope][1]

    def max_searches(self, hunt: Hunt) -> int:
        return self.params.max_ricerche

    def describe(self, hunt: Hunt) -> str:
        lo, hi = hunt.price_limits("vinted")
        bits = [f"domini {','.join(self.params.domini)}", f"prezzo {lo if lo is not None else ''}..{hi if hi is not None else ''}"]
        if self.params.condizioni:
            bits.append(f"stato {self.params.condizioni}")
        return ", ".join(bits)

    # --- sessione ---------------------------------------------------------
    def _base(self, scope: str) -> str:
        return f"https://www.vinted.{VINTED_DOMAINS[scope][0]}"

    def _ensure_session(self, scope: str, refresh: bool = False) -> None:
        if scope in self._ready and not refresh:
            return
        r = self._get(self._base(scope) + "/", accept="text/html,application/xhtml+xml")
        if r.status_code >= 400:
            raise SourceError(f"Vinted {scope}: home irraggiungibile ({r.status_code})", blocked=looks_blocked(r.text))
        if not any(c.name == SESSION_COOKIE for c in self.http.cookies.jar):
            # senza il cookie l'API di solito risponde 401: si prova lo stesso, poi si vedra'
            log.warning("Vinted %s: cookie %s non ricevuto, la ricerca potrebbe essere rifiutata", scope, SESSION_COOKIE)
        self._ready.add(scope)

    # --- ricerca ---------------------------------------------------------
    def search(self, query: str, scope: str, hunt: Hunt) -> list[Listing]:
        p = self.params
        lo, hi = hunt.price_limits("vinted")
        per_page = min(MAX_PER_PAGE, p.max_risultati_per_query)
        pages = math.ceil(p.max_risultati_per_query / per_page)
        out: list[Listing] = []
        for page in range(1, pages + 1):
            params: dict = {"search_text": query, "page": page, "per_page": per_page,
                            "order": p.ordinamento, "currency": p.valuta}
            if lo is not None:
                params["price_from"] = f"{lo:g}"
            if hi is not None:
                params["price_to"] = f"{hi:g}"
            if p.condizioni:
                params["status_ids[]"] = [str(i) for i in p.condizioni]
            if p.categorie:
                params["catalog[]"] = [str(i) for i in p.categorie]
            self._ensure_session(scope)
            url = self._base(scope) + "/api/v2/catalog/items"
            r = self._get(url, params=params, headers={"Referer": self._base(scope) + "/catalog"})
            if r.status_code == 401:       # cookie scaduto o mai ottenuto: una sola volta si rinnova
                log.info("Vinted %s: sessione scaduta, la rinnovo", scope)
                self.http.cookies.clear()
                self._ensure_session(scope, refresh=True)
                r = self._get(url, params=params, headers={"Referer": self._base(scope) + "/catalog"})
            body = self._json(r, "il catalogo")
            items = body.get("items") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise SourceError("Vinted: la risposta non contiene l'elenco 'items' (formato cambiato?)")
            for raw in items:
                l = self.parse_item(raw, scope, query)
                if l is not None:
                    out.append(l)
            if len(items) < per_page:
                break
        return out[: p.max_risultati_per_query]

    def parse_item(self, raw: dict, scope: str, query: str) -> Listing | None:
        if not isinstance(raw, dict) or raw.get("id") in (None, ""):
            return None
        iid = str(raw["id"])
        total_obj = raw.get("total_item_price")
        price_obj = raw.get("price")
        shown = total_obj if to_float(total_obj) else price_obj
        price = to_float(shown)
        if price is None:
            log.debug("Vinted: annuncio %s senza prezzo, saltato", iid)
            return None
        currency = (shown.get("currency_code") if isinstance(shown, dict) else None) \
            or (price_obj.get("currency_code") if isinstance(price_obj, dict) else None) \
            or raw.get("currency") or self.params.valuta
        base = self._base(scope)
        site = f"vinted.{VINTED_DOMAINS[scope][0]}"
        # l'URL dei dati vale solo se punta a Vinted: altrimenti si ricostruisce dall'ID
        url = safe_url(raw.get("url"), site)
        if not url and raw.get("path"):
            url = safe_url(base + str(raw["path"]), site)
        url = url or f"{base}/items/{iid}"
        photo = raw.get("photo") if isinstance(raw.get("photo"), dict) else {}
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        rep = to_float(user.get("feedback_reputation"))
        status = str(raw.get("status") or "")
        aspects = {k: str(v) for k, v in (("Marca", raw.get("brand_title")), ("Taglia", raw.get("size_title")),
                                          ("Stato", status)) if v}
        return Listing(
            item_id=iid, legacy_id=f"vinted:{iid}", marketplace=f"VINTED_{scope.upper()}",
            title=str(raw.get("title") or ""), url=url, price=price, currency=str(currency).upper(),
            shipping=self.params.spedizione_stimata, condition=status, condition_id="",
            buying_options=["FIXED_PRICE"], country="", seller=str(user.get("login") or ""),
            seller_feedback_pct=(rep * 100 if rep is not None and rep <= 1 else rep),
            seller_feedback_score=user.get("feedback_count") if isinstance(user.get("feedback_count"), int) else None,
            image=str(photo.get("url") or photo.get("full_size_url") or ""), end_date="", query=query,
            aspects=aspects, source="vinted")

    # --- dettaglio -------------------------------------------------------
    def enrich(self, listing: Listing, desc_max_chars: int) -> Listing:
        """Descrizione dalla pagina pubblica dell'annuncio (JSON-LD o meta description)."""
        if not safe_url(listing.url, "vinted." + listing.marketplace.removeprefix("VINTED_").lower()):
            raise SourceError(f"Vinted: URL dell'annuncio {listing.item_id} non valido, dettaglio saltato")
        r = self._get(listing.url, accept="text/html,application/xhtml+xml")
        if r.status_code >= 400:
            raise SourceError(f"Vinted: pagina dell'annuncio {listing.item_id} ha risposto {r.status_code}",
                              blocked=looks_blocked(r.text))
        meta, blocks = page_head(r.text)
        text = ""
        for d in find_dicts(blocks, lambda d: isinstance(d.get("description"), str) and d["description"].strip()):
            text = d["description"]
            break
        text = text or meta.get("og:description") or meta.get("description") or ""
        listing.description = html_to_text(text)[:desc_max_chars]
        return listing
