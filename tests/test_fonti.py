"""Test delle fonti Vinted e Subito, della scelta delle fonti nella caccia e della riesecuzione dal web.

Come negli altri test, nessun servizio esterno: i siti sono simulati con httpx.MockTransport.
Le risposte simulate seguono la struttura che i due siti usano oggi (da verificare dal vivo: vedi README).
"""
import json
import threading
from dataclasses import replace
from pathlib import Path
from http.server import ThreadingHTTPServer

import httpx
import pytest
import yaml

from scovatore import cli, pipeline
from scovatore.db import DB
from scovatore.hunt import HuntError, parse_hunt
from scovatore.pipeline import local_reject_reason, plan_note, run_hunt
from scovatore.sources import SourceError, SubitoSource, VintedSource
from scovatore.web import make_handler

from test_scovatore import FakeEbay, FakeLLM, env, make_clients  # noqa: F401  (env e' una fixture)


# ---------------------------------------------------------------- finti Vinted e Subito
def vinted_item(iid, title, price, total=None, **kw):
    d = {"id": iid, "title": title, "price": {"amount": str(price), "currency_code": "EUR"},
         "url": f"https://www.vinted.it/items/{iid}-x", "photo": {"url": f"https://img.example/{iid}.jpg"},
         "user": {"login": "marco", "feedback_reputation": 0.98, "feedback_count": 12},
         "status": "Buone condizioni", "brand_title": "Marantz", "size_title": ""}
    if total is not None:
        d["total_item_price"] = {"amount": str(total), "currency_code": "EUR"}
    d.update(kw)
    return d


VINTED_CATALOG = [
    vinted_item(111, "Amplificatore Marantz PM-40 non funzionante", 40, total=42.45),
    vinted_item(112, "Cover amplificatore in stoffa", 5, price_old=None),      # la parola "cover" e' esclusa
    vinted_item(113, "Amplificatore Pioneer A-757 per ricambi", 500),          # oltre budget
]


class FakeVinted:
    def __init__(self, items=None, home_status=200, cookie=True, expire_first=False):
        self.items = VINTED_CATALOG if items is None else items
        self.home_status, self.cookie, self.expire_first = home_status, cookie, expire_first
        self.requests = []          # (path, query)
        self.homes = 0
        self.pages = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append((req.url.path, str(req.url.query, "ascii")))
        if req.url.path == "/":
            self.homes += 1
            if self.home_status != 200:
                return httpx.Response(self.home_status, text="blocked")
            headers = {"set-cookie": f"access_token_web=tok{self.homes}; Path=/"} if self.cookie else {}
            return httpx.Response(200, text="<html></html>", headers=headers)
        if req.url.path == "/api/v2/catalog/items":
            if "access_token_web" not in req.headers.get("cookie", ""):
                return httpx.Response(401, json={"message": "unauthorized"})
            if self.expire_first and self.homes == 1:        # il primo cookie risulta scaduto
                return httpx.Response(401, json={"message": "expired"})
            return httpx.Response(200, json={"items": self.items, "pagination": {}})
        if req.url.path.startswith("/items/"):
            self.pages += 1
            ld = json.dumps({"@type": "Product", "description": "Si accende ma non da' suono, vendo per ricambi"})
            return httpx.Response(200, text=f'<html><head><script type="application/ld+json">{ld}</script></head></html>')
        return httpx.Response(404)


def subito_ad(aid, title, price, **kw):
    feats = {"/item_condition": {"label": "Condizione", "values": [{"key": "u", "value": "Usato"}]}}
    if price is not None:
        feats["/price"] = {"label": "Prezzo", "values": [{"key": str(price), "value": f"{price} €"}]}
    d = {"urn": f"id:ad:{aid}:list:99", "subject": title, "body": "Non si accende, fusibile ok.",
         "urls": {"default": f"https://www.subito.it/audio-video/{title.lower().replace(' ', '-')}-torino-{aid}.htm"},
         "features": feats, "geo": {"city": {"value": "Torino"}, "region": {"value": "Piemonte"}},
         "advertiser": {"name": "Gianni", "company": False}, "category": {"label": "Audio/Video"},
         "images": [{"scale": [{"uri": f"https://images.subito.it/{aid}.jpg"}]}]}
    d.update(kw)
    return d


SUBITO_ADS = [
    subito_ad(555, "Amplificatore Sansui AU-217 guasto", 80),
    subito_ad(556, "Amplificatore senza prezzo", None),       # prezzo assente: saltato
    subito_ad(557, "Amplificatore Technics SU-V4 rotto", 120),  # oltre budget
]


def subito_page(ads, wrap=True):
    items = [{"type": "item", "item": a} for a in ads] if wrap else ads
    data = {"props": {"pageProps": {"initialState": {"items": {"list": items}}}}}
    return ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data) + "</script></body></html>")


class FakeSubito:
    def __init__(self, ads=None, status=200, text=None):
        self.ads = SUBITO_ADS if ads is None else ads
        self.status, self.text = status, text
        self.requests = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append((req.url.path, str(req.url.query, "ascii")))
        if self.status != 200:
            return httpx.Response(self.status, text="denied")
        if req.url.path.startswith("/annunci-"):
            return httpx.Response(200, text=self.text if self.text is not None else subito_page(self.ads))
        if req.url.path.endswith(".htm"):
            detail = subito_ad(555, "Amplificatore Sansui AU-217 guasto", 80, body="<p>Ronzio forte, poi si spegne</p>")
            detail["features"]["/brand"] = {"label": "Marca", "values": [{"value": "Sansui"}]}
            return httpx.Response(200, text=subito_page([detail], wrap=False))
        return httpx.Response(404)


def vinted_src(hunt, fake):
    return VintedSource(hunt.vinted, "test-agent", 0, httpx.MockTransport(fake))


def subito_src(hunt, fake):
    return SubitoSource(hunt.subito, "test-agent", 0, httpx.MockTransport(fake))


MULTI = {
    "nome": "Multi", "ricerca": "amplificatore hi-fi guasto", "fonti": ["ebay", "vinted", "subito"],
    "parole_escluse": ["cover"],
    "ebay": {"marketplaces": ["EBAY_IT"], "prezzo_max": 100, "condizioni": ["guasto"]},
    "vinted": {"max_ricerche": 4}, "subito": {"regione": "Piemonte"},
}


class SpyLLM(FakeLLM):
    """Come FakeLLM, ma tiene i testi inviati alla verifica."""
    def __init__(self):
        super().__init__()
        self.verify_prompts = []

    def __call__(self, req):
        body = json.loads(req.content)
        if body["messages"][0]["content"].startswith("Sei un tecnico"):
            user = body["messages"][1]["content"]
            self.verify_prompts.append(user)
            if "Marantz" in user:
                self.calls["verify"] += 1
                content = json.dumps({"oggetto": "ampli", "esito": "conforme", "punteggio": 90, "requisiti": [],
                                      "segnali_rischio": [], "sintesi": "guasto di alimentazione probabile"})
                return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                                 "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        return super().__call__(req)


def run_multi(env, hunt_data=None, vinted=None, subito=None, ebay_fake=None, only=None, with_ebay=True):
    hunt = parse_hunt(hunt_data or MULTI)
    llm = SpyLLM()
    ef = ebay_fake or FakeEbay()
    ebay, pal, groq = make_clients(env, ef, llm)
    fv, fs = vinted or FakeVinted(), subito or FakeSubito()
    sources = {}
    if hunt.uses("vinted"):
        sources["vinted"] = vinted_src(hunt, fv)
    if hunt.uses("subito"):
        sources["subito"] = subito_src(hunt, fs)
    db = DB(env.db_path)
    stats = run_hunt(hunt, env, db, ebay if with_ebay else None, pal, groq, sources=sources, only=only)
    return hunt, db, stats, SimpleNS(llm=llm, ebay=ef, vinted=fv, subito=fs)


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------- schema della caccia
def test_fonti_default_ed_elenco():
    assert parse_hunt({"ricerca": "x"}).fonti == ["ebay"]                       # le cacce esistenti non cambiano
    h = parse_hunt({"ricerca": "x", "fonti": ["Subito.it", "vinted", "subito", "eBay"]})
    assert h.fonti == ["subito", "vinted", "ebay"] and h.uses("vinted")
    assert parse_hunt({"ricerca": "x", "fonti": "vinted"}).fonti == ["vinted"]
    with pytest.raises(HuntError):
        parse_hunt({"ricerca": "x", "fonti": ["wallapop"]})
    with pytest.raises(HuntError):
        parse_hunt({"ricerca": "x", "fonti": []})


def test_sezioni_vinted_e_subito():
    h = parse_hunt({"ricerca": "x", "fonti": ["vinted", "subito"], "ebay": {"prezzo_max": 90},
                    "vinted": {"domini": ["it", "vinted.fr"], "condizioni": ["nuovo", 2], "spedizione_stimata": 4.5},
                    "subito": {"regione": "Valle d'Aosta", "categoria": "Audio-Video", "prezzo_min": 10}})
    assert h.vinted.domini == ["it", "fr"] and h.vinted.condizioni == [1, 2, 6] and h.vinted.spedizione_stimata == 4.5
    assert h.subito.regione == "valle-d-aosta" and h.subito.categoria == "audio-video"
    assert h.price_limits("vinted") == (None, 90)       # senza prezzo proprio vale quello della sezione ebay
    assert h.price_limits("subito") == (10, 90)
    assert "it" in h.languages() and "fr" in h.languages()
    for bad in ({"vinted": {"domini": ["xx"]}}, {"vinted": {"colore": 1}}, {"subito": {"ordinamento": "caso"}},
                {"vinted": {"ordinamento": "caso"}}, {"subito": {"prezzo_min": 9, "prezzo_max": 1}},
                {"vinted": {"condizioni": ["rotto"]}}, {"subito": "piemonte"}):
        with pytest.raises(HuntError):
            parse_hunt({"ricerca": "x", **bad})


def test_spedizione_max_ebay():
    from scovatore.ebay import parse_summary
    from test_scovatore import summary
    h = parse_hunt({"ricerca": "x", "ebay": {"prezzo_max": 500, "spedizione_max": 15}})
    ok = parse_summary(summary(1, "ampli", 50, ship=15), "EBAY_IT", "q")
    caro = parse_summary(summary(2, "ampli", 50, ship=15.5), "EBAY_IT", "q")
    assert local_reject_reason(ok, h, []) is None
    assert "oltre il massimo" in local_reject_reason(caro, h, [])
    with pytest.raises(HuntError):
        parse_hunt({"ricerca": "x", "ebay": {"spedizione_max": -1}})


# ---------------------------------------------------------------- Vinted
def test_vinted_search_cookie_prezzi_e_parametri():
    h = parse_hunt({**MULTI, "vinted": {"max_ricerche": 4, "condizioni": ["ottimo", "buono"], "categorie": [2050]}})
    fake = FakeVinted()
    src = vinted_src(h, fake)
    found = src.search("amplificatore", "it", h)
    assert fake.homes == 1                                   # sessione aperta una volta sola
    assert [l.legacy_id for l in found] == ["vinted:111", "vinted:112", "vinted:113"]
    a = found[0]
    assert a.source == "vinted" and a.price == 42.45 and a.currency == "EUR"   # totale con protezione acquirenti
    assert a.shipping is None and a.url == "https://www.vinted.it/items/111-x"
    assert a.seller_feedback_pct == pytest.approx(98) and a.aspects["Marca"] == "Marantz"
    path, query = fake.requests[-1]
    assert path == "/api/v2/catalog/items"
    for part in ("search_text=amplificatore", "price_to=100", "order=newest_first", "status_ids%5B%5D=2",
                 "status_ids%5B%5D=3", "catalog%5B%5D=2050"):
        assert part in query, part
    src.search("amplificatore", "it", h)
    assert fake.homes == 1                                   # il cookie si riusa


def test_vinted_spedizione_stimata_nel_totale():
    h = parse_hunt({**MULTI, "vinted": {"spedizione_stimata": 5}})
    found = vinted_src(h, FakeVinted([vinted_item(1, "ampli", 40)])).search("ampli", "it", h)
    assert found[0].shipping == 5 and found[0].total == 45


def test_vinted_rinnova_sessione_su_401():
    h = parse_hunt(MULTI)
    fake = FakeVinted(expire_first=True)
    found = vinted_src(h, fake).search("amplificatore", "it", h)
    assert fake.homes == 2 and len(found) == 3


def test_vinted_blocco_e_formato_cambiato():
    h = parse_hunt(MULTI)
    with pytest.raises(SourceError) as e:
        vinted_src(h, FakeVinted(home_status=403)).search("x", "it", h)
    assert e.value.blocked and "anti-bot" in str(e.value)
    # risposta valida ma senza "items": formato cambiato, non "nessun risultato"
    src = vinted_src(h, FakeVinted(items=None))
    src.http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"results": []}, headers={"set-cookie": "access_token_web=t; Path=/"})))
    with pytest.raises(SourceError, match="formato cambiato"):
        src.search("x", "it", h)


def test_vinted_enrich_legge_la_pagina_pubblica():
    h = parse_hunt(MULTI)
    fake = FakeVinted()
    src = vinted_src(h, fake)
    l = src.search("amplificatore", "it", h)[0]
    src.enrich(l, 2500)
    assert "ricambi" in l.description and fake.pages == 1


# ---------------------------------------------------------------- Subito
def test_subito_search_parsing_e_url():
    h = parse_hunt(MULTI)
    fake = FakeSubito()
    found = subito_src(h, fake).search("amplificatore", "piemonte", h)
    assert [l.legacy_id for l in found] == ["subito:555", "subito:557"]      # senza prezzo: saltato
    a = found[0]
    assert a.source == "subito" and a.price == 80 and a.total == 80 and a.country == "IT"
    assert a.condition == "Usato" and a.aspects["Luogo"] == "Torino (Piemonte)" and a.seller == "Gianni"
    assert a.image == "https://images.subito.it/555.jpg" and a.description.startswith("Non si accende")
    path, query = fake.requests[0]
    assert path == "/annunci-piemonte/vendita/usato/" and "q=amplificatore" in query and "order=datedesc" in query
    assert "ps=" not in query and "pe=" not in query          # il prezzo si filtra solo in locale


def test_subito_trova_gli_annunci_anche_se_la_struttura_si_sposta():
    h = parse_hunt(MULTI)
    page = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
        {"altrove": {"profondo": [subito_ad(777, "Amplificatore Rotel", 60)]}}) + "</script>"
    found = subito_src(h, FakeSubito(text=page)).search("x", "italia", h)
    assert [l.legacy_id for l in found] == ["subito:777"]


def test_subito_errori():
    h = parse_hunt(MULTI)
    with pytest.raises(SourceError) as e:
        subito_src(h, FakeSubito(status=403)).search("x", "italia", h)
    assert e.value.blocked
    with pytest.raises(SourceError, match="manca il JSON"):
        subito_src(h, FakeSubito(text="<html>pagina qualunque</html>")).search("x", "italia", h)
    with pytest.raises(SourceError, match="controlla regione"):
        subito_src(h, FakeSubito(status=404)).search("x", "regione-sbagliata", h)
    # annunci presenti nella pagina ma illeggibili: errore, non elenco vuoto
    rotta = ('<script id="__NEXT_DATA__" type="application/json">{"a":1}</script>'
             '<div>"subject": "x"</div>')
    with pytest.raises(SourceError, match="formato cambiato"):
        subito_src(h, FakeSubito(text=rotta)).search("x", "italia", h)
    # pagina valida senza risultati: elenco vuoto, nessun errore
    assert subito_src(h, FakeSubito(ads=[])).search("x", "italia", h) == []


def test_subito_enrich_descrizione_e_caratteristiche():
    h = parse_hunt(MULTI)
    src = subito_src(h, FakeSubito())
    l = src.search("amplificatore", "italia", h)[0]
    src.enrich(l, 2500)
    assert l.description == "Ronzio forte, poi si spegne" and l.aspects["Marca"] == "Sansui"


# ---------------------------------------------------------------- pipeline a piu' fonti
def test_pipeline_tre_fonti(env):
    hunt, db, s, x = run_multi(env)
    assert s.fonti == ["ebay", "vinted", "subito"]
    ids = {r["legacy_id"]: r for r in db.conn.execute("SELECT * FROM items WHERE hunt=?", (hunt.nome,))}
    assert {"vinted:111", "subito:555"} <= set(ids) and "1" in ids          # ID eBay invariati
    assert ids["vinted:111"]["source"] == "vinted" and ids["subito:555"]["source"] == "subito"
    assert ids["1"]["source"] == "ebay"
    # scartati in locale: cover (parola esclusa), Vinted 113 e Subito 557 (budget), piu' i 3 eBay di sempre
    assert "vinted:112" not in ids and "vinted:113" not in ids and "subito:557" not in ids
    assert s.richieste["vinted"] > 0 and s.richieste["subito"] > 0 and s.trovati_fonte == {"vinted": 3, "subito": 2}
    # verificati: eBay 1 e 2, Vinted 111, Subito 555; il dettaglio e' stato letto sulle pagine di Vinted e Subito
    assert s.verificati == 4 and x.vinted.pages == 1
    assert any(p.endswith(".htm") for p, _ in x.subito.requests)
    prompt = next(p for p in x.llm.verify_prompts if "Marantz" in p)
    assert '"fonte": "Vinted"' in prompt and "ricambi" in prompt
    assert all('"fonte"' not in p for p in x.llm.verify_prompts if "Xeon" in p)   # eBay: payload invariato
    # query solo nella lingua del sito: Vinted.it e Subito non ricevono le query tedesche o inglesi
    assert all("x79+mainboard" not in q and "motherboard" not in q for _, q in x.vinted.requests + x.subito.requests)
    db.close()


def test_piani_per_fonte(env):
    # con condizioni "guasto" eBay ha una nota sui filtri e le altre fonti no: due piani
    hunt, db, s, x = run_multi(env)
    assert x.llm.calls["plan"] == 2
    assert plan_note(hunt, "ebay") and plan_note(hunt, "vinted") == "" and plan_note(hunt, "subito") == ""
    db.close()
    # senza nota su nessuna fonte il piano e' uno solo e si condivide
    data = {**MULTI, "nome": "multi2", "ebay": {"marketplaces": ["EBAY_IT"], "prezzo_max": 100}}
    hunt, db, s, x = run_multi(env, data)
    assert x.llm.calls["plan"] == 1
    db.close()


def test_fonte_bloccata_non_ferma_le_altre(env):
    hunt, db, s, x = run_multi(env, vinted=FakeVinted(home_status=403))
    assert any("Vinted" in e and "rifiutata" in e for e in s.errori)
    assert s.richieste["vinted"] >= 1 and x.vinted.homes == 1               # un solo tentativo, poi si ferma
    assert db.get_item(hunt.nome, "subito:555") and db.get_item(hunt.nome, "1")
    assert db.get_item(hunt.nome, "vinted:111") is None
    db.close()


def test_caccia_senza_ebay_non_richiede_il_client(env):
    data = {**MULTI, "fonti": ["vinted", "subito"]}
    hunt, db, s, x = run_multi(env, data, with_ebay=False)
    assert s.chiamate_ebay == 0 and s.fonti == ["vinted", "subito"]
    assert db.get_item(hunt.nome, "vinted:111") and db.get_item(hunt.nome, "1") is None
    assert not x.ebay.searches
    db.close()


def test_ebay_non_configurato_non_ferma_le_altre_fonti(env):
    hunt, db, s, x = run_multi(env, with_ebay=False)             # eBay tra le fonti ma senza client
    assert any("eBay" in e and "non configurato" in e for e in s.errori)
    assert db.get_item(hunt.nome, "vinted:111")
    db.close()


def test_giro_parziale_non_azzera_ogni_minuti(env):
    hunt, db, s, x = run_multi(env, only=["vinted"])
    assert s.fonti == ["vinted"] and not x.ebay.searches and not x.subito.requests
    assert db.get_item(hunt.nome, "subito:555") is None and db.get_item(hunt.nome, "vinted:111")
    assert db.last_run_at(hunt.nome) is None                    # il giro era parziale: la caccia e' ancora "dovuta"
    run_multi(env)                                              # giro completo (stesso DB)
    assert db.last_run_at(hunt.nome) is not None
    with pytest.raises(ValueError):
        run_multi(env, {**MULTI, "fonti": ["ebay"]}, only=["vinted"])
    db.close()


def test_notifica_con_link_della_fonte(env, monkeypatch):
    sent = []

    def handler(req):
        sent.append((req.url.params["title"], req.url.params["click"], req.content.decode()))
        return httpx.Response(200)
    from scovatore.notify import notify
    cfg = replace(env, ntfy_url="https://ntfy.example/t", notify_min_score=30)
    hunt, db, s, x = run_multi(cfg)
    notify(hunt, db, cfg, transport=httpx.MockTransport(handler))
    by_link = {c: b for _, c, b in sent}
    assert "https://www.vinted.it/items/111-x" in by_link and "| Vinted" in by_link["https://www.vinted.it/items/111-x"]
    assert any(c == "https://www.ebay.it/itm/1" for c in by_link)    # eBay come prima
    db.close()


# ---------------------------------------------------------------- coda delle riesecuzioni
def test_db_richieste_di_riesecuzione(env):
    db = DB(env.db_path)
    rid = db.request_run("multi", True, ["vinted"])
    assert rid and db.request_run("multi") is None               # una sola richiesta in attesa per caccia
    assert db.request_run("altra") is not None
    row = db.pending_requests()[0]
    assert row["rigenera_piano"] == 1 and row["fonti"] == "vinted"
    assert db.claim_request(rid) and not db.claim_request(rid)    # presa una volta sola
    db.finish_request(rid, "errore di prova")
    assert db.request_state("multi")["error"] == "errore di prova"
    assert db.request_run("multi") is not None                    # dopo la presa se ne puo' accodare un'altra
    db.close()


def test_db_run_in_progress_ignora_i_crash_vecchi(env):
    db = DB(env.db_path)
    rid = db.start_run("multi")
    assert db.run_in_progress("multi")
    db.finish_run(rid, {})
    assert not db.run_in_progress("multi")
    db.conn.execute("INSERT INTO runs (hunt, started_at) VALUES ('crash', '2020-01-01T00:00:00+00:00')")
    db.conn.commit()
    assert not db.run_in_progress("crash")                       # senza fine ma vecchio: e' un crash
    db.close()


def test_db_migrazione_aggiunge_source_e_parziale(tmp_path):
    import sqlite3
    path = tmp_path / "vecchio.db"
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE items (hunt TEXT NOT NULL, legacy_id TEXT NOT NULL, item_id TEXT NOT NULL,
        title TEXT, score INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        PRIMARY KEY (hunt, legacy_id));
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, hunt TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, stats_json TEXT, error TEXT);
        INSERT INTO items VALUES ('h', '1', 'v1|1|0', 't', NULL, 'a', 'b');""")
    conn.commit()
    conn.close()
    db = DB(path)
    assert {r["name"] for r in db.conn.execute("PRAGMA table_info(items)")} >= {"source", "image", "hidden"}
    assert "parziale" in {r["name"] for r in db.conn.execute("PRAGMA table_info(runs)")}
    assert db.get_item("h", "1")["source"] == "ebay"             # le righe vecchie sono eBay
    db.close()


HUNT_YAML = """nome: multi
ricerca: amplificatore hi-fi guasto
fonti: [ebay, vinted, subito]
parole_escluse: [cover]
ebay: {marketplaces: [EBAY_IT], prezzo_max: 100, condizioni: [guasto]}
vinted: {max_ricerche: 4}
subito: {regione: piemonte}
"""


@pytest.fixture
def servizio(env, tmp_path, monkeypatch):
    """Configurazione con una caccia vera su disco e client finti al posto dei siti."""
    hunts = tmp_path / "cacce"
    hunts.mkdir()
    (hunts / "multi.yaml").write_text(HUNT_YAML, encoding="utf-8")
    cfg = replace(env, hunts_dir=hunts)
    fakes = SimpleNS(llm=SpyLLM(), ebay=FakeEbay(), vinted=FakeVinted(), subito=FakeSubito(), only=[], plans=[])

    def fake_clients(cfg_, need_ebay=True, db=None):
        ebay, pal, groq = make_clients(cfg_, fakes.ebay, fakes.llm)
        return (ebay if need_ebay else None), pal, groq

    def fake_build(hunt, cfg_, names, transport=None):
        out = {}
        if "vinted" in names:
            out["vinted"] = vinted_src(hunt, fakes.vinted)
        if "subito" in names:
            out["subito"] = subito_src(hunt, fakes.subito)
        return out
    monkeypatch.setattr(cli, "_clients", fake_clients)
    monkeypatch.setattr(pipeline, "build_sources", fake_build)
    return cfg, fakes


def test_process_requests_esegue_la_richiesta_del_web(servizio):
    cfg, fakes = servizio
    db = DB(cfg.db_path)
    db.request_run("multi", rigenera_piano=False, fonti=["vinted"])
    db.close()
    assert cli.process_requests(cfg) == 1
    db = DB(cfg.db_path)
    req = db.request_state("multi")
    assert req["claimed_at"] and req["finished_at"] and req["error"] is None
    assert db.get_item("multi", "vinted:111") and db.get_item("multi", "subito:555") is None   # solo Vinted
    assert not fakes.ebay.searches
    assert db.last_run_at("multi") is None                       # era parziale
    assert cli.process_requests(cfg) == 0                         # niente da fare
    db.close()


def test_process_requests_caccia_disattivata_e_rigenera_piano(servizio):
    cfg, fakes = servizio
    (cfg.hunts_dir / "multi.yaml").write_text(HUNT_YAML + "attiva: false\n", encoding="utf-8")
    db = DB(cfg.db_path)
    db.request_run("multi")
    db.close()
    assert cli.process_requests(cfg) == 1                        # l'operatore ha chiesto: gira anche se disattivata
    plans_before = fakes.llm.calls["plan"]
    db = DB(cfg.db_path)
    db.request_run("multi", rigenera_piano=True)
    db.close()
    cli.process_requests(cfg)
    assert fakes.llm.calls["plan"] == plans_before + 2           # ebay + gli altri, una volta ciascuno


def test_process_requests_errori_visibili(servizio):
    cfg, _ = servizio
    db = DB(cfg.db_path)
    db.request_run("inesistente")
    db.close()
    assert cli.process_requests(cfg) == 0
    db = DB(cfg.db_path)
    assert "non trovata" in db.request_state("inesistente")["error"]
    db.request_run("multi", fonti=["subito"])
    db.close()
    (cfg.hunts_dir / "multi.yaml").write_text(HUNT_YAML.replace("[ebay, vinted, subito]", "[ebay]"), encoding="utf-8")
    cli.process_requests(cfg)
    db = DB(cfg.db_path)
    assert "non ha attiva la fonte" in db.request_state("multi")["error"]
    db.close()


def test_wait_for_requests_esegue_durante_l_attesa(servizio, monkeypatch):
    cfg, fakes = servizio
    db = DB(cfg.db_path)
    db.request_run("multi", fonti=["subito"])
    db.close()
    monkeypatch.setattr(cli, "REQUEST_POLL_SECONDS", 0.01)
    cli._wait_for_requests(cfg, 0.05)
    db = DB(cfg.db_path)
    assert db.get_item("multi", "subito:555")
    db.close()


# ---------------------------------------------------------------- interfaccia web
def _serve(cfg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_web_pulsante_riesegui(servizio):
    cfg, _ = servizio
    DB(cfg.db_path).close()                     # il DB esiste ma la caccia non ha ancora girato
    srv, base = _serve(cfg)
    try:
        with httpx.Client(base_url=base) as c:
            # la caccia definita nel YAML compare anche senza annunci, con le sue fonti e il pulsante
            home = c.get("/").text
            assert "multi" in home and "Vinted" in home and "Subito" in home
            # il pulsante sta nella pagina Giri, non piu' in quella della caccia
            assert "Riesegui" not in c.get("/caccia/multi").text
            page = c.get("/giri")
            assert page.status_code == 200 and "Riesegui</button>" in page.text and 'action="/caccia/multi/riesegui"' in page.text
            assert ">Vinted</option>" in page.text and ">Subito</option>" in page.text and ">eBay</option>" in page.text
            assert " disabled" not in page.text and 'http-equiv="refresh"' not in page.text

            r = c.post("/caccia/multi/riesegui", data={"fonte": "vinted", "rigenera_piano": "1", "da": "giri"})
            assert r.status_code == 303 and r.headers["location"].startswith("/giri?msg=")
            db = DB(cfg.db_path)
            req = db.pending_requests()[0]
            assert req["hunt"] == "multi" and req["fonti"] == "vinted" and req["rigenera_piano"] == 1
            db.close()

            # in coda: pulsante disattivato, stato visibile, pagina che si aggiorna da sola
            page = c.get("/giri").text
            assert "in coda" in page and " disabled" in page and 'http-equiv="refresh"' in page
            assert "in coda" in c.get("/caccia/multi").text
            assert "in coda" in c.get("/").text

            # seconda richiesta mentre la prima e' in attesa: non si accoda
            r = c.post("/caccia/multi/riesegui", data={})
            assert "richiesta+in+coda" in r.headers["location"]
            assert len(DB(cfg.db_path).pending_requests()) == 1

            # input sporco: caccia sconosciuta, fonte sconosciuta, fonte non attiva nella caccia
            assert "sconosciuta" in c.post("/caccia/nessuna/riesegui", data={}).headers["location"]
            DB(cfg.db_path).conn.execute("DELETE FROM run_requests")
            assert "non+riconosciuta" in c.post("/caccia/multi/riesegui", data={"fonte": "wallapop"}).headers["location"]
            # form inviato da un altro sito: rifiutato
            r = c.post("/caccia/multi/riesegui", data={}, headers={"Origin": "http://sito-cattivo.example"})
            assert r.status_code == 403
    finally:
        srv.shutdown()


def test_web_riesegui_richiede_il_token(servizio):
    cfg, _ = servizio
    DB(cfg.db_path).close()
    srv, base = _serve(replace(cfg, web_token="segreto"))
    try:
        with httpx.Client(base_url=base) as c:
            assert c.post("/caccia/multi/riesegui", data={}).status_code == 403
            assert not DB(cfg.db_path).pending_requests()
            c.get("/", params={"token": "segreto"})                 # imposta il cookie
            assert c.post("/caccia/multi/riesegui", data={}).status_code == 303
            assert len(DB(cfg.db_path).pending_requests()) == 1
    finally:
        srv.shutdown()


def test_web_stato_in_corso_e_filtro_per_fonte(servizio):
    cfg, _ = servizio
    hunt = parse_hunt(yaml.safe_load(HUNT_YAML))
    llm = SpyLLM()
    ebay, pal, groq = make_clients(cfg, FakeEbay(), llm)
    db = DB(cfg.db_path)
    run_hunt(hunt, cfg, db, ebay, pal, groq, sources={"vinted": vinted_src(hunt, FakeVinted()),
                                                      "subito": subito_src(hunt, FakeSubito())})
    srv, base = _serve(cfg)
    try:
        with httpx.Client(base_url=base) as c:
            tutti = c.get("/caccia/multi", params={"esito": "tutti"}).text
            assert "Marantz" in tutti and "Sansui" in tutti and "Xeon" in tutti
            # link: Vinted e Subito con il proprio URL, eBay sul dominio configurato; chip della fonte
            assert 'href="https://www.vinted.it/items/111-x"' in tutti and "su Vinted" in tutti
            assert "su Subito" in tutti and 'href="https://www.ebay.it/itm/1"' in tutti
            solo = c.get("/caccia/multi", params={"esito": "tutti", "fonte": "vinted"}).text
            assert "Marantz" in solo and "Sansui" not in solo and "Xeon" not in solo
            api = c.get("/api/caccia/multi", params={"esito": "tutti", "fonte": "subito"}).json()
            assert [a["legacy_id"] for a in api] == ["subito:555"] and api[0]["link"].startswith("https://www.subito.it/")

            # giro in corso: pulsante disattivato
            db.start_run("multi")
            assert " disabled" in c.get("/giri").text and "Giro in corso" in c.get("/caccia/multi").text

            # azioni sugli ID con prefisso: l'ID viene accettato, quello sporco no
            c.post("/caccia/multi/azioni", data={"azione": "elimina:vinted:111"})
            assert db.get_item("multi", "vinted:111")["hidden"] == 1
            c.post("/caccia/multi/azioni", data={"azione": "ripristina:vinted:111"})
            assert db.get_item("multi", "vinted:111")["hidden"] == 0
            c.post("/caccia/multi/azioni", data={"azione": "elimina:vinted:111 OR 1=1"})
            assert db.get_item("multi", "vinted:111")["hidden"] == 0
    finally:
        srv.shutdown()
        db.close()


def test_url_ostili_non_arrivano_ne_alle_richieste_ne_ai_link(servizio):
    from scovatore.sources.base import safe_url
    from scovatore.web import item_href
    assert safe_url("https://www.vinted.it/items/1-x", "vinted.it") == "https://www.vinted.it/items/1-x"
    for bad in ("javascript:alert(1)", "http://192.168.1.1/admin", "https://vinted.it.evil.example/x",
                "ftp://www.vinted.it/x", "", None):
        assert safe_url(bad, "vinted.it") == "", bad

    # Vinted: l'URL ostile si sostituisce con il permalink costruito dall'ID, e non si scarica altro
    h = parse_hunt(yaml.safe_load(HUNT_YAML))
    hostile = [vinted_item(901, "Amplificatore Denon", 30, url="javascript:alert(1)"),
               vinted_item(902, "Amplificatore Yamaha", 30, url="http://192.168.1.1/admin")]
    fake = FakeVinted(hostile)
    src = vinted_src(h, fake)
    found = src.search("amplificatore", "it", h)
    assert [l.url for l in found] == ["https://www.vinted.it/items/901", "https://www.vinted.it/items/902"]
    # Subito: senza URL valido a subito.it l'annuncio si scarta
    ads = [subito_ad(801, "Amplificatore Kenwood", 50, urls={"default": "http://192.168.1.1/x-801.htm"}),
           subito_ad(802, "Amplificatore Onkyo", 50)]
    assert [l.legacy_id for l in subito_src(h, FakeSubito(ads)).search("x", "italia", h)] == ["subito:802"]
    # seconda difesa: un URL strano gia' nel DB non diventa un link nella pagina
    row = {"legacy_id": "vinted:1", "url": "javascript:alert(1)"}
    assert item_href(row, "vinted", "ebay.it") == "#"
    assert item_href(row, "ebay", "ebay.it") == "https://www.ebay.it/itm/vinted:1"


def test_cacce_di_esempio_con_fonti():
    from pathlib import Path
    from scovatore.hunt import load_all
    root = Path(__file__).resolve().parent.parent
    hunts = {h.nome: h for h in load_all(root / "cacce")}
    ex = hunts["esempio-fonti"]
    assert ex.fonti == ["ebay", "vinted", "subito"] and ex.attiva is False   # l'esempio non gira da solo
    assert ex.vinted.spedizione_stimata == 5 and ex.subito.regione == "piemonte"


# ---------------------------------------------------- cacce spente / eliminate e cancellazione dati
def _popola(cfg, nome="multi"):
    """Mette nel DB una caccia con annunci e un giro, come se avesse girato."""
    hunt = parse_hunt(yaml.safe_load(HUNT_YAML))
    ebay, pal, groq = make_clients(cfg, FakeEbay(), SpyLLM())
    db = DB(cfg.db_path)
    run_hunt(hunt, cfg, db, ebay, pal, groq, sources={"vinted": vinted_src(hunt, FakeVinted()),
                                                      "subito": subito_src(hunt, FakeSubito())})
    return db


def test_web_mostra_cacce_spente_ed_eliminate(servizio):
    cfg, _ = servizio
    db = _popola(cfg)
    # "fantasma": ha dati nel DB ma nessun file YAML. "spenta": file con attiva: false
    db.conn.execute("INSERT INTO items(hunt, legacy_id, item_id, first_seen, last_seen) VALUES "
                    "('fantasma','1','1','2026-01-01T00:00:00','2026-01-01T00:00:00')")
    db.conn.commit()
    (cfg.hunts_dir / "spenta.yaml").write_text(HUNT_YAML.replace("nome: multi", "nome: spenta\nattiva: false"), encoding="utf-8")
    (cfg.hunts_dir / "rotta.yaml").write_text("nome: rotta\nattiva: [", encoding="utf-8")
    db.conn.execute("INSERT INTO items(hunt, legacy_id, item_id, first_seen, last_seen) VALUES "
                    "('rotta','1','1','2026-01-01T00:00:00','2026-01-01T00:00:00')")
    db.conn.commit()
    srv, base = _serve(cfg)
    try:
        with httpx.Client(base_url=base) as c:
            home = c.get("/").text
            assert "Spente" in home and "Eliminate" in home
            assert home.index("Spente") < home.index("Eliminate")
            # i tre stati sono distinti: la caccia con il file rotto NON si dichiara eliminata
            assert 'chip st-eliminata">eliminata' in home and 'chip st-spenta">spenta' in home and "file non valido" in home
            # colpo d'occhio: colori diversi per stato e pulsante di eliminazione gia' nella home
            assert 'class="card st-spenta"' in home and 'class="card st-eliminata"' in home and 'class="card st-attiva"' in home
            assert 'href="/caccia/spenta/elimina"' in home and 'href="/caccia/fantasma/elimina"' in home
            assert 'href="/caccia/multi/elimina"' not in home and 'href="/caccia/rotta/elimina"' not in home
            assert c.get("/caccia/fantasma").text.count("Caccia eliminata") == 1
            giri = c.get("/giri").text
            assert 'action="/caccia/fantasma/riesegui"' not in giri               # senza file non si puo' rieseguire
            assert "Caccia spenta" in c.get("/caccia/spenta").text and 'action="/caccia/spenta/riesegui"' in giri
            assert "Elimina i dati" in c.get("/caccia/spenta").text
            assert "Elimina i dati" not in c.get("/caccia/multi").text           # la attiva non si puo'
            assert "Elimina i dati" not in c.get("/caccia/rotta").text
    finally:
        srv.shutdown()
        db.close()


def test_web_senza_cartella_cacce_non_dice_eliminata(servizio):
    cfg, _ = servizio
    db = _popola(cfg)
    srv, base = _serve(replace(cfg, hunts_dir=cfg.hunts_dir / "non-montata"))
    try:
        with httpx.Client(base_url=base) as c:
            assert 'chip st-eliminata' not in c.get("/").text and "Eliminate" not in c.get("/").text
            r = c.post("/caccia/multi/elimina", data={"conferma": "multi"})
            assert r.status_code == 303
            assert db.hunt_counts("multi")["annunci"] > 0             # cartella assente: non si cancella nulla
    finally:
        srv.shutdown()
        db.close()


def test_web_elimina_dati_con_conferma(servizio):
    cfg, _ = servizio
    db = _popola(cfg)
    # un giro vecchio e uno di oggi: quello di oggi resta (serve al tetto giornaliero delle chiamate eBay)
    db.conn.execute("INSERT INTO runs(hunt, started_at, finished_at, stats_json) VALUES "
                    "('multi','2020-01-01T00:00:00','2020-01-01T00:01:00','{}')")
    db.conn.commit()
    altra = db.hunt_counts("multi")
    assert altra["annunci"] > 0 and altra["giri"] >= 2
    db.conn.execute("INSERT INTO items(hunt, legacy_id, item_id, first_seen, last_seen) VALUES "
                    "('altra','1','1','2026-01-01T00:00:00','2026-01-01T00:00:00')")
    db.conn.commit()
    yaml_path = cfg.hunts_dir / "multi.yaml"
    yaml_prima = yaml_path.read_text(encoding="utf-8")
    srv, base = _serve(cfg)
    try:
        with httpx.Client(base_url=base) as c:
            # attiva: rifiutata, sia la pagina sia la POST
            assert "attiva" in c.get("/caccia/multi/elimina").text and "Elimina i dati</button>" not in \
                c.get("/caccia/multi/elimina").text
            r = c.post("/caccia/multi/elimina", data={"conferma": "multi"})
            assert "non+consentita" in r.headers["location"] and db.hunt_counts("multi")["annunci"] > 0

            # spegnere il file YAML la rende eliminabile
            yaml_path.write_text(yaml_prima.replace("nome: multi\n", "nome: multi\nattiva: false\n"), encoding="utf-8")
            conferma = c.get("/caccia/multi/elimina").text
            assert f"<b>{altra['annunci']}</b> annunci" in conferma and "non viene toccato" in conferma
            assert 'name="conferma"' in conferma

            # nome sbagliato o mancante: non cancella
            for form in ({"conferma": "altro"}, {"conferma": ""}, {}):
                r = c.post("/caccia/multi/elimina", data=form)
                assert "non+corrisponde" in r.headers["location"]
            assert db.hunt_counts("multi")["annunci"] == altra["annunci"]

            # richiesta in coda: non cancella
            db.request_run("multi")
            r = c.post("/caccia/multi/elimina", data={"conferma": "multi"})
            assert "in+coda" in r.headers["location"] and db.hunt_counts("multi")["annunci"] > 0
            db.conn.execute("DELETE FROM run_requests")
            db.conn.commit()

            # conferma giusta
            r = c.post("/caccia/multi/elimina", data={"conferma": "multi"})
            assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
            assert "eliminati" in c.get(r.headers["location"]).text
        after = db.hunt_counts("multi")
        assert after["annunci"] == 0 and after["piani"] == 0 and after["richieste"] == 0
        assert after["giri"] == altra["giri"] - 1                         # tolto solo il giro del 2020
        assert db.hunt_counts("altra")["annunci"] == 1                    # le altre cacce intatte
        assert yaml_path.read_text(encoding="utf-8").count("attiva: false") == 1   # il file non e' stato toccato
    finally:
        srv.shutdown()
        db.close()


def test_web_elimina_dati_caccia_eliminata_e_token(servizio):
    cfg, _ = servizio
    db = _popola(cfg)
    (cfg.hunts_dir / "multi.yaml").unlink()                              # file rimosso: caccia "eliminata"
    srv, base = _serve(replace(cfg, web_token="segreto"))
    try:
        with httpx.Client(base_url=base) as c:
            assert c.post("/caccia/multi/elimina", data={"conferma": "multi"}).status_code == 403
            c.get("/", params={"token": "segreto"})
            assert c.post("/caccia/multi/elimina", data={"conferma": "multi"}).status_code == 303
        assert db.hunt_counts("multi")["annunci"] == 0
    finally:
        srv.shutdown()
        db.close()
