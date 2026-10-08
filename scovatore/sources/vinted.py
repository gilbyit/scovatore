"""Fonte Vinted: l'endpoint JSON del catalogo che usa il sito.

Non e' un'API pubblica e cambia: da settembre 2026 il vecchio `www.vinted.<paese>/api/v2/catalog/items`
risponde 404 per tutti. Quello attuale e':
- ricerca: `GET https://api.vinted.<paese>/svc-catalogue/items`, con `Authorization: Bearer <token>`;
- token: il cookie `access_token_web` che il sito rilascia a una semplice `HEAD /catalog` (anonimo,
  dura circa 24 ore). Il server manda prima un cookie vuoto che cancella il precedente e poi quello
  vero: vale l'ultimo non vuoto;
- filtri: `attribute_ids[status]` e `attribute_ids[catalog]` (i vecchi `status_ids`/`catalog_ids` vengono
  ignorati in silenzio); un `price_from`/`price_to` vuoto da' 400, quindi si manda solo se c'e';
- risposta: `items[]` con `id`, `title`, `price` e `total_item_price` ({amount, currency_code}), `url`
  relativo, `photo.url`, `user.login`, e in `item_box` la marca (`first_line`, solo se diversa dal titolo)
  e "taglia · stato" (`second_line`). Non c'e' piu' la valuta come parametro.
- il dettaglio (descrizione) si legge dai metadati della pagina pubblica dell'annuncio.
Il prezzo e' `total_item_price` quando c'e': e' quello che paga l'acquirente, con la protezione acquirenti
inclusa. La spedizione non e' nel catalogo: vale `spedizione_stimata` della caccia, se indicata.
"""
from __future__ import annotations

import logging
import math
import re

from ..ebay import Listing, html_to_text
from ..hunt import VINTED_DOMAINS, Hunt, VintedParams
from .base import SourceError, WebSource, find_dicts, looks_blocked, page_head, safe_url, to_float

log = logging.getLogger(__name__)

SESSION_COOKIE = "access_token_web"
MAX_PER_PAGE = 96
TOKEN_RE = re.compile(SESSION_COOKIE + r"=([^;\s]*)")


class VintedSource(WebSource):
    name = "vinted"
    label = "Vinted"

    def __init__(self, params: VintedParams, user_agent: str, delay: float = 2.0, transport=None):
        super().__init__(user_agent, delay, transport)
        self.params = params
        self._tokens: dict[str, str] = {}

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
        """Il sito pubblico (link agli annunci, pagina di dettaglio, rilascio del token)."""
        return f"https://www.vinted.{VINTED_DOMAINS[scope][0]}"

    def _api(self, scope: str) -> str:
        return f"https://api.vinted.{VINTED_DOMAINS[scope][0]}"

    def _ensure_session(self, scope: str, refresh: bool = False) -> str:
        """Token anonimo del dominio: una HEAD sulla pagina del catalogo, poi si riusa finche' vale."""
        if scope in self._tokens and not refresh:
            return self._tokens[scope]
        self.http.cookies.clear()
        r = self._get(self._base(scope) + "/catalog", method="HEAD", accept="text/html,application/xhtml+xml")
        if r.status_code >= 400:
            raise SourceError(f"Vinted {scope}: pagina del catalogo irraggiungibile ({r.status_code})")
        tokens = [m.group(1) for h in r.headers.get_list("set-cookie") if (m := TOKEN_RE.search(h))]
        token = next((t for t in reversed(tokens) if t), "")
        if not token:       # rete o proxy che toglie i set-cookie: si prova col cookie jar del client
            token = next((c.value for c in self.http.cookies.jar if c.name == SESSION_COOKIE and c.value), "")
        if not token:
            raise SourceError(f"Vinted {scope}: il sito non ha rilasciato il token di sessione "
                              f"(blocco o cambio del sito?)")
        self._tokens[scope] = token
        return token

    # --- ricerca ---------------------------------------------------------
    def search(self, query: str, scope: str, hunt: Hunt) -> list[Listing]:
        p = self.params
        lo, hi = hunt.price_limits("vinted")
        per_page = min(MAX_PER_PAGE, p.max_risultati_per_query)
        pages = math.ceil(p.max_risultati_per_query / per_page)
        url = self._api(scope) + "/svc-catalogue/items"
        out: list[Listing] = []
        for page in range(1, pages + 1):
            params: dict = {"search_text": query, "page": page, "per_page": per_page, "order": p.ordinamento}
            if lo is not None:                      # un prezzo vuoto farebbe rispondere 400
                params["price_from"] = f"{lo:g}"
            if hi is not None:
                params["price_to"] = f"{hi:g}"
            if p.condizioni:
                params["attribute_ids[status]"] = ",".join(str(i) for i in p.condizioni)
            if p.categorie:
                params["attribute_ids[catalog]"] = ",".join(str(i) for i in p.categorie)
            r = self._catalog_get(url, params, scope)
            if r.status_code == 400:
                raise SourceError(f"Vinted: richiesta non valida (400): {r.text[:200]}. Controlla stato e "
                                  f"categorie della caccia (sezione vinted)")
            if r.status_code == 404:
                raise SourceError("Vinted: l'endpoint del catalogo non esiste piu' (404): il sito e' cambiato di nuovo")
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

    def _catalog_get(self, url: str, params: dict, scope: str):
        """GET del catalogo col token; su 401/403 il token si rinnova una volta sola."""
        for attempt in (0, 1):
            token = self._ensure_session(scope, refresh=attempt == 1)
            r = self._get(url, params=params, raise_on_block=False,
                          headers={"Authorization": f"Bearer {token}", "Referer": self._base(scope) + "/catalog",
                                   "Origin": self._base(scope)})
            if r.status_code not in (401, 403):
                if r.status_code == 429:
                    raise SourceError("Vinted: troppe richieste (429), riprova piu' tardi", blocked=True)
                return r
            log.info("Vinted %s: %d dal catalogo, %s", scope, r.status_code,
                     "rinnovo il token" if attempt == 0 else "rifiutato anche col token nuovo")
        raise SourceError(f"Vinted: richiesta rifiutata ({r.status_code}) anche con un token nuovo, probabile "
                          f"protezione anti-bot", blocked=True)

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
        rel = str(raw.get("url") or raw.get("path") or "")
        url = safe_url(base + rel if rel.startswith("/") else rel, site)     # ora l'URL e' relativo
        url = url or f"{base}/items/{iid}"
        photo = raw.get("photo") if isinstance(raw.get("photo"), dict) else {}
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        rep = to_float(user.get("feedback_reputation"))
        title = str(raw.get("title") or "")
        box = raw.get("item_box") if isinstance(raw.get("item_box"), dict) else {}
        first = str(box.get("first_line") or "")
        brand = raw.get("brand_title") or (first if first and first != title else "")   # = titolo: senza marca
        second = str(box.get("second_line") or "")
        size, _, cond = second.partition(" · ") if " · " in second else ("", "", second)
        size = raw.get("size_title") or size
        status = str(raw.get("status") or cond or "")      # "taglia · stato", oppure solo lo stato
        aspects = {k: str(v) for k, v in (("Marca", brand), ("Taglia", size), ("Stato", status)) if v}
        return Listing(
            item_id=iid, legacy_id=f"vinted:{iid}", marketplace=f"VINTED_{scope.upper()}",
            title=title, url=url, price=price, currency=str(currency).upper(),
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
