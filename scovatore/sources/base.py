"""Base delle fonti web senza API ufficiale (Vinted, Subito.it).

Queste fonti leggono gli endpoint dei siti, non un'API documentata: possono cambiare o
bloccare senza preavviso. Il codice quindi e' difensivo (estrae il JSON in modo tollerante,
distingue "bloccato" da "errore passeggero") e non tenta di aggirare le protezioni: una
richiesta rifiutata ferma quella fonte per il giro, le altre fonti proseguono.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from html.parser import HTMLParser

import httpx

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """Errore di una fonte. `blocked` = il sito ha rifiutato la richiesta (anti-bot, 403/429):
    inutile insistere in questo giro."""

    def __init__(self, msg: str, blocked: bool = False):
        super().__init__(msg)
        self.blocked = blocked


class WebSource:
    """Client HTTP educato: una pausa fra le richieste, nessun tentativo di eludere i blocchi."""

    name = ""
    label = ""

    def __init__(self, user_agent: str, delay: float = 2.0, transport: httpx.BaseTransport | None = None):
        self.delay = delay
        self.calls = 0
        self._last = 0.0
        self.http = httpx.Client(
            timeout=30, transport=transport, follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept-Language": "it-IT,it;q=0.9,en;q=0.6"})

    def close(self) -> None:
        self.http.close()

    def _pace(self) -> None:
        wait = self._last + self.delay - time.monotonic()
        if self._last and wait > 0:
            time.sleep(wait)

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None,
             accept: str = "application/json") -> httpx.Response:
        """GET con pausa, un paio di tentativi sugli errori passeggeri e blocchi riconosciuti."""
        hdrs = {"Accept": accept, **(headers or {})}
        for attempt in range(3):
            self._pace()
            self.calls += 1
            try:
                r = self.http.get(url, params=params, headers=hdrs)
            except httpx.HTTPError as exc:
                self._last = time.monotonic()
                if attempt < 2:
                    log.warning("%s: %s, riprovo", self.label, exc)
                    time.sleep(2 * (attempt + 1))
                    continue
                raise SourceError(f"{self.label}: rete non raggiungibile ({exc})") from exc
            self._last = time.monotonic()
            log.debug("%s GET %s -> %d (richiesta %d)", self.label, url.split("?")[0][-70:], r.status_code, self.calls)
            if r.status_code in (403, 429):
                raise SourceError(f"{self.label}: richiesta rifiutata ({r.status_code}), probabile protezione "
                                  f"anti-bot o troppe richieste", blocked=True)
            if r.status_code >= 500 and attempt < 2:
                log.warning("%s: %d, riprovo fra %d s", self.label, r.status_code, 2 * (attempt + 1))
                time.sleep(2 * (attempt + 1))
                continue
            return r
        raise SourceError(f"{self.label}: nessuna risposta utile")  # pragma: no cover

    def _json(self, r: httpx.Response, what: str):
        if r.status_code >= 400:
            raise SourceError(f"{self.label}: {what} ha risposto {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise SourceError(f"{self.label}: {what} non ha risposto con JSON (pagina di blocco?)",
                              blocked=looks_blocked(r.text)) from exc


# ---------------------------------------------------------------------------
# Utilita' di parsing
# ---------------------------------------------------------------------------
def looks_blocked(text: str) -> bool:
    low = text[:6000].lower()
    return any(k in low for k in ("captcha", "datadome", "access denied", "verify you are human",
                                  "are you a robot", "pardon our interruption"))


def safe_url(url, host_suffix: str) -> str:
    """L'URL se e' http(s) e punta al sito atteso (es. "vinted.it"), altrimenti "".

    Gli URL arrivano dai dati del sito e poi si usano per scaricare il dettaglio: non devono poter
    puntare altrove (altri host, schemi strani, indirizzi interni).
    """
    from urllib.parse import urlparse
    try:
        u = urlparse(str(url or ""))
        host = (u.hostname or "").lower()
    except ValueError:
        return ""
    ok = u.scheme in ("http", "https") and (host == host_suffix or host.endswith("." + host_suffix))
    return str(url) if ok else ""


def to_float(v) -> float | None:
    """Numero da un valore qualsiasi: 12, "12.5", "12,50 €", {"amount": "12.5"}."""
    if isinstance(v, dict):
        for k in ("amount", "value", "key", "price"):
            if k in v:
                return to_float(v[k])
        return None
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:                 # 1.234,50 oppure 1,234.50: vale l'ultimo separatore
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def find_dicts(obj, pred, limit: int = 2000) -> list[dict]:
    """Tutti i dizionari annidati che soddisfano `pred`, senza scendere dentro quelli trovati."""
    out: list[dict] = []
    stack = [obj]
    while stack and len(out) < limit:
        cur = stack.pop()
        if isinstance(cur, dict):
            if pred(cur):
                out.append(cur)
                continue
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    out.reverse()    # lo stack inverte l'ordine: si rimette quello della pagina
    return out


NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(page: str):
    """JSON incorporato nelle pagine Next.js, None se manca o e' rotto."""
    m = NEXT_DATA_RE.search(page)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


class _HeadParser(HTMLParser):
    """Estrae <meta property/name=...> e i blocchi JSON-LD."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.ld: list[str] = []
        self._in_ld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content") is not None and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld, self._buf = True, []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld.append("".join(self._buf))

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)


def page_head(page: str) -> tuple[dict[str, str], list]:
    """(meta tag, blocchi JSON-LD gia' decodificati) di una pagina HTML."""
    p = _HeadParser()
    try:
        p.feed(page)
    except Exception:  # HTML rotto: si usa quello che e' stato letto
        pass
    blocks = []
    for raw in p.ld:
        try:
            blocks.append(json.loads(html.unescape(raw)))
        except ValueError:
            continue
    return p.meta, blocks
