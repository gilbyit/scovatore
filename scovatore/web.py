"""Interfaccia web sui risultati: python -m scovatore web.

Solo libreria standard: nessuna dipendenza in piu' nel container. Le pagine leggono il
database in sola lettura; le azioni dell'operatore (elimina, correggi esito, riverifica)
scrivono con transazioni brevi, quindi il servizio puo' girare in parallelo al loop.
"""
from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from .config import Config
from .db import DB, MANUAL_VERDICTS, STALE_RUN_HOURS
import yaml

from . import huntfiles
from .hunt import SOURCE_LABELS, SOURCES, Hunt, load_hunt, slug

log = logging.getLogger(__name__)

POLL_HINT = 10                      # secondi: ogni quanto il loop guarda le richieste (cli.REQUEST_POLL_SECONDS)
ITEM_ID = re.compile(r"(?:[a-z]+:)?\d+")   # ID legacy eBay (solo cifre) o "vinted:123", "subito:456"

ESITI = {
    "verificati": "Verificati",
    "conforme": "Conformi",
    "incerto": "Incerti",
    "non_conforme": "Non conformi",
    "attesa": "In attesa di verifica",
    "scartati": "Scartati dalla scrematura",
    "manuali": "Corretti a mano",
    "tutti": "Tutti",
    "eliminati": "Eliminati",
}
# azione -> (etichetta, esito al singolare, esito al plurale)
AZIONI = {
    "conforme": ("Segna conforme", "segnato come conforme", "segnati come conformi"),
    "incerto": ("Segna incerto", "segnato come incerto", "segnati come incerti"),
    "non_conforme": ("Segna non conforme", "segnato come non conforme", "segnati come non conformi"),
    "annulla": ("Togli correzione", "riportato al giudizio del modello", "riportati al giudizio del modello"),
    "riverifica": ("Rimetti in verifica", "rimesso in verifica per il prossimo giro",
                   "rimessi in verifica per il prossimo giro"),
    "elimina": ("Elimina", "eliminato", "eliminati"),
    "ripristina": ("Ripristina", "ripristinato", "ripristinati"),
}
EFF = "COALESCE(manual_verdict, verdict)"   # esito effettivo: l'operatore prevale sul modello
ORDINI = {"punteggio": "Punteggio", "prezzo": "Prezzo", "recenti": "Più recenti"}
FONTI = {"": "Tutte"} | SOURCE_LABELS
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
                  SUM(hidden = 0) AS totale,
                  SUM(hidden = 0 AND (score IS NOT NULL OR manual_verdict IS NOT NULL)) AS verificati,
                  SUM(hidden = 0 AND {EFF} = 'conforme') AS conformi,
                  SUM(hidden = 0 AND manual_verdict IS NULL AND score IS NULL
                      AND screen_verdict IS NOT NULL AND screen_verdict != 'no') AS attesa,
                  SUM(hidden = 1) AS eliminati,
                  MAX(first_seen) AS ultimo_nuovo
           FROM items GROUP BY hunt ORDER BY hunt""".replace("{EFF}", EFF)).fetchall()
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


def hunt_defs(cfg: Config) -> dict[str, Hunt]:
    """Le cacce definite nei file YAML (se la cartella e' montata), per nome. Le non valide si saltano."""
    out: dict[str, Hunt] = {}
    try:
        files = sorted(cfg.hunts_dir.glob("*.y*ml"))
    except OSError:
        return out
    for p in files:
        try:
            h = load_hunt(p)
            out[h.nome] = h
        except Exception as exc:
            log.debug("web: caccia %s non letta: %s", p.name, exc)
    return out


def hunt_files(cfg: Config) -> tuple[dict[str, Hunt], set[str], bool]:
    """(cacce valide per nome, nomi di TUTTI i file YAML anche se non validi, cartella leggibile?).

    Serve a distinguere una caccia eliminata (file sparito) da una caccia con il file rotto o dalla
    cartella non montata: in quei due casi non si deve mai dire "eliminata".
    """
    defs = hunt_defs(cfg)
    names: set[str] = set(defs)
    try:
        files = sorted(cfg.hunts_dir.glob("*.y*ml"))
        ok = cfg.hunts_dir.is_dir()
    except OSError:
        return defs, names, False
    for p in files:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            names.add(slug(str(data.get("nome") or p.stem)) if isinstance(data, dict) else slug(p.stem))
        except Exception:
            names.add(slug(p.stem))
    return defs, names, ok


def hunt_state(name: str, defs: dict[str, Hunt], file_names: set[str] | None, dir_ok: bool) -> str:
    """attiva | spenta | eliminata (file YAML rimosso, dati ancora nel DB) | non_valida | ignota."""
    if name in defs:
        return "attiva" if defs[name].attiva else "spenta"
    if file_names is None or not dir_ok:
        return "ignota"          # cartella delle cacce non disponibile: non si puo' dire
    return "non_valida" if name in file_names else "eliminata"


STATE_CHIP = {"spenta": ("spenta", "off"), "eliminata": ("eliminata", "off"), "non_valida": ("file non valido", "off")}
DELETABLE = ("spenta", "eliminata")


def run_status(conn: sqlite3.Connection, hunt: str) -> dict:
    """Stato della riesecuzione: in coda, in corso, oppure libera (con l'esito dell'ultima richiesta)."""
    req = conn.execute("SELECT * FROM run_requests WHERE hunt=? ORDER BY id DESC LIMIT 1", (hunt,)).fetchone()
    running = conn.execute(
        """SELECT started_at FROM runs WHERE hunt=? AND finished_at IS NULL
           AND started_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) ORDER BY id DESC LIMIT 1""",
        (hunt, f"-{STALE_RUN_HOURS} hours")).fetchone()
    if req and req["claimed_at"] is None:
        return {"stato": "coda", "dal": req["requested_at"]}
    if running:
        return {"stato": "corso", "dal": running["started_at"]}
    err = req["error"] if req and req["finished_at"] else None
    return {"stato": "libero", "errore": err, "dal": req["requested_at"] if req else None}


def query_items(conn: sqlite3.Connection, hunt: str, esito: str = "verificati", min_score: int = 0,
                paese: str = "", ordina: str = "punteggio", giorni: int = 0,
                fonte: str = "") -> list[sqlite3.Row]:
    where = ["hunt = ?", "hidden = 1" if esito == "eliminati" else "hidden = 0"]
    args: list = [hunt]
    if fonte in SOURCES:
        where.append("COALESCE(source, 'ebay') = ?")
        args.append(fonte)
    if esito == "verificati":
        where.append("(score IS NOT NULL OR manual_verdict IS NOT NULL)")
    elif esito in MANUAL_VERDICTS:
        where.append(f"{EFF} = ?")
        args.append(esito)
    elif esito == "attesa":
        where.append("score IS NULL AND manual_verdict IS NULL AND (screen_verdict IS NULL OR screen_verdict != 'no')")
    elif esito == "scartati":
        where.append("screen_verdict = 'no' AND manual_verdict IS NULL")
    elif esito == "manuali":
        where.append("manual_verdict IS NOT NULL")
    if min_score and esito not in ("attesa", "scartati", "eliminati"):
        # il punteggio e' del modello; un conforme deciso dall'operatore passa comunque
        where.append("(COALESCE(score, -1) >= ? OR manual_verdict = 'conforme')")
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
    }.get(ordina, f"COALESCE({EFF}, '') = 'non_conforme', COALESCE({EFF}, '') = 'conforme' DESC, "
                  "score IS NULL, score DESC, total ASC")
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
.card{background:var(--card);border:1px solid var(--line);border-radius:10px}
.card h2{font-size:16px;margin:0 0 8px}.nums{display:flex;gap:16px;margin:6px 0}.nums b{display:block;font-size:20px}
.nums span{font-size:12px;color:var(--muted)}.err{color:var(--lo);font-size:13px}
.empty{padding:30px;text-align:center;color:var(--muted)}
.scroll{overflow-x:auto}
.thumbcol{display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.item.manual{border-left:3px solid var(--accent)}
.chip.man{background:var(--accent);color:var(--card)}
.acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
button.act{font-size:12px;padding:3px 9px;background:var(--chip);color:var(--ink);border:1px solid var(--line)}
button.act.on{background:var(--accent);color:var(--card);border-color:var(--accent)}
button.act.danger{color:var(--lo)}
.bulk{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:12px}
.bulk .all{font-size:13px;display:flex;gap:5px;align-items:center}
.bulk input[type=text]{flex:1;min-width:140px}
.flash{background:var(--chip);border-left:3px solid var(--hi);padding:8px 12px;border-radius:6px;margin-bottom:12px}
form.f label.chk{flex-direction:row;align-items:center;gap:6px;font-size:13px}
button:disabled{opacity:.45;cursor:not-allowed}
.chip.run{background:var(--mid);color:var(--card)}
.chip.off{background:var(--line);color:var(--muted)}
.card{padding:0;overflow:hidden}.cardlink{display:block;padding:14px;text-decoration:none;color:inherit}
.card.st-spenta{border-left:5px solid var(--mid);background:color-mix(in srgb,var(--mid) 13%,var(--card))}
.card.st-eliminata{border-left:5px solid var(--lo);background:color-mix(in srgb,var(--lo) 13%,var(--card))}
.card.st-non_valida{border-left:5px solid var(--muted);background:color-mix(in srgb,var(--muted) 12%,var(--card))}
.chip.st-spenta{background:var(--mid);color:var(--card)}.chip.st-eliminata{background:var(--lo);color:var(--card)}
.cardact{padding:0 14px 12px}
a.btn{display:inline-block;font-size:12px;padding:4px 10px;border-radius:6px;text-decoration:none;
border:1px solid var(--lo);color:var(--lo);background:var(--card)}a.btn:hover{background:var(--lo);color:var(--card)}
form.mini{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0}form.mini select{font-size:12px;padding:3px 5px}
form.mini label{font-size:12px;color:var(--muted);display:flex;gap:3px;align-items:center}
form.mini button{font-size:12px;padding:4px 10px}
.cardact{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.cardact form{margin:0}
a.btn.n{color:var(--ink);border-color:var(--line)}a.btn.n:hover{background:var(--chip);color:var(--ink)}
button.btn{font-size:12px;padding:4px 10px;background:var(--card);color:var(--ink);border:1px solid var(--line)}
button.btn:hover{background:var(--chip)}
textarea.code{width:100%;box-sizing:border-box;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;padding:10px;
border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);tab-size:2;white-space:pre}
.err{color:var(--lo)}.flash.bad{border-left-color:var(--lo)}
h2.grp{font-size:15px;margin:22px 0 -6px}
.danger{border:1px solid var(--lo,#c0392b);border-radius:10px;padding:14px;margin:14px 0}
@media (max-width:640px){.item{grid-template-columns:64px 1fr}.thumb{width:64px;height:64px}
.right{grid-column:1/-1;text-align:left;display:flex;gap:12px;align-items:center}}
"""


JS = """<script>
(function(){var all=document.getElementById('selall');if(!all)return;
var boxes=function(){return document.querySelectorAll('input.sel')};
var upd=function(){var n=0;boxes().forEach(function(b){if(b.checked)n++});
document.getElementById('nsel').textContent=n+' selezionat'+(n==1?'o':'i')};
all.addEventListener('change',function(){boxes().forEach(function(b){b.checked=all.checked});upd()});
document.addEventListener('change',function(ev){if(ev.target.classList.contains('sel'))upd()});
document.getElementById('lista').addEventListener('submit',function(ev){
var b=ev.submitter;if(!b||b.name!=='multipla')return;var n=0;boxes().forEach(function(x){if(x.checked)n++});
if(!n){alert('Nessun annuncio selezionato');ev.preventDefault();return}
var a=this.querySelector('select[name=azione_multipla]').value;
if(a==='elimina'&&!confirm('Eliminare '+n+' annunci dalla caccia?'))ev.preventDefault()});})();
</script>"""


def page(title: str, body: str, hunts: list[str], current: str = "", refresh: int = 0) -> str:
    nav = "".join(f'<a href="/caccia/{quote(h)}" class="{"on" if h == current else ""}">{e(h)}</a>' for h in hunts)
    nav += f'<a href="/giri" class="{"on" if current == "__giri" else ""}">Giri</a>'
    meta_refresh = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">{meta_refresh}
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · Scovatore</title>
<style>{CSS}</style></head><body><header><div class="wrap top"><a class="brand" href="/">Scovatore</a>
<nav>{nav}</nav></div></header><main class="wrap">{body}</main></body></html>"""


def all_hunt_names(conn, defs: dict[str, Hunt]) -> list[str]:
    """Cacce con annunci nel DB piu' quelle definite nei YAML che non hanno ancora girato."""
    return sorted({h["hunt"] for h in hunts_overview(conn)} | set(defs))


def render_home(conn, defs: dict[str, Hunt] | None = None, file_names: set[str] | None = None,
                dir_ok: bool = False, msg: str = "", can_edit: bool = False) -> tuple[str, list[str]]:
    defs = defs or {}
    flash = f'<div class="flash">{e(msg)}</div>' if msg else ""
    by_name = {h["hunt"]: h for h in hunts_overview(conn)}
    for n in defs:   # caccia definita ma mai girata: compare lo stesso, con i contatori a zero
        by_name.setdefault(n, {"hunt": n, "totale": 0, "verificati": 0, "conformi": 0, "attesa": 0,
                               "eliminati": 0, "ultimo_nuovo": None, "ultimo_giro": last_run(conn, n)})
    for n in (file_names or ()):   # file YAML rotto e mai girato: va comunque visibile, per poterlo correggere
        by_name.setdefault(n, {"hunt": n, "totale": 0, "verificati": 0, "conformi": 0, "attesa": 0,
                               "eliminati": 0, "ultimo_nuovo": None, "ultimo_giro": last_run(conn, n)})
    hs = [by_name[n] for n in sorted(by_name)]
    names = [h["hunt"] for h in hs]
    if not hs:
        return flash + '<div class="empty">Nessun annuncio nel database: la prima caccia non ha ancora girato.</div>', names
    cards: dict[str, list[str]] = {"attiva": [], "spenta": [], "eliminata": []}
    for h in hs:
        state = hunt_state(h["hunt"], defs, file_names, dir_ok)
        label, cls = STATE_CHIP.get(state, ("", ""))
        state_chip = f'<span class="chip st-{state}">{label}</span>' if label else ""
        fonti = "".join(f'<span class="chip">{e(SOURCE_LABELS[s])}</span>'
                        for s in (defs[h["hunt"]].fonti if h["hunt"] in defs else []))
        st = run_status(conn, h["hunt"])
        stato = {"coda": '<span class="chip run">in coda</span>',
                 "corso": '<span class="chip run">in corso</span>'}.get(st["stato"], "")
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
        q_h = quote(h["hunt"])
        acts = []
        if state in ("attiva", "spenta", "non_valida") and (file_names is not None):
            acts.append(f'<a class="btn n" href="/caccia/{q_h}/file">{"Modifica" if can_edit else "Vedi file"}</a>')
        if can_edit and state in ("attiva", "spenta"):
            nuovo, lab = ("0", "Spegni") if state == "attiva" else ("1", "Accendi")
            acts.append(f'<form method="post" action="/caccia/{q_h}/attiva"><input type="hidden" name="valore" '
                        f'value="{nuovo}"><button class="btn">{lab}</button></form>')
        if state in DELETABLE:
            acts.append(f'<a class="btn" href="/caccia/{q_h}/elimina">Elimina dati...</a>')
        delete = f'<div class="cardact">{"".join(acts)}</div>' if acts else ""
        cards["eliminata" if state == "eliminata" else "spenta" if state in ("spenta", "non_valida") else "attiva"].append(f"""<div class="card st-{state}"><a class="cardlink" href="/caccia/{quote(h['hunt'])}">
<h2>{e(h['hunt'])}</h2><div class="meta" style="margin:-4px 0 6px">{state_chip}{fonti}{stato}</div>
<div class="nums"><div><b class="s-hi">{h['conformi'] or 0}</b><span>conformi</span></div>
<div><b>{h['verificati'] or 0}</b><span>verificati</span></div><div><b>{h['attesa'] or 0}</b><span>in attesa</span></div>
<div><b>{h['totale']}</b><span>visti</span></div></div>
{f'<div class="meta">{h["eliminati"]} eliminati a mano</div>' if h.get("eliminati") else ''}
<div class="meta">{e(run_line)} · ultimo annuncio nuovo {e(ago(h['ultimo_nuovo']))}</div>{err}</a>{delete}</div>""")
    nuova = ('<a class="btn n" href="/nuova">+ Nuova caccia</a>' if file_names is not None and dir_ok else "")
    out = [f"<h1>Cacce</h1><p>{nuova}</p>{flash}"]
    titles = {"attiva": None,
              "spenta": "Spente <span class='meta'>(attiva: false nel file YAML: non girano da sole)</span>",
              "eliminata": "Eliminate <span class='meta'>(file YAML rimosso: restano solo i dati nel database)</span>"}
    for key in ("attiva", "spenta", "eliminata"):
        if cards[key]:
            if titles[key]:
                out.append(f"<h2 class='grp'>{titles[key]}</h2>")
            out.append(f'<div class="cards">{"".join(cards[key])}</div>')
    return "".join(out), names


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


def _btn(azione: str, lid: str, label: str, cls: str = "") -> str:
    confirm = ' onclick="return confirm(\'Eliminare questo annuncio dalla caccia?\')"' if azione == "elimina" else ""
    return (f'<button class="act {cls}" name="azione" value="{e(azione)}:{e(lid)}"{confirm}>'
            f'{e(label)}</button>')


def item_href(r, src: str, link_domain: str) -> str:
    """Link all'annuncio: eBay con il dominio configurato, le altre fonti con il loro URL (solo http/https,
    perche' arriva da dati esterni)."""
    if src == "ebay":
        return f"https://www.{link_domain}/itm/{r['legacy_id']}"
    url = str(r["url"] or "")
    return url if url.startswith(("https://", "http://")) else "#"


def render_item(r: sqlite3.Row, link_domain: str = "ebay.it") -> str:
    keys = r.keys()
    v = json.loads(r["verify_json"] or "{}")
    img = r["image"] if "image" in keys else ""
    thumb = f'<img class="thumb" loading="lazy" src="{e(img)}" alt="">' if img else '<div class="thumb"></div>'
    src = (r["source"] if "source" in keys else None) or "ebay"
    if src == "ebay":
        mp = (r["marketplace"] or "").removeprefix("EBAY_").lower()
        chips = [f"da {r['country'] or '?'}", f"su ebay.{'co.uk' if mp == 'gb' else mp}" if mp else ""]
    else:
        chips = [f"su {SOURCE_LABELS.get(src, src)}"]
    href = item_href(r, src, link_domain)
    if r["is_auction"]:
        chips.append("asta")
    if r["condition"]:
        chips.append(r["condition"])
    chip_html = "".join(f'<span class="chip">{e(c)}</span>' for c in chips if c)
    ship = "spedizione ?" if r["shipping"] is None else f"+ {money(r['shipping'])} sped."
    manual = r["manual_verdict"] if "manual_verdict" in keys else None
    hidden = bool(r["hidden"]) if "hidden" in keys else False
    model_esito = r["verdict"] or ("scartato" if r["screen_verdict"] == "no" else "da verificare")
    esito = (manual or model_esito).replace("_", " ")
    lid = r["legacy_id"]
    sintesi = v.get("sintesi") or (r["screen_reason"] if r["screen_verdict"] == "no" else "")
    detail = _req_rows(v) + _list("Segnali di rischio", v.get("segnali_rischio")) + \
        _list("Domande al venditore", v.get("domande_al_venditore"))
    if r["screen_verdict"]:
        detail += f"<div class='meta' style='margin-top:8px'>Scrematura: {e(r['screen_verdict'])}" + \
            (f", {e(r['screen_reason'])}" if r["screen_reason"] else "") + "</div>"
    if manual:
        detail += (f"<div class='meta' style='margin-top:8px'>Correzione manuale {e(ago(r['manual_at']))}: "
                   f"{e(manual.replace('_', ' '))} (il modello diceva: {e(model_esito.replace('_', ' '))}"
                   f"{'' if r['score'] is None else ', ' + str(r['score'])})"
                   f"{' · nota: ' + e(r['manual_note']) if r['manual_note'] else ''}</div>")
    detail += (f"<div class='meta'>Visto la prima volta {e(ago(r['first_seen']))}, l'ultima {e(ago(r['last_seen']))}"
               f" · venditore {e(r['seller'])} · ID {e(r['legacy_id'])}</div>")
    if hidden:
        actions = _btn("ripristina", lid, "Ripristina")
    else:
        actions = "".join(_btn(a, lid, lbl, "on" if manual == a else "")
                          for a, lbl in (("conforme", "Conforme"), ("incerto", "Incerto"),
                                         ("non_conforme", "Non conforme")))
        if manual:
            actions += _btn("annulla", lid, "Togli correzione")
        actions += _btn("riverifica", lid, "Riverifica") + _btn("elimina", lid, "Elimina", "danger")
    badge = '<span class="chip man">a mano</span>' if manual else ""
    return f"""<div class="item{' manual' if manual else ''}"><div class="thumbcol">
<input type="checkbox" class="sel" name="id" value="{e(lid)}" aria-label="seleziona">{thumb}</div><div>
<a class="title" href="{e(href)}" target="_blank" rel="noopener">{e(r['title'])}</a>
<div class="meta">{chip_html}{' · visto ' + e(ago(r['first_seen']))}</div>
{f'<div class="sintesi">{e(sintesi)}</div>' if sintesi else ''}
<details><summary>Dettagli</summary>{detail}</details><div class="acts">{actions}</div></div>
<div class="right"><div class="score {score_class(r['score'])}">{'-' if r['score'] is None else r['score']}</div>
<div class="price">{money(r['total'], r['currency'])}</div><div class="meta">{money(r['price'])} {e(ship)}</div>
<div class="meta">{badge}{e(esito)}</div></div></div>"""


def rerun_cell(conn, hunt: str, info: Hunt | None, state: str) -> tuple[str, bool]:
    """Pulsante "Riesegui" compatto per la riga di una caccia nella pagina Giri. Ritorna (html, occupata)."""
    if state in ("eliminata", "non_valida", "ignota") or info is None:
        return '<span class="meta">non eseguibile</span>', False
    st = run_status(conn, hunt)
    busy = st["stato"] != "libero"
    fonti = info.fonti
    sel = ""
    if len(fonti) > 1:
        sel = ('<select name="fonte" title="Cosa rieseguire"><option value="">Tutte le fonti</option>' +
               "".join(f'<option value="{s}">{e(SOURCE_LABELS[s])}</option>' for s in fonti) + "</select>")
    if st["stato"] == "coda":
        note = f"in coda, {e(ago(st['dal']))}"
    elif st["stato"] == "corso":
        note = "giro in corso"
    elif st.get("errore"):
        note = f"ultima richiesta fallita: {e(st['errore'][:120])}"
    else:
        note = ""
    form = f"""<form class="mini" method="post" action="/caccia/{quote(hunt)}/riesegui">
<input type="hidden" name="da" value="giri">{sel}
<label title="Rigenera le query con l'LLM"><input type="checkbox" name="rigenera_piano" value="1">query</label>
<button{' disabled' if busy else ''}>Riesegui</button></form>{f'<div class="meta">{note}</div>' if note else ''}"""
    return form, busy


def state_banner(hunt: str, state: str) -> str:
    """Avviso in cima alla pagina di una caccia spenta o eliminata, col link per eliminarne i dati."""
    if state == "spenta":
        txt = "Caccia spenta (<code>attiva: false</code>): non gira da sola, ma si puo' rieseguire a mano dalla pagina <a href='/giri'>Giri</a>."
    elif state == "eliminata":
        txt = "Caccia eliminata: il file YAML non c'e' piu', restano i dati nel database. Non si puo' rieseguire."
    elif state == "non_valida":
        return (f"<div class='flash bad'>Il file YAML di questa caccia non e' valido. "
                f"<a href='/caccia/{quote(hunt)}/file'>Apri il file per correggerlo</a>.</div>")
    elif state == "attiva":
        return f"<div class='flash'><a href='/caccia/{quote(hunt)}/file'>Modifica il file</a> della caccia.</div>"
    else:
        return ""
    link = f' <a href="/caccia/{quote(hunt)}/elimina">Elimina i dati dal database...</a>' if state in DELETABLE else ""
    if state in ("attiva", "spenta"):
        link += f' <a href="/caccia/{quote(hunt)}/file">Modifica il file</a>'
    return f"<div class='flash'>{txt}{link}</div>"


def render_delete(conn, hunt: str, state: str, status: dict, msg: str = "") -> str:
    """Pagina di conferma per eliminare i dati di una caccia spenta o eliminata (mai il file YAML)."""
    h = e(hunt)
    flash = f'<div class="flash">{e(msg)}</div>' if msg else ""
    back = f'<p><a href="/caccia/{quote(hunt)}">Torna alla caccia</a></p>'
    if state not in DELETABLE:
        why = {"attiva": "La caccia e' attiva: spegnila prima (<code>attiva: false</code> nel file YAML).",
               "non_valida": "Il file YAML non e' leggibile, quindi non si sa se la caccia e' spenta.",
               }.get(state, "Non si riesce a leggere la cartella delle cacce, quindi non si sa se la caccia e' spenta.")
        return f"<h1>Elimina i dati di {h}</h1>{flash}<div class='empty'>{why}</div>{back}"
    if status["stato"] != "libero":
        return (f"<h1>Elimina i dati di {h}</h1>{flash}<div class='empty'>C'e' una richiesta in coda o un giro in "
                f"corso: aspetta che finisca.</div>{back}")
    c = {"annunci": conn.execute("SELECT COUNT(*) FROM items WHERE hunt=?", (hunt,)).fetchone()[0],
         "giri": conn.execute("SELECT COUNT(*) FROM runs WHERE hunt=?", (hunt,)).fetchone()[0],
         "piani": conn.execute("SELECT COUNT(*) FROM plans WHERE hunt=?", (hunt,)).fetchone()[0]}
    corretti = conn.execute("SELECT COUNT(*) FROM items WHERE hunt=? AND manual_verdict IS NOT NULL", (hunt,)).fetchone()[0]
    extra = (f" Tra gli annunci ci sono <b>{corretti}</b> corretti a mano: si perdono anche quelli." if corretti else "")
    file_note = ("Il file YAML non c'e' piu'." if state == "eliminata"
                 else "Il file YAML <b>non viene toccato</b>: la caccia resta definita, spenta, e se la riaccendi riparte da zero.")
    return f"""<h1>Elimina i dati di {h}</h1>{flash}
<div class="danger"><p>Stai per cancellare dal database <b>{c['annunci']}</b> annunci, <b>{c['giri']}</b> giri e
<b>{c['piani']}</b> piani di query di <b>{h}</b>.{extra} L'operazione non si puo' annullare.</p>
<p>{file_note}</p>
<form class="f" method="post" action="/caccia/{quote(hunt)}/elimina">
<label>Per confermare scrivi il nome della caccia<input type="text" name="conferma" autocomplete="off"
placeholder="{h}" required></label>
<button>Elimina i dati</button> <a href="/caccia/{quote(hunt)}">Annulla</a></form></div>"""


def delete_hunt_data(cfg: Config, hunt: str, form: dict[str, list[str]], state: str) -> str:
    """Elimina i dati di una caccia spenta o eliminata, previa conferma col nome. Ritorna il messaggio."""
    if state not in DELETABLE:
        return "Eliminazione non consentita: la caccia non risulta spenta ne' eliminata."
    if (form.get("conferma") or [""])[-1].strip() != hunt:
        return "Il nome scritto non corrisponde: non e' stato eliminato nulla."
    db = DB(cfg.db_path)
    try:
        if db.run_in_progress(hunt) or db.request_state(hunt) and db.request_state(hunt)["claimed_at"] is None:
            return "C'e' una richiesta in coda o un giro in corso: non e' stato eliminato nulla."
        n = db.delete_hunt_data(hunt)
    finally:
        db.close()
    log.info("web: dati della caccia %s eliminati: %s", hunt, n)
    return (f"Dati di {hunt} eliminati: {n['annunci']} annunci, {n['giri']} giri, {n['piani']} piani. "
            f"Il file YAML non e' stato toccato.")


def render_hunt(conn, hunt: str, q: dict, link_domain: str = "ebay.it", info: Hunt | None = None,
                state: str = "attiva") -> tuple[str, int]:
    """Pagina della caccia. Ritorna (corpo, secondi di aggiornamento automatico: 0 = nessuno)."""
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
    fonte = q.get("fonte", "") if q.get("fonte") in SOURCES else ""
    rows = query_items(conn, hunt, esito, min_score, paese, ordina, giorni, fonte)

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
<label>Fonte<select name="fonte">{opts(FONTI, fonte)}</select></label>
<label>Visti<select name="giorni">{opts(giorni_opt, str(giorni))}</select></label>
<label>Ordina<select name="ordina">{opts(ORDINI, ordina)}</select></label>
<button>Filtra</button></form>"""
    items = "".join(render_item(x, link_domain) for x in rows) or '<div class="empty">Nessun annuncio con questi filtri.</div>'
    more = f'<p class="meta">Mostrati i primi {LIMIT}.</p>' if len(rows) >= LIMIT else ""
    msg = f'<div class="flash">{e(q["msg"])}</div>' if q.get("msg") else ""
    back = urlencode({k: v for k, v in q.items() if k not in ("msg", "token")})
    bulk_opts = [a for a in AZIONI if (a == "ripristina") == (esito == "eliminati")]
    bulk = f"""<div class="bulk"><label class="all"><input type="checkbox" id="selall"> tutti</label>
<span id="nsel" class="meta">0 selezionati</span>
<select name="azione_multipla">{"".join(f'<option value="{a}">{e(AZIONI[a][0])}</option>' for a in bulk_opts)}</select>
<input type="text" name="nota" placeholder="nota (facoltativa)" maxlength="300">
<button name="multipla" value="1">Applica ai selezionati</button></div>"""
    actions_form = (f'<form method="post" action="/caccia/{quote(hunt)}/azioni" id="lista">'
                    f'<input type="hidden" name="back" value="{e(back)}">{bulk if rows else ""}{items}</form>')
    st = run_status(conn, hunt)
    rerun = ""
    if st["stato"] == "coda":
        rerun = f"<div class='flash'>Riesecuzione richiesta {e(ago(st['dal']))}, in coda.</div>"
    elif st["stato"] == "corso":
        rerun = f"<div class='flash'>Giro in corso da {e(ago(st['dal']).replace(' fa', ''))}.</div>"
    fonti = "".join(f'<span class="chip">{e(SOURCE_LABELS[s])}</span>' for s in (info.fonti if info else []))
    body = (f"<h1>{e(hunt)}</h1><p class='sub'>{fonti} {e(sub)} · {len(rows)} annunci</p>{msg}{state_banner(hunt, state)}{rerun}{form}"
            f"{actions_form}{more}{JS}")
    # mentre c'e' una richiesta in coda o un giro in corso la pagina si aggiorna da sola
    return body, (15 if st["stato"] != "libero" else 0)


def render_file_page(cfg: Config, name: str, text: str, base: str, *, is_new: bool = False, msg: str = "",
                     error: str = "", loaded: str = "") -> str:
    """Pagina di visualizzazione e modifica del file YAML di una caccia (o di una nuova)."""
    can_edit = bool(cfg.web_token)
    summary = ""
    try:
        h = huntfiles.parse_text(huntfiles.normalize(text), cfg.hunts_dir,
                                 None if is_new else huntfiles.find_file(cfg.hunts_dir, name))
        lo, hi = h.price_limits("ebay")
        chips = "".join(f'<span class="chip">{e(SOURCE_LABELS[s])}</span>' for s in h.fonti)
        summary = (f"<p class='sub'>{'attiva' if h.attiva else 'spenta'} · ogni {h.ogni_minuti} minuti · {chips} · "
                   f"eBay: {e(', '.join(h.ebay.marketplaces))}, prezzo {'' if lo is None else lo}..{'' if hi is None else hi} · "
                   f"lingue delle query: {e(', '.join(h.languages()))}</p>")
    except huntfiles.HuntFileError as exc:
        if not error:
            summary = f"<div class='flash bad'>Il file attuale non e' valido: {e(exc)}</div>"
    flash = f'<div class="flash">{e(msg)}</div>' if msg else ""
    if loaded:
        flash += f'<div class="flash">Caricata la versione del {e(loaded)}: non e\' ancora salvata.</div>'
    if error:
        flash += f'<div class="flash bad"><b>Non salvato.</b> {e(error)}</div>'
    if not can_edit:
        flash += ('<div class="flash bad">Modifica disabilitata: imposta <code>SCOVATORE_WEB_TOKEN</code> nel .env '
                  'e riavvia il servizio web. Finche\' non c\'e\' un token si puo\' solo leggere il file.</div>')
    action = "/nuova" if is_new else f"/caccia/{quote(name)}/file"
    versions = ""
    if not is_new:
        vs = huntfiles.backups(cfg.hunts_dir, name)
        if vs:
            versions = ("<details><summary>Versioni precedenti (" + str(len(vs)) + ")</summary><ul class='small'>" +
                        "".join(f'<li><a href="/caccia/{quote(name)}/file?versione={quote(f)}">{e(d)}</a></li>'
                                for f, d in vs) + "</ul><p class='meta'>Cliccando una versione la carichi nell'editor "
                        "senza salvarla.</p></details>")
    title = "Nuova caccia" if is_new else f"File di {e(name)}"
    ro = "" if can_edit else " readonly"
    back = "/" if is_new else f"/caccia/{quote(name)}"
    return f"""<h1>{title}</h1>{summary}{flash}
<form method="post" action="{action}"><input type="hidden" name="base" value="{e(base)}">
<textarea class="code" name="testo" rows="34" spellcheck="false"{ro}>{e(text)}</textarea>
<p><button{'' if can_edit else ' disabled'}>Salva</button> <a href="{back}">Torna indietro</a>
<span class="meta"> Prima del salvataggio il file viene controllato; la versione precedente resta in cacce/.storico.</span></p>
</form>{versions}"""


def save_hunt_file(cfg: Config, name: str, form: dict[str, list[str]]) -> tuple[str, str]:
    """Salva il file di una caccia. Ritorna (messaggio, errore): uno dei due e' vuoto."""
    if not cfg.web_token:
        return "", "Modifica disabilitata: manca SCOVATORE_WEB_TOKEN."
    text = (form.get("testo") or [""])[-1]
    base = (form.get("base") or [""])[-1]
    try:
        h, changed = huntfiles.save(cfg.hunts_dir, name, text, base)
    except huntfiles.HuntFileError as exc:
        return "", str(exc)
    log.info("web: file della caccia %s %s", name, "salvato" if changed else "invariato")
    return ("File salvato: vale dal prossimo giro." if changed else "Nessuna modifica da salvare."), ""


def create_hunt_file(cfg: Config, form: dict[str, list[str]]) -> tuple[str, str, str]:
    """Crea una nuova caccia. Ritorna (nome, messaggio, errore)."""
    if not cfg.web_token:
        return "", "", "Modifica disabilitata: manca SCOVATORE_WEB_TOKEN."
    try:
        h = huntfiles.create(cfg.hunts_dir, (form.get("testo") or [""])[-1])
    except huntfiles.HuntFileError as exc:
        return "", "", str(exc)
    log.info("web: creata la caccia %s", h.nome)
    return h.nome, f"Caccia {h.nome} creata.", ""


def toggle_hunt(cfg: Config, name: str, form: dict[str, list[str]]) -> str:
    """Accende o spegne una caccia cambiando solo `attiva:` nel suo file."""
    if not cfg.web_token:
        return "Modifica disabilitata: manca SCOVATORE_WEB_TOKEN."
    on = (form.get("valore") or [""])[-1] == "1"
    try:
        changed = huntfiles.set_active(cfg.hunts_dir, name, on)
    except huntfiles.HuntFileError as exc:
        return f"Non modificata: {exc}"
    log.info("web: caccia %s %s", name, "accesa" if on else "spenta")
    return f"Caccia {name} {'accesa' if on else 'spenta'}." if changed else f"La caccia {name} era gia' cosi'."


def render_runs(conn, defs: dict[str, Hunt] | None = None, file_names: set[str] | None = None,
                dir_ok: bool = False, msg: str = "") -> tuple[str, int]:
    """Pagina Giri: gli ultimi giri di tutte le cacce. Sulla riga piu' recente di ogni caccia c'e' il
    pulsante "Riesegui". Ritorna (corpo, secondi di aggiornamento automatico)."""
    defs = defs or {}
    flash = f'<div class="flash">{e(msg)}</div>' if msg else ""
    runs = recent_runs(conn)
    seen: set[str] = set()
    busy_any = False
    rows = []

    def action(hunt: str) -> str:
        nonlocal busy_any
        cell, busy = rerun_cell(conn, hunt, defs.get(hunt), hunt_state(hunt, defs, file_names, dir_ok))
        busy_any = busy_any or busy
        return cell

    for r in runs:
        st = r["stats"]
        dur = st.get("durate", {}).get("totale")
        stato = "in corso" if not r["finished_at"] else ("errore" if r["error"] else "ok")
        cls = {"errore": "ko", "in corso": "dub"}.get(stato, "ok")
        fasi = ", ".join(f"{k} {v:.0f}s" for k, v in (st.get("durate") or {}).items() if k != "totale")
        errs = st.get("errori") or []
        err_html = f"<details><summary>{len(errs)} errori</summary><ul class='small'>" + \
            "".join(f"<li>{e(x[:300])}</li>" for x in errs) + "</ul></details>" if errs else ""
        first = r["hunt"] not in seen            # i giri arrivano dal piu' recente: il primo di ogni caccia
        seen.add(r["hunt"])
        rows.append(f"""<tr><td>{r['id']}</td><td>{e(r['hunt'])}</td><td>{e(ago(r['started_at']))}</td>
<td class="{cls}">{stato}</td><td>{'' if dur is None else f'{dur:.0f} s'}<div class="meta">{e(fasi)}</div></td>
<td>{st.get('query', '')}</td><td>{st.get('unici', '')}</td><td>{st.get('scartati_filtri', '')}</td>
<td>{st.get('nuovi', '')}</td><td>{st.get('scremati', '')}</td><td>{st.get('verificati', '')}</td>
<td>{st.get('conformi', '')}</td><td>{st.get('chiamate_ebay', '')}</td><td>{err_html}</td>
<td>{action(r['hunt']) if first else ''}</td></tr>""")
    for name in sorted(defs):                    # cacce che non hanno giri nell'elenco: si possono comunque avviare
        if name not in seen:
            rows.append(f"""<tr><td></td><td>{e(name)}</td><td colspan="12" class="meta">nessun giro recente</td>
<td>{action(name)}</td></tr>""")
    if not rows:
        return flash + '<div class="empty">Nessun giro registrato.</div>', 0
    body = f"""<h1>Giri recenti</h1>{flash}<p class="sub">Ultimi {len(runs)} giri di tutte le cacce. "Riesegui" sulla riga
piu' recente di una caccia la fa partire subito, senza aspettare l'intervallo (anche se e' spenta). Parte entro
{POLL_HINT} secondi se il servizio e' libero.</p>
<div class="scroll"><table><tr><th>#</th><th>Caccia</th><th>Avvio</th><th>Stato</th><th>Durata</th><th>Ricerche</th>
<th>Unici</th><th>Scartati</th><th>Nuovi</th><th>Scremati</th><th>Verificati</th><th>Conformi</th><th>eBay</th>
<th>Errori</th><th>Azione</th></tr>{''.join(rows)}</table></div>"""
    return body, (15 if busy_any else 0)


def apply_action(db_path: Path, hunt: str, form: dict[str, list[str]]) -> str:
    """Esegue un'azione dell'operatore e restituisce il messaggio da mostrare."""
    single = (form.get("azione") or [""])[-1]
    if single and ":" in single:
        azione, lid = single.split(":", 1)
        ids = [lid]
    else:
        azione = (form.get("azione_multipla") or [""])[-1]
        ids = form.get("id") or []
    # ID eBay numerici, oppure "fonte:numero" per Vinted e Subito: tutto il resto e' scartato
    ids = [i for i in dict.fromkeys(ids) if ITEM_ID.fullmatch(i)][:1000]
    if azione not in AZIONI:
        return "Azione non riconosciuta."
    if not ids:
        return "Nessun annuncio selezionato."
    note = (form.get("nota") or [""])[-1].strip()[:300]
    db = DB(db_path)
    try:
        if azione in MANUAL_VERDICTS:
            n = db.set_manual(hunt, ids, azione, note)
        elif azione == "annulla":
            n = db.set_manual(hunt, ids, None)
        elif azione == "riverifica":
            n = db.requeue(hunt, ids)
        else:
            n = db.set_hidden(hunt, ids, azione == "elimina")
    finally:
        db.close()
    log.info("web: %s su %d annunci di %s (%s)%s", azione, n, hunt, ",".join(ids[:10]),
             f", nota: {note}" if note else "")
    return f"{n} annuncio {AZIONI[azione][1]}." if n == 1 else f"{n} annunci {AZIONI[azione][2]}."


def request_rerun(cfg: Config, hunt: str, form: dict[str, list[str]], names: list[str],
                  defs: dict[str, Hunt]) -> str:
    """Mette in coda la riesecuzione di una caccia (la esegue il loop) e ritorna il messaggio da mostrare."""
    if hunt not in names:
        return "Caccia sconosciuta."
    fonte = (form.get("fonte") or [""])[-1].strip().lower()
    if fonte and fonte not in SOURCES:
        return "Fonte non riconosciuta."
    if fonte and hunt in defs and fonte not in defs[hunt].fonti:
        return (f"La fonte {SOURCE_LABELS[fonte]} non e' attiva in questa caccia: "
                f"aggiungila al campo `fonti` del file YAML.")
    rigenera = bool((form.get("rigenera_piano") or [""])[-1])
    db = DB(cfg.db_path)
    try:
        rid = db.request_run(hunt, rigenera, [fonte] if fonte else None)
    finally:
        db.close()
    log.info("web: riesecuzione di %s richiesta (fonte %s%s)", hunt, fonte or "tutte",
             ", piano rigenerato" if rigenera else "")
    if rid is None:
        return "C'e' gia' una richiesta in coda per questa caccia."
    return f"Riesecuzione messa in coda: parte entro {POLL_HINT} secondi se il servizio e' libero."


def api_items(conn, hunt: str, q: dict, link_domain: str = "ebay.it") -> list[dict]:
    rows = query_items(conn, hunt, q.get("esito", "verificati"), int(q.get("min") or 0),
                       (q.get("paese") or "").upper(), q.get("ordina", "punteggio"), int(q.get("giorni") or 0),
                       q.get("fonte", ""))
    out = []
    for r in rows:
        d = dict(r)
        d["link"] = item_href(r, (r["source"] or "ebay"), link_domain)
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

        def _same_origin(self) -> bool:
            """Blocca form inviati da altri siti (CSRF): Origin o Referer devono puntare a questo host."""
            src = self.headers.get("Origin") or self.headers.get("Referer")
            if not src:
                return True
            return urlparse(src).netloc == self.headers.get("Host", "")

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authorized({}) or not self._same_origin():
                return self._send(403, page("Accesso", '<div class="empty">Azione non consentita.</div>', []))
            parts = [p for p in u.path.split("/") if p]
            is_new = parts == ["nuova"]
            if not is_new and (len(parts) != 3 or parts[0] != "caccia" or
                               parts[2] not in ("azioni", "riesegui", "elimina", "file", "attiva")):
                return self._send(404, "non trovato", "text/plain")
            hunt = "" if is_new else parts[1]
            length = min(int(self.headers.get("Content-Length") or 0), 500_000)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
            try:
                if is_new:
                    nome, msg, err = create_hunt_file(cfg, form)
                    if err:       # resta nell'editor con il testo scritto
                        return self._send(400, page("Nuova caccia", render_file_page(
                            cfg, "", (form.get("testo") or [""])[-1], "", is_new=True, error=err), []))
                    return self._send(303, "", extra={"Location": f"/caccia/{quote(nome)}/file?" + urlencode({"msg": msg})})
                if parts[2] == "file":
                    msg, err = save_hunt_file(cfg, hunt, form)
                    if err:
                        return self._send(400, page("Modifica", render_file_page(
                            cfg, hunt, (form.get("testo") or [""])[-1], (form.get("base") or [""])[-1], error=err), [], hunt))
                    return self._send(303, "", extra={"Location": f"/caccia/{quote(hunt)}/file?" + urlencode({"msg": msg})})
                if parts[2] == "attiva":
                    return self._send(303, "", extra={"Location": "/?" + urlencode({"msg": toggle_hunt(cfg, hunt, form)})})
                if parts[2] == "riesegui":
                    defs = hunt_defs(cfg)
                    conn = connect(cfg.db_path)
                    try:
                        names = all_hunt_names(conn, defs) if conn else sorted(defs)
                    finally:
                        if conn:
                            conn.close()
                    msg = request_rerun(cfg, hunt, form, names, defs)
                elif parts[2] == "elimina":
                    defs, fnames, dir_ok = hunt_files(cfg)
                    conn = connect(cfg.db_path)
                    try:
                        known = hunt in all_hunt_names(conn, defs) if conn else False
                    finally:
                        if conn:
                            conn.close()
                    if not known:
                        msg = "Caccia sconosciuta."
                    else:
                        msg = delete_hunt_data(cfg, hunt, form, hunt_state(hunt, defs, fnames, dir_ok))
                    if msg.startswith("Dati di"):
                        return self._send(303, "", extra={"Location": "/?" + urlencode({"msg": msg})})
                    return self._send(303, "", extra={"Location": f"/caccia/{quote(hunt)}/elimina?" +
                                                      urlencode({"msg": msg})})
                else:
                    msg = apply_action(cfg.db_path, hunt, form)
            except Exception as exc:
                log.exception("azione web fallita su %s", hunt)
                msg = f"Errore: {exc}"
            if (form.get("da") or [""])[-1] == "giri":
                return self._send(303, "", extra={"Location": "/giri?" + urlencode({"msg": msg})})
            back = dict(parse_qs((form.get("back") or [""])[-1]))
            q = {k: v[-1] for k, v in back.items()}
            q["msg"] = msg
            return self._send(303, "", extra={"Location": f"/caccia/{quote(hunt)}?{urlencode(q)}"})

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
            if conn is None:       # nessun giro ancora fatto: pagine come se il database fosse vuoto
                conn = DB(Path(":memory:")).conn
            try:
                defs, fnames, dir_ok = hunt_files(cfg)
                names = all_hunt_names(conn, defs)
                parts = [p for p in u.path.split("/") if p]
                if not parts:
                    body, names = render_home(conn, defs, fnames, dir_ok, q.get("msg", ""), bool(cfg.web_token))
                    return self._send(200, page("Cacce", body, names))
                if parts == ["nuova"]:
                    return self._send(200, page("Nuova caccia", render_file_page(
                        cfg, "", huntfiles.template(cfg.hunts_dir), "", is_new=True, msg=q.get("msg", "")), names))
                if len(parts) == 3 and parts[0] == "caccia" and parts[2] == "file":
                    try:
                        _, current = huntfiles.read(cfg.hunts_dir, parts[1])
                    except huntfiles.HuntFileError:
                        return self._send(404, page("Non trovato", '<div class="empty">File della caccia non '
                                                    'trovato (e\' una caccia eliminata?).</div>', names))
                    text, loaded = current, ""
                    if q.get("versione"):
                        try:
                            text = huntfiles.read_backup(cfg.hunts_dir, parts[1], q["versione"])
                            loaded = dict(huntfiles.backups(cfg.hunts_dir, parts[1])).get(q["versione"], q["versione"])
                        except huntfiles.HuntFileError:
                            pass
                    return self._send(200, page("File " + parts[1], render_file_page(
                        cfg, parts[1], text, huntfiles.digest(current), msg=q.get("msg", ""), loaded=loaded),
                        names, parts[1]))
                if len(parts) == 3 and parts[0] == "caccia" and parts[2] == "elimina" and parts[1] in names:
                    st = hunt_state(parts[1], defs, fnames, dir_ok)
                    return self._send(200, page("Elimina i dati", render_delete(
                        conn, parts[1], st, run_status(conn, parts[1]), q.get("msg", "")), names, parts[1]))
                if parts == ["giri"]:
                    body, refresh = render_runs(conn, defs, fnames, dir_ok, q.get("msg", ""))
                    return self._send(200, page("Giri", body, names, "__giri", refresh))
                if len(parts) == 2 and parts[0] == "caccia" and parts[1] in names:
                    body, refresh = render_hunt(conn, parts[1], q, cfg.link_domain, defs.get(parts[1]),
                                                hunt_state(parts[1], defs, fnames, dir_ok))
                    return self._send(200, page(parts[1], body, names, parts[1], refresh))
                if len(parts) == 3 and parts[:2] == ["api", "caccia"] and parts[2] in names:
                    return self._send(200, json.dumps(api_items(conn, parts[2], q, cfg.link_domain), ensure_ascii=False),
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
    DB(cfg.db_path).close()   # crea il DB o aggiunge le colonne nuove prima di servire le pagine
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    log.info("interfaccia web su http://%s:%d (DB %s, token %s)", host, port, cfg.db_path,
             "attivo" if cfg.web_token else "disattivato")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
