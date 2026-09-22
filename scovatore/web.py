"""Interfaccia web in sola lettura sui risultati: python -m scovatore web.

Solo libreria standard: nessuna dipendenza in piu' nel container. Legge lo stesso SQLite
del servizio aprendolo in sola lettura, quindi puo' girare in parallelo al loop.
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from .config import Config

log = logging.getLogger(__name__)

ESITI = {
    "verificati": "Verificati",
    "conforme": "Conformi",
    "incerto": "Incerti",
    "non_conforme": "Non conformi",
    "attesa": "In attesa di verifica",
    "scartati": "Scartati dalla scrematura",
    "tutti": "Tutti",
}
ORDINI = {"punteggio": "Punteggio", "prezzo": "Prezzo", "recenti": "Più recenti"}
LIMIT = 300


# ---------------------------------------------------------------------------
# Dati
# ---------------------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def hunts_overview(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT hunt,
                  COUNT(*) AS totale,
                  SUM(score IS NOT NULL) AS verificati,
                  SUM(verdict = 'conforme') AS conformi,
                  SUM(screen_verdict IS NOT NULL AND screen_verdict != 'no' AND score IS NULL) AS attesa,
                  MAX(first_seen) AS ultimo_nuovo
           FROM items GROUP BY hunt ORDER BY hunt""").fetchall()
    out = [dict(r) for r in rows]
    for h in out:
        h["ultimo_giro"] = last_run(conn, h["hunt"])
    return out


def last_run(conn: sqlite3.Connection, hunt: str) -> dict | None:
    r = conn.execute("SELECT * FROM runs WHERE hunt=? ORDER BY id DESC LIMIT 1", (hunt,)).fetchone()
    return _run_dict(r) if r else None


def recent_runs(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    return [_run_dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def _run_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["stats"] = json.loads(r["stats_json"] or "{}")
    return d


def countries(conn: sqlite3.Connection, hunt: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT country FROM items WHERE hunt=? AND country != '' ORDER BY country", (hunt,))]


def query_items(conn: sqlite3.Connection, hunt: str, esito: str = "verificati", min_score: int = 0,
                paese: str = "", ordina: str = "punteggio", giorni: int = 0) -> list[sqlite3.Row]:
    where = ["hunt = ?"]
    args: list = [hunt]
    if esito == "verificati":
        where.append("score IS NOT NULL")
    elif esito in ("conforme", "incerto", "non_conforme"):
        where.append("verdict = ?")
        args.append(esito)
    elif esito == "attesa":
        where.append("score IS NULL AND (screen_verdict IS NULL OR screen_verdict != 'no')")
    elif esito == "scartati":
        where.append("screen_verdict = 'no'")
    if min_score and esito not in ("attesa", "scartati"):
        where.append("COALESCE(score, -1) >= ?")
        args.append(min_score)
    if paese:
        where.append("country = ?")
        args.append(paese)
    if giorni:
        # first_seen e' ISO con la T: il confronto va fatto nello stesso formato
        where.append("first_seen >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
        args.append(f"-{int(giorni)} days")
    order = {
        "prezzo": "total ASC",
        "recenti": "first_seen DESC",
    }.get(ordina, "score IS NULL, score DESC, total ASC")
    return conn.execute(f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
                        (*args, LIMIT)).fetchall()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def ago(iso: str | None) -> str:
    if not iso:
        return "mai"
    try:
        t = datetime.fromisoformat(iso.replace(" ", "T"))
    except ValueError:
        return iso
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - t).total_seconds()
    if s < 90:
        return "ora"
    if s < 3600:
        return f"{int(s // 60)} min fa"
    if s < 86400 * 2:
        return f"{int(s // 3600)} h fa"
    return f"{int(s // 86400)} giorni fa"


def money(v, cur: str = "") -> str:
    if v is None:
        return "?"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + (f" {cur}" if cur else "")


def score_class(score) -> str:
    if score is None:
        return "s-none"
    return "s-hi" if score >= 70 else "s-mid" if score >= 40 else "s-lo"


CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a66;--line:#e3e1dc;--accent:#2f5d8a;
--hi:#1f7a4d;--mid:#a86b00;--lo:#b3261e;--chip:#eeece7}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe7;--muted:#9a9892;
--line:#33322f;--accent:#7fb0e0;--hi:#5cc48f;--mid:#e0a84a;--lo:#ef7b72;--chip:#2a2927}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--accent)}header{border-bottom:1px solid var(--line);background:var(--card)}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px}
.top{display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding-top:12px;padding-bottom:12px}
.brand{font-weight:700;font-size:17px;text-decoration:none;color:var(--ink)}
nav a{margin-right:14px;text-decoration:none;color:var(--muted)}nav a.on{color:var(--ink);font-weight:600}
h1{font-size:20px;margin:22px 0 4px}.sub{color:var(--muted);margin:0 0 14px}
form.f{display:flex;flex-wrap:wrap;gap:10px;align-items:end;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px;margin-bottom:16px}
form.f label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:3px}
select,input{font:inherit;padding:5px 7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
input[type=number]{width:80px}button{font:inherit;padding:6px 14px;border:0;border-radius:6px;
background:var(--accent);color:#fff;cursor:pointer}
.item{display:grid;grid-template-columns:96px 1fr auto;gap:14px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px;margin-bottom:10px}
.thumb{width:96px;height:96px;object-fit:cover;border-radius:6px;background:var(--chip)}
.title{font-weight:600;text-decoration:none;color:var(--ink)}.title:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:13px;margin-top:3px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 8px;font-size:12px;margin-right:4px}
.sintesi{margin-top:6px}
.right{text-align:right;min-width:110px}.price{font-size:18px;font-weight:700;white-space:nowrap}
.score{display:inline-block;font-weight:700;font-size:15px;padding:2px 9px;border-radius:6px;margin-bottom:6px;
border:1px solid currentColor}
.s-hi{color:var(--hi)}.s-mid{color:var(--mid)}.s-lo{color:var(--lo)}.s-none{color:var(--muted)}
details{margin-top:8px}summary{cursor:pointer;color:var(--accent);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
td,th{border-bottom:1px solid var(--line);padding:5px 6px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}.ok{color:var(--hi)}.ko{color:var(--lo)}.dub{color:var(--mid)}
ul.small{margin:4px 0 0 18px;padding:0;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:16px;margin:0 0 8px}.nums{display:flex;gap:16px;margin:6px 0}.nums b{display:block;font-size:20px}
.nums span{font-size:12px;color:var(--muted)}.err{color:var(--lo);font-size:13px}
.empty{padding:30px;text-align:center;color:var(--muted)}
.scroll{overflow-x:auto}
@media (max-width:640px){.item{grid-template-columns:64px 1fr}.thumb{width:64px;height:64px}
.right{grid-column:1/-1;text-align:left;display:flex;gap:12px;align-items:center}}
"""


def page(title: str, body: str, hunts: list[str], current: str = "") -> str:
    nav = "".join(f'<a href="/caccia/{quote(h)}" class="{"on" if h == current else ""}">{e(h)}</a>' for h in hunts)
    nav += f'<a href="/giri" class="{"on" if current == "__giri" else ""}">Giri</a>'
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · Scovatore</title>
<style>{CSS}</style></head><body><header><div class="wrap top"><a class="brand" href="/">Scovatore</a>
<nav>{nav}</nav></div></header><main class="wrap">{body}</main></body></html>"""


def render_home(conn) -> tuple[str, list[str]]:
    hs = hunts_overview(conn)
    names = [h["hunt"] for h in hs]
    if not hs:
        return '<div class="empty">Nessun annuncio nel database: la prima caccia non ha ancora girato.</div>', names
    cards = []
    for h in hs:
        r = h["ultimo_giro"]
        run_line = "nessun giro registrato"
        err = ""
        if r:
            dur = r["stats"].get("durate", {}).get("totale")
            run_line = f"ultimo giro {ago(r['started_at'])}" + (f", {dur:.0f} s" if dur else "")
            if not r["finished_at"]:
                run_line = f"giro in corso da {ago(r['started_at']).replace(' fa', '')}"
            if r["error"]:
                err = f'<div class="err">errore: {e(r["error"][:160])}</div>'
            elif r["stats"].get("errori"):
                n = len(r["stats"]["errori"])
                err = f'<div class="err">{n} error{"e" if n == 1 else "i"} nell\'ultimo giro: {e(r["stats"]["errori"][0][:120])}</div>'
        cards.append(f"""<a class="card" href="/caccia/{quote(h['hunt'])}" style="text-decoration:none;color:inherit">
<h2>{e(h['hunt'])}</h2><div class="nums"><div><b class="s-hi">{h['conformi'] or 0}</b><span>conformi</span></div>
<div><b>{h['verificati'] or 0}</b><span>verificati</span></div><div><b>{h['attesa'] or 0}</b><span>in attesa</span></div>
<div><b>{h['totale']}</b><span>visti</span></div></div>
<div class="meta">{e(run_line)} · ultimo annuncio nuovo {e(ago(h['ultimo_nuovo']))}</div>{err}</a>""")
    return f'<h1>Cacce</h1><div class="cards">{"".join(cards)}</div>', names


def _req_rows(v: dict) -> str:
    reqs = v.get("requisiti") or []
    if not reqs:
        return ""
    cls = {"ok": "ok", "ko": "ko", "no": "ko", "incerto": "dub"}
    rows = "".join(
        f"<tr><td>{e(r.get('requisito'))}</td><td class='{cls.get(str(r.get('stato')).lower(), '')}'>{e(r.get('stato'))}</td>"
        f"<td>{e(r.get('valore'))}</td><td>{e(r.get('fonte'))}</td><td>{e(r.get('nota'))}</td></tr>"
        for r in reqs if isinstance(r, dict))
    return (f"<div class='scroll'><table><tr><th>Requisito</th><th>Stato</th><th>Valore</th><th>Fonte</th>"
            f"<th>Nota</th></tr>{rows}</table></div>")


def _list(title: str, items) -> str:
    items = [i for i in (items or []) if i]
    if not items:
        return ""
    return f"<div class='meta' style='margin-top:8px'>{e(title)}</div><ul class='small'>" + \
        "".join(f"<li>{e(i)}</li>" for i in items) + "</ul>"


def render_item(r: sqlite3.Row) -> str:
    keys = r.keys()
    v = json.loads(r["verify_json"] or "{}")
    img = r["image"] if "image" in keys else ""
    thumb = f'<img class="thumb" loading="lazy" src="{e(img)}" alt="">' if img else '<div class="thumb"></div>'
    mp = (r["marketplace"] or "").removeprefix("EBAY_").lower()
    chips = [f"da {r['country'] or '?'}", f"su ebay.{'co.uk' if mp == 'gb' else mp}" if mp else ""]
    if r["is_auction"]:
        chips.append("asta")
    if r["condition"]:
        chips.append(r["condition"])
    chip_html = "".join(f'<span class="chip">{e(c)}</span>' for c in chips if c)
    ship = "spedizione ?" if r["shipping"] is None else f"+ {money(r['shipping'])} sped."
    esito = (r["verdict"] or ("scartato" if r["screen_verdict"] == "no" else "da verificare")).replace("_", " ")
    sintesi = v.get("sintesi") or (r["screen_reason"] if r["screen_verdict"] == "no" else "")
    detail = _req_rows(v) + _list("Segnali di rischio", v.get("segnali_rischio")) + \
        _list("Domande al venditore", v.get("domande_al_venditore"))
    if r["screen_verdict"]:
        detail += f"<div class='meta' style='margin-top:8px'>Scrematura: {e(r['screen_verdict'])}" + \
            (f", {e(r['screen_reason'])}" if r["screen_reason"] else "") + "</div>"
    detail += (f"<div class='meta'>Visto la prima volta {e(ago(r['first_seen']))}, l'ultima {e(ago(r['last_seen']))}"
               f" · venditore {e(r['seller'])} · ID {e(r['legacy_id'])}</div>")
    return f"""<div class="item">{thumb}<div>
<a class="title" href="{e(r['url'])}" target="_blank" rel="noopener">{e(r['title'])}</a>
<div class="meta">{chip_html}{' · visto ' + e(ago(r['first_seen']))}</div>
{f'<div class="sintesi">{e(sintesi)}</div>' if sintesi else ''}
<details><summary>Dettagli</summary>{detail}</details></div>
<div class="right"><div class="score {score_class(r['score'])}">{'-' if r['score'] is None else r['score']}</div>
<div class="price">{money(r['total'], r['currency'])}</div><div class="meta">{money(r['price'])} {e(ship)}</div>
<div class="meta">{e(esito)}</div></div></div>"""


def render_hunt(conn, hunt: str, q: dict) -> str:
    esito = q.get("esito", "verificati") if q.get("esito") in ESITI else "verificati"
    ordina = q.get("ordina", "punteggio") if q.get("ordina") in ORDINI else "punteggio"
    try:
        min_score = max(0, int(q.get("min") or 0))
    except ValueError:
        min_score = 0
    try:
        giorni = max(0, int(q.get("giorni") or 0))
    except ValueError:
        giorni = 0
    paese = (q.get("paese") or "").upper()[:2]
    rows = query_items(conn, hunt, esito, min_score, paese, ordina, giorni)

    def opts(d, cur):
        return "".join(f'<option value="{k}"{" selected" if k == cur else ""}>{e(v)}</option>' for k, v in d.items())

    paesi = {"": "Tutti"} | {c: c for c in countries(conn, hunt)}
    giorni_opt = {"0": "Sempre", "1": "Ultime 24 h", "3": "Ultimi 3 giorni", "7": "Ultima settimana"}
    r = last_run(conn, hunt)
    sub = "nessun giro registrato"
    if r:
        st = r["stats"]
        sub = (f"Ultimo giro {ago(r['started_at'])}: {st.get('query', 0)} ricerche, {st.get('unici', 0)} annunci unici, "
               f"{st.get('verificati', 0)} verificati, {st.get('chiamate_ebay', 0)} chiamate eBay")
        if not r["finished_at"]:
            sub = f"Giro in corso, avviato {ago(r['started_at'])}"
        if r["error"]:
            sub += f" · errore: {r['error'][:120]}"
    form = f"""<form class="f" method="get">
<label>Esito<select name="esito">{opts(ESITI, esito)}</select></label>
<label>Punteggio minimo<input type="number" name="min" min="0" max="100" value="{min_score}"></label>
<label>Paese<select name="paese">{opts(paesi, paese)}</select></label>
<label>Visti<select name="giorni">{opts(giorni_opt, str(giorni))}</select></label>
<label>Ordina<select name="ordina">{opts(ORDINI, ordina)}</select></label>
<button>Filtra</button></form>"""
    items = "".join(render_item(x) for x in rows) or '<div class="empty">Nessun annuncio con questi filtri.</div>'
    more = f'<p class="meta">Mostrati i primi {LIMIT}.</p>' if len(rows) >= LIMIT else ""
    return f"<h1>{e(hunt)}</h1><p class='sub'>{e(sub)} · {len(rows)} annunci</p>{form}{items}{more}"


def render_runs(conn) -> str:
    runs = recent_runs(conn)
    if not runs:
        return '<div class="empty">Nessun giro registrato.</div>'
    rows = []
    for r in runs:
        st = r["stats"]
        dur = st.get("durate", {}).get("totale")
        stato = "in corso" if not r["finished_at"] else ("errore" if r["error"] else "ok")
        cls = {"errore": "ko", "in corso": "dub"}.get(stato, "ok")
        fasi = ", ".join(f"{k} {v:.0f}s" for k, v in (st.get("durate") or {}).items() if k != "totale")
        errs = st.get("errori") or []
        err_html = f"<details><summary>{len(errs)} errori</summary><ul class='small'>" + \
            "".join(f"<li>{e(x[:300])}</li>" for x in errs) + "</ul></details>" if errs else ""
        rows.append(f"""<tr><td>{r['id']}</td><td>{e(r['hunt'])}</td><td>{e(ago(r['started_at']))}</td>
<td class="{cls}">{stato}</td><td>{'' if dur is None else f'{dur:.0f} s'}<div class="meta">{e(fasi)}</div></td>
<td>{st.get('query', '')}</td><td>{st.get('unici', '')}</td><td>{st.get('scartati_filtri', '')}</td>
<td>{st.get('nuovi', '')}</td><td>{st.get('scremati', '')}</td><td>{st.get('verificati', '')}</td>
<td>{st.get('conformi', '')}</td><td>{st.get('chiamate_ebay', '')}</td><td>{err_html}</td></tr>""")
    return f"""<h1>Giri recenti</h1><p class="sub">Ultimi {len(runs)} giri di tutte le cacce.</p>
<div class="scroll"><table><tr><th>#</th><th>Caccia</th><th>Avvio</th><th>Stato</th><th>Durata</th><th>Ricerche</th>
<th>Unici</th><th>Scartati</th><th>Nuovi</th><th>Scremati</th><th>Verificati</th><th>Conformi</th><th>eBay</th>
<th>Errori</th></tr>{''.join(rows)}</table></div>"""


def api_items(conn, hunt: str, q: dict) -> list[dict]:
    rows = query_items(conn, hunt, q.get("esito", "verificati"), int(q.get("min") or 0),
                       (q.get("paese") or "").upper(), q.get("ordina", "punteggio"), int(q.get("giorni") or 0))
    out = []
    for r in rows:
        d = dict(r)
        d["verifica"] = json.loads(d.pop("verify_json") or "null")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "scovatore-web"

        def log_message(self, fmt, *args):  # nel logging standard, a livello debug
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, q: dict) -> bool | str:
            if not cfg.web_token:
                return True
            if q.get("token") == cfg.web_token:
                return "set"
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return "scovatore_token" in c and c["scovatore_token"].value == cfg.web_token

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            auth = self._authorized(q)
            if not auth:
                return self._send(401, page("Accesso", '<div class="empty">Serve il token: apri la pagina '
                                                      'con <code>?token=...</code></div>', []))
            if auth == "set":  # token valido in URL: lo salvo in un cookie e lo tolgo dall'indirizzo
                q.pop("token")
                loc = u.path + ("?" + urlencode(q) if q else "")
                return self._send(303, "", extra={
                    "Location": loc,
                    "Set-Cookie": f"scovatore_token={cfg.web_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"})
            if u.path == "/salute":
                return self._send(200, "ok", "text/plain")
            conn = connect(cfg.db_path)
            if conn is None:
                return self._send(200, page("Scovatore", f'<div class="empty">Database non trovato in '
                                                         f'{e(cfg.db_path)}: la prima caccia non ha ancora girato.</div>', []))
            try:
                names = [h["hunt"] for h in hunts_overview(conn)]
                parts = [p for p in u.path.split("/") if p]
                if not parts:
                    body, names = render_home(conn)
                    return self._send(200, page("Cacce", body, names))
                if parts == ["giri"]:
                    return self._send(200, page("Giri", render_runs(conn), names, "__giri"))
                if len(parts) == 2 and parts[0] == "caccia" and parts[1] in names:
                    return self._send(200, page(parts[1], render_hunt(conn, parts[1], q), names, parts[1]))
                if len(parts) == 3 and parts[:2] == ["api", "caccia"] and parts[2] in names:
                    return self._send(200, json.dumps(api_items(conn, parts[2], q), ensure_ascii=False),
                                      "application/json; charset=utf-8")
                if parts == ["api", "giri"]:
                    return self._send(200, json.dumps(recent_runs(conn), ensure_ascii=False),
                                      "application/json; charset=utf-8")
                return self._send(404, page("Non trovato", '<div class="empty">Pagina non trovata.</div>', names))
            except Exception as exc:
                log.exception("errore interfaccia web su %s", self.path)
                return self._send(500, page("Errore", f'<div class="empty">Errore: {e(exc)}</div>', []))
            finally:
                conn.close()

    return Handler


def serve(cfg: Config, host: str | None = None, port: int | None = None) -> None:
    host = host or cfg.web_host
    port = port or cfg.web_port
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    log.info("interfaccia web su http://%s:%d (DB %s, token %s)", host, port, cfg.db_path,
             "attivo" if cfg.web_token else "disattivato")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
