"""Client minimale per la Browse API di eBay (OAuth client credentials)."""
from __future__ import annotations

import base64
import html
import logging
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote

import httpx

from .hunt import EbayParams

log = logging.getLogger(__name__)

SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayError(RuntimeError):
    pass


@dataclass
class Listing:
    item_id: str
    legacy_id: str
    marketplace: str
    title: str
    url: str
    price: float
    currency: str
    shipping: float | None          # None = costo non noto
    condition: str
    condition_id: str
    buying_options: list[str]
    country: str
    seller: str
    seller_feedback_pct: float | None
    seller_feedback_score: int | None
    image: str
    end_date: str
    query: str
    # riempiti da get_item
    description: str = ""
    aspects: dict[str, str] = field(default_factory=dict)
    condition_description: str = ""
    ships_to_buyer: bool | None = None   # dal dettaglio: None = non si sa

    @property
    def total(self) -> float:
        return round(self.price + (self.shipping or 0.0), 2)

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options and "FIXED_PRICE" not in self.buying_options


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    p = _TextExtractor()
    try:
        p.feed(raw)
        text = "".join(p.parts)
    except Exception:  # HTML rotto: si toglie tutto a mano
        text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def build_filter(p: EbayParams) -> str:
    """Traduce i parametri della caccia nella stringa `filter` della Browse API."""
    parts: list[str] = []
    if p.prezzo_min is not None or p.prezzo_max is not None:
        lo = "" if p.prezzo_min is None else f"{p.prezzo_min:g}"
        hi = "" if p.prezzo_max is None else f"{p.prezzo_max:g}"
        rng = f"{lo}..{hi}" if hi else lo
        parts.append(f"price:[{rng}]")
        parts.append(f"priceCurrency:{p.valuta}")
    if p.regione:
        parts.append(f"itemLocationRegion:{p.regione}")
    elif p.paese:
        parts.append(f"itemLocationCountry:{p.paese.upper()}")
    if p.consegna_paese:
        parts.append(f"deliveryCountry:{p.consegna_paese.upper()}")
    if p.condizioni:
        parts.append("conditionIds:{" + "|".join(str(c) for c in p.condizioni) + "}")
    if p.formati:
        parts.append("buyingOptions:{" + "|".join(p.formati) + "}")
    if p.solo_spedizione_gratuita:
        parts.append("maxDeliveryCost:0")
    if p.tipo_venditore:
        parts.append("sellerAccountTypes:{" + p.tipo_venditore + "}")
    if p.escludi_venditori:
        parts.append("excludeSellers:{" + "|".join(p.escludi_venditori) + "}")
    if p.cerca_in_descrizione:
        parts.append("searchInDescription:true")
    return ",".join(parts)


def _money(obj) -> float | None:
    if not obj or obj.get("value") in (None, ""):
        return None
    try:
        return float(obj["value"])
    except (TypeError, ValueError):
        return None


def _shipping(options) -> float | None:
    costs = [_money(o.get("shippingCost")) for o in options or []]
    costs = [c for c in costs if c is not None]
    return min(costs) if costs else None


BROAD_REGIONS = {"WORLDWIDE", "EUROPE", "EUROPEAN_UNION", "EU"}


def ships_to(body: dict, country: str) -> bool | None:
    """Se il venditore spedisce nel paese dell'acquirente, dal dettaglio dell'annuncio.

    Conservativo: False solo quando e' esplicito (paese o area escluso, oppure elenco di
    paesi ammessi che non lo contiene); True se le opzioni di spedizione calcolate per il
    nostro paese esistono o se l'area ammessa lo copre; None negli altri casi.
    """
    country = country.upper()
    loc = body.get("shipToLocations") or {}
    def ids(key):
        return {(str(r.get("regionType", "")).upper(), str(r.get("regionId", "")).upper())
                for r in loc.get(key) or [] if isinstance(r, dict)}
    excl, incl = ids("regionExcluded"), ids("regionIncluded")
    if ("COUNTRY", country) in excl or any(t == "WORLD_REGION" and i in BROAD_REGIONS for t, i in excl):
        return False
    if body.get("shippingOptions"):
        return True
    if any(t == "WORLDWIDE" or i in BROAD_REGIONS for t, i in incl) or ("COUNTRY", country) in incl:
        return True
    if incl and all(t == "COUNTRY" for t, _ in incl):
        return False   # spedisce solo a un elenco di paesi e il nostro non c'e'
    return None


def parse_summary(raw: dict, marketplace: str, query: str) -> Listing:
    price_obj = raw.get("price") or raw.get("currentBidPrice") or {}
    if raw.get("currentBidPrice") and "FIXED_PRICE" not in (raw.get("buyingOptions") or []):
        price_obj = raw["currentBidPrice"]
    seller = raw.get("seller") or {}
    fb = seller.get("feedbackPercentage")
    return Listing(
        item_id=raw.get("itemId", ""),
        legacy_id=str(raw.get("legacyItemId") or raw.get("itemId", "")),
        marketplace=marketplace,
        title=raw.get("title", ""),
        url=raw.get("itemWebUrl", ""),
        price=_money(price_obj) or 0.0,
        currency=price_obj.get("currency", ""),
        shipping=_shipping(raw.get("shippingOptions")),
        condition=raw.get("condition", ""),
        condition_id=str(raw.get("conditionId", "")),
        buying_options=list(raw.get("buyingOptions") or []),
        country=(raw.get("itemLocation") or {}).get("country", ""),
        seller=seller.get("username", ""),
        seller_feedback_pct=float(fb) if fb not in (None, "") else None,
        seller_feedback_score=seller.get("feedbackScore"),
        image=(raw.get("image") or {}).get("imageUrl", ""),
        end_date=raw.get("itemEndDate", ""),
        query=query,
    )


class EbayClient:
    def __init__(self, app_id: str, cert_id: str, api_base: str, buyer_country: str,
                 buyer_zip: str, call_budget: int, transport: httpx.BaseTransport | None = None):
        if not app_id or not cert_id:
            raise EbayError("EBAY_APP_ID e EBAY_CERT_ID mancanti nel .env")
        self.app_id = app_id
        self.cert_id = cert_id
        self.api_base = api_base
        ctx = f"country={buyer_country}"
        if buyer_zip:
            ctx += f",zip={buyer_zip}"
        self.enduser_ctx = "contextualLocation=" + quote(ctx, safe="")
        self.buyer_country = buyer_country
        self.call_budget = call_budget
        self.calls = 0
        self._token: str | None = None
        self._token_exp = 0.0
        self.http = httpx.Client(timeout=30, transport=transport)

    def close(self) -> None:
        self.http.close()

    # --- auth ---------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        basic = base64.b64encode(f"{self.app_id}:{self.cert_id}".encode()).decode()
        r = self.http.post(
            f"{self.api_base}/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        if r.status_code != 200:
            raise EbayError(f"OAuth eBay fallito ({r.status_code}): {r.text[:300]}")
        body = r.json()
        log.debug("eBay: nuovo token OAuth, scade fra %s s", body.get("expires_in"))
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 7200))
        return self._token

    def _get(self, path: str, marketplace: str, params: dict | None = None) -> dict:
        if self.calls >= self.call_budget:
            raise EbayError(f"budget di chiamate eBay esaurito ({self.call_budget})")
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "X-EBAY-C-ENDUSERCTX": self.enduser_ctx,
        }
        for attempt in range(3):
            self.calls += 1
            r = self.http.get(f"{self.api_base}{path}", params=params, headers=headers)
            log.debug("eBay %s %s -> %d (chiamata %d/%d)", marketplace, path.rsplit("/", 1)[-1][:40],
                      r.status_code, self.calls, self.call_budget)
            if r.status_code == 401 and attempt == 0:
                log.info("eBay: token scaduto, lo rinnovo")
                self._token = None
                headers["Authorization"] = f"Bearer {self._get_token()}"
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                log.warning("eBay %s: %d, riprovo fra %d s", marketplace, r.status_code, 2 * (attempt + 1))
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 404:
                return {}
            if r.status_code >= 400:
                raise EbayError(f"eBay {r.status_code} su {path}: {r.text[:400]}")
            return r.json()
        return {}

    # --- API ----------------------------------------------------------
    def search(self, query: str, marketplace: str, p: EbayParams) -> list[Listing]:
        flt = build_filter(p)
        out: list[Listing] = []
        offset = 0
        page = min(200, p.max_risultati_per_query)
        while len(out) < p.max_risultati_per_query:
            params = {"q": query, "limit": page, "offset": offset}
            if flt:
                params["filter"] = flt
            if p.ordinamento:
                params["sort"] = p.ordinamento
            cats = p.categorie.get(marketplace)
            if cats:
                params["category_ids"] = ",".join(str(c) for c in cats)
            log.debug("eBay search %s q=%r offset=%d params=%s", marketplace, query, offset,
                      {k: v for k, v in params.items() if k not in ("q", "offset")})
            body = self._get("/buy/browse/v1/item_summary/search", marketplace, params)
            items = body.get("itemSummaries") or []
            if body.get("warnings"):
                log.warning("eBay %s '%s': avvisi %s", marketplace, query,
                            [w.get("message") for w in body["warnings"]][:3])
            out.extend(parse_summary(i, marketplace, query) for i in items)
            total = int(body.get("total") or 0)
            offset += page
            if not items or offset >= total:
                break
        return out[: p.max_risultati_per_query]

    def get_item(self, listing: Listing, desc_max_chars: int) -> Listing:
        if not listing.item_id or "|" not in listing.item_id:
            return listing
        body = self._get(f"/buy/browse/v1/item/{quote(listing.item_id, safe='')}", listing.marketplace)
        if not body:
            return listing
        listing.description = html_to_text(body.get("description") or body.get("shortDescription") or "")[:desc_max_chars]
        listing.aspects = {a.get("name", ""): a.get("value", "") for a in body.get("localizedAspects") or []}
        listing.condition_description = body.get("conditionDescription", "") or ""
        ship = _shipping(body.get("shippingOptions"))
        if ship is not None:
            listing.shipping = ship
        listing.ships_to_buyer = ships_to(body, self.buyer_country)
        return listing
