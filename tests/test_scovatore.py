import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from scovatore import prompts
from scovatore.config import load_config
from scovatore.db import DB
from scovatore.ebay import EbayClient, build_filter, html_to_text
from scovatore.hunt import HuntError, load_all, parse_hunt
from scovatore.llm import LLMClient, TokenBucket, extract_json
from scovatore.pipeline import local_reject_reason, normalize_plan, run_hunt

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers
def summary(item_id, title, price, ship=5.0, currency="EUR", country="DE", fb="99.5"):
    d = {"itemId": f"v1|{item_id}|0", "legacyItemId": str(item_id), "title": title,
         "itemWebUrl": f"https://www.ebay.it/itm/{item_id}",
         "price": {"value": str(price), "currency": currency},
         "condition": "Usato", "conditionId": "3000", "buyingOptions": ["FIXED_PRICE"],
         "itemLocation": {"country": country},
         "seller": {"username": "tizio", "feedbackPercentage": fb, "feedbackScore": 500}}
    if ship is not None:
        d["shippingOptions"] = [{"shippingCost": {"value": str(ship), "currency": currency}}]
    return d


CATALOG = [
    summary(1, "Asus P9X79 + Xeon E5-1650 v2 + 16GB", 80),
    summary(2, "Gigabyte X79 UD3 con i7-4820K", 60, ship=12),
    summary(3, "Mainboard X79 solo cpu cooler", 20),         # parola esclusa via piano
    summary(4, "Asus X79 Deluxe con i7-3930K", 99, ship=10),  # oltre budget con spedizione
    summary(5, "Custodia per scheda madre", 10),              # la scrematura dice no
    summary(6, "X79 board UK", 50, currency="GBP"),           # valuta sbagliata
]


class FakeEbay:
    def __init__(self):
        self.searches = []
        self.details = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        assert req.headers["Authorization"] == "Bearer tok"
        assert req.headers["X-EBAY-C-ENDUSERCTX"].startswith("contextualLocation=country%3DIT")
        if req.url.path.endswith("/item_summary/search"):
            q = parse_qs(urlparse(str(req.url)).query)
            self.searches.append((req.headers["X-EBAY-C-MARKETPLACE-ID"], q["q"][0], q.get("filter", [""])[0]))
            return httpx.Response(200, json={"total": len(CATALOG), "itemSummaries": CATALOG})
        if "/item/" in req.url.path:
            self.details += 1
            return httpx.Response(200, json={
                "description": "<p>Funzionante, <b>CPU inclusa</b></p><script>x()</script>",
                "localizedAspects": [{"name": "Chipset", "value": "Intel X79"}],
                "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "EUR"}}]})
        return httpx.Response(404)


class FakeLLM:
    def __init__(self):
        self.calls = {"plan": 0, "screen": 0, "verify": 0}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if system == prompts.PLAN_SYSTEM:
            self.calls["plan"] += 1
            content = json.dumps({"elementi_chiave": ["x79"],
                                  "query": {"it": ["scheda madre x79 cpu", "scheda madre x79 cpu"],
                                            "de": ["x79 mainboard cpu"], "en": ["x79 motherboard cpu"]},
                                  "parole_escluse": ["cooler"], "requisiti_base": ["scheda madre con cpu"]})
            content = "<think>ragiono</think>\n```json\n" + content + "\n```"
        elif system == prompts.SCREEN_SYSTEM:
            self.calls["screen"] += 1
            rows = []
            for line in user.splitlines():
                if line.startswith("["):
                    i = line[1:line.index("]")]
                    rows.append({"id": i, "esito": "no" if "Custodia" in line else "si", "motivo": "t"})
            content = json.dumps({"valutazioni": rows})
        else:
            self.calls["verify"] += 1
            score = 85 if "1650" in user else 40
            content = json.dumps({"oggetto": "bundle", "esito": "conforme" if score > 50 else "incerto",
                                  "punteggio": score, "requisiti": [], "segnali_rischio": [],
                                  "sintesi": "ok"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


HUNT = {
    "nome": "Test Mobo",
    "ricerca": "scheda madre DDR3 quad channel con cpu",
    "requisiti_avanzati": "TDP < 100 W",
    "ebay": {"marketplaces": ["EBAY_IT", "EBAY_DE"], "prezzo_max": 100, "condizioni": ["usato"]},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("EBAY_APP_ID", "EBAY_CERT_ID", "GROQ_API_KEY", "PALANTIR_BASE_URL", "SCOVATORE_NTFY_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SCOVATORE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROQ_TPM_LIMIT", "0")
    return load_config(env_file=str(tmp_path / "nessuno.env"))


def make_clients(cfg, ebay_fake, llm_fake):
    ebay = EbayClient("app", "cert", cfg.ebay_api_base, "IT", "10100", 1000,
                      transport=httpx.MockTransport(ebay_fake))
    pal = LLMClient(cfg.palantir, transport=httpx.MockTransport(llm_fake))
    groq = LLMClient(cfg.groq, transport=httpx.MockTransport(llm_fake))
    return ebay, pal, groq


# ---------------------------------------------------------------- unit
def test_build_filter():
    h = parse_hunt(HUNT)
    f = build_filter(h.ebay)
    assert "price:[..100]" in f and "priceCurrency:EUR" in f
    assert "itemLocationRegion:EUROPEAN_UNION" in f
    assert "deliveryCountry:IT" in f
    assert "conditionIds:{3000|4000|5000|6000}" in f


def test_filter_price_range_and_country():
    h = parse_hunt({**HUNT, "ebay": {"prezzo_min": 10, "prezzo_max": 50.5, "regione": None, "paese": "de",
                                     "formati": ["auction"], "escludi_venditori": ["a", "b"]}})
    f = build_filter(h.ebay)
    assert "price:[10..50.5]" in f and "itemLocationCountry:DE" in f
    assert "buyingOptions:{AUCTION}" in f and "excludeSellers:{a|b}" in f


def test_hunt_validation():
    with pytest.raises(HuntError):
        parse_hunt({"nome": "x"})
    with pytest.raises(HuntError):
        parse_hunt({**HUNT, "ebay": {"regione": "EUROPEAN_UNION", "paese": "IT"}})
    with pytest.raises(HuntError):
        parse_hunt({**HUNT, "ebay": {"condizioni": ["rotto-forte"]}})
    with pytest.raises(HuntError):
        parse_hunt({**HUNT, "ebay": {"prezo_max": 10}})
    with pytest.raises(HuntError):
        parse_hunt({**HUNT, "campo_sbagliato": 1})
    assert parse_hunt(HUNT).nome == "test-mobo"
    # default: tutte le lingue UE; con lingue esplicite, quelle + la lingua dei marketplace + inglese
    assert parse_hunt(HUNT).languages() == ["de", "en", "es", "fr", "it", "nl", "pl"]
    assert parse_hunt({**HUNT, "lingue": ["it"]}).languages() == ["de", "en", "it"]
    assert parse_hunt({"ricerca": "x", "lingue": "ue"}).languages() == sorted(["it", "en", "de", "fr", "es", "nl", "pl"])
    with pytest.raises(HuntError):
        parse_hunt({**HUNT, "lingue": ["klingon"]})


def test_example_hunts_are_valid():
    hunts = load_all(ROOT / "cacce")
    files = sorted((ROOT / "cacce").glob("*.y*ml"))
    assert len(hunts) == len(files), "una caccia di esempio non si carica"   # load_all salta le invalide
    assert {"mobo-ddr3-quad", "ampli-guasto"} <= {h.nome for h in hunts}
    amp = next(h for h in hunts if h.nome == "ampli-guasto")
    assert amp.ebay.condizioni == [7000] and amp.ebay.spedizione_max == 15


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('<think>bla {x}</think>\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Ecco il risultato: {"a": 3} spero vada bene') == {"a": 3}


def test_html_to_text():
    assert html_to_text("<p>Ciao&nbsp;<b>mondo</b></p><script>alert(1)</script>") == "Ciao mondo"


def test_normalize_plan_dedup_and_cap():
    plan = normalize_plan({"query": {"it": ["A b", "a  B", "c", "d", "e"]}}, ["it", "en"], 3)
    assert plan["query"] == {"it": ["A b", "c", "d"], "en": []}
    assert normalize_plan({"query": ["x"]}, ["en"], 3)["query"] == {"en": ["x"]}


def test_token_bucket_no_limit_is_instant():
    TokenBucket(0).wait(10_000)


def test_local_reject():
    from scovatore.ebay import parse_summary
    h = parse_hunt({**HUNT, "ebay": {**HUNT["ebay"], "feedback_minimo": 98}})
    ok = parse_summary(summary(1, "X79 board", 80), "EBAY_IT", "q")
    assert local_reject_reason(ok, h, []) is None
    assert "budget" in local_reject_reason(parse_summary(summary(2, "b", 99, ship=10), "EBAY_IT", "q"), h, [])
    assert "valuta" in local_reject_reason(parse_summary(summary(3, "b", 9, currency="GBP"), "EBAY_IT", "q"), h, [])
    assert "esclusa" in local_reject_reason(parse_summary(summary(4, "X79 solo cpu", 9), "EBAY_IT", "q"), h, ["solo cpu"])
    assert local_reject_reason(parse_summary(summary(5, "X79 cpucooler", 9), "EBAY_IT", "q"), h, ["cpu"]) is None
    assert "feedback" in local_reject_reason(parse_summary(summary(6, "b", 9, fb="95.0"), "EBAY_IT", "q"), h, [])


# ---------------------------------------------------------------- end to end
def test_pipeline_end_to_end(env):
    cfg = env
    hunt = parse_hunt(HUNT)
    db = DB(cfg.db_path)
    ef, lf = FakeEbay(), FakeLLM()
    ebay, pal, groq = make_clients(cfg, ef, lf)

    s = run_hunt(hunt, cfg, db, ebay, pal, groq)

    # ogni marketplace lancia tutte le lingue del piano: it (1 dopo dedup) + de + en, su IT e DE
    assert s.query == 6
    assert {mp for mp, _, _ in ef.searches} == {"EBAY_IT", "EBAY_DE"}
    assert s.unici == 6
    assert s.scartati_filtri == 3           # cooler, oltre budget, GBP
    assert s.scremati == 3 and s.scremati_no == 1
    assert s.verificati == 2 and s.conformi == 1
    assert ef.details == 2
    assert lf.calls == {"plan": 1, "screen": 1, "verify": 2}

    top = db.results(hunt.nome)
    assert top[0]["legacy_id"] == "1" and top[0]["score"] == 85

    # secondo giro: piano in cache, niente riscrematura ne' riverifica
    s2 = run_hunt(hunt, cfg, db, ebay, pal, groq)
    assert s2.nuovi == 0 and s2.scremati == 0 and s2.verificati == 0
    assert lf.calls == {"plan": 1, "screen": 1, "verify": 2}

    # cambiano i requisiti: si riverifica, ma non si ripianifica ne' si riscrema
    hunt2 = parse_hunt({**HUNT, "requisiti_avanzati": "TDP < 80 W"})
    s3 = run_hunt(hunt2, cfg, db, ebay, pal, groq)
    assert s3.verificati == 2 and lf.calls["plan"] == 1 and lf.calls["screen"] == 1
    db.close()


def test_pipeline_without_groq_and_screening(env):
    cfg = env
    hunt = parse_hunt({**HUNT, "screening": False})
    db = DB(cfg.db_path)
    ef, lf = FakeEbay(), FakeLLM()
    ebay, pal, _ = make_clients(cfg, ef, lf)
    s = run_hunt(hunt, cfg, db, ebay, pal, None)
    assert s.scremati == 0 and s.verificati == 0
    assert lf.calls["screen"] == 0 and lf.calls["verify"] == 0
    rows = db.results(hunt.nome, include_unverified=True)
    assert {r["screen_verdict"] for r in rows} == {"saltato"}
    db.close()


def test_llm_falls_back_when_json_mode_rejected(env):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, text="unsupported param response_format")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    c = LLMClient(env.palantir, transport=httpx.MockTransport(handler))
    assert c.chat_json("s", "u") == {"ok": True}
    assert seen == [True, False]


def test_notify(env, monkeypatch):
    from scovatore.notify import notify
    env.ntfy_url = "https://ntfy.example/topic"
    hunt = parse_hunt(HUNT)
    db = DB(env.db_path)
    ebay, pal, groq = make_clients(env, FakeEbay(), FakeLLM())
    env_ntfy = env.ntfy_url
    env.ntfy_url = ""
    run_hunt(hunt, env, db, ebay, pal, groq)
    env.ntfy_url = env_ntfy
    posts = []

    def handler(req):
        posts.append(req)
        return httpx.Response(200)

    assert notify(hunt, db, env, transport=httpx.MockTransport(handler)) == 1
    assert "title=" in str(posts[0].url)
    assert notify(hunt, db, env, transport=httpx.MockTransport(handler)) == 0
    db.close()


def test_daily_ebay_budget_counts_previous_runs(env):
    hunt = parse_hunt(HUNT)
    db = DB(env.db_path)
    ebay, pal, groq = make_clients(env, FakeEbay(), FakeLLM())
    s = run_hunt(hunt, env, db, ebay, pal, groq)
    assert s.chiamate_ebay > 0
    assert db.ebay_calls_today() == s.chiamate_ebay
    db.close()


# ---------------------------------------------------------------- UE, tetto ricerche, web
from scovatore.hunt import EU27, EU_MARKETPLACES  # noqa: E402
from scovatore.pipeline import search_tasks  # noqa: E402


def test_marketplaces_ue_alias_and_default():
    h = parse_hunt({"ricerca": "x", "ebay": {"marketplaces": "ue"}})
    assert h.ebay.marketplaces == EU_MARKETPLACES
    h = parse_hunt({"ricerca": "x", "ebay": {"marketplaces": ["EBAY_GB", "ue", "ebay_it"]}})
    assert h.ebay.marketplaces[0] == "EBAY_GB" and h.ebay.marketplaces.count("EBAY_IT") == 1
    assert len(h.ebay.marketplaces) == 1 + len(EU_MARKETPLACES)
    assert parse_hunt({"ricerca": "x"}).ebay.marketplaces == ["EBAY_IT"]   # default: solo ebay.it
    with pytest.raises(HuntError):
        parse_hunt({"ricerca": "x", "ebay": {"marketplaces": ["EBAY_LT"]}})


def test_paesi_ammessi():
    assert parse_hunt({"ricerca": "x"}).ebay.allowed_countries() == EU27
    assert parse_hunt({"ricerca": "x", "ebay": {"paesi_ammessi": "tutti"}}).ebay.allowed_countries() is None
    h = parse_hunt({"ricerca": "x", "ebay": {"paesi_ammessi": ["lt", "lv"]}})
    assert h.ebay.allowed_countries() == {"LT", "LV"}
    h = parse_hunt({"ricerca": "x", "ebay": {"paesi_ammessi": ["ue", "ch"]}})
    assert "CH" in h.ebay.allowed_countries() and "LT" in h.ebay.allowed_countries()
    with pytest.raises(HuntError):
        parse_hunt({"ricerca": "x", "ebay": {"paesi_ammessi": ["Lituania"]}})
    h = parse_hunt({"ricerca": "x", "ebay": {"regione": None, "paese": "ch"}})
    assert h.ebay.allowed_countries() == {"CH"}


def test_local_reject_country():
    from scovatore.ebay import parse_summary
    h = parse_hunt(HUNT)
    lt = parse_summary(summary(10, "X79 bundle", 50, country="LT"), "EBAY_DE", "q")
    gb = parse_summary(summary(11, "X79 bundle", 50, country="GB"), "EBAY_DE", "q")
    ch = parse_summary(summary(12, "X79 bundle", 50, country="CH"), "EBAY_DE", "q")
    assert local_reject_reason(lt, h, []) is None
    assert local_reject_reason(gb, h, []).startswith("paese GB")
    assert local_reject_reason(ch, h, []).startswith("paese CH")


def test_search_tasks_round_robin_and_cap(env):
    plan = {"query": {"it": ["a1", "a2"], "de": ["b1"], "en": ["e1"]}, "parole_escluse": [], "requisiti_base": []}
    h = parse_hunt({**HUNT, "query_extra": []})
    tasks = search_tasks(h, plan)
    # prima la query n.1 di ogni marketplace, poi la n.2...
    assert tasks[:2] == [("EBAY_IT", "a1"), ("EBAY_DE", "b1")]
    assert ("EBAY_IT", "e1") in tasks and ("EBAY_DE", "e1") in tasks

    fake, llm = FakeEbay(), FakeLLM()
    h = parse_hunt({**HUNT, "ebay": {**HUNT["ebay"], "max_ricerche": 2}})
    ebay, pal, groq = make_clients(env, fake, llm)
    db = DB(env.db_path)
    stats = run_hunt(h, env, db, ebay, pal, groq)
    assert stats.query == 2 and len(fake.searches) == 2
    assert stats.ricerche_saltate > 0
    assert {mp for mp, _, _ in fake.searches} == {"EBAY_IT", "EBAY_DE"}
    assert set(stats.durate) >= {"piano", "ricerca", "totale"}


def _seeded_db(env):
    fake, llm = FakeEbay(), FakeLLM()
    ebay, pal, groq = make_clients(env, fake, llm)
    db = DB(env.db_path)
    run_hunt(parse_hunt(HUNT), env, db, ebay, pal, groq)
    db.close()


def test_db_migration_adds_image_column(tmp_path):
    import sqlite3
    path = tmp_path / "vecchio.db"
    from scovatore.db import SCHEMA
    old_schema = SCHEMA.replace("    image TEXT,\n", "")   # schema della prima versione
    assert "image" not in old_schema
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.commit()
    conn.close()
    db = DB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(items)")}
    assert "image" in cols


def test_web_pages(env):
    import threading
    from http.server import ThreadingHTTPServer
    from scovatore.web import make_handler

    _seeded_db(env)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(env))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with httpx.Client(base_url=base) as c:
            home = c.get("/")
            assert home.status_code == 200 and "test-mobo" in home.text and "conformi" in home.text
            page = c.get("/caccia/test-mobo")
            assert page.status_code == 200 and "Xeon E5-1650" in page.text and "Dettagli" in page.text
            # filtro esito: gli scartati dalla scrematura sono solo la custodia
            scart = c.get("/caccia/test-mobo", params={"esito": "scartati"})
            assert "Custodia" in scart.text and "Xeon" not in scart.text
            assert c.get("/caccia/test-mobo", params={"min": 90}).text.count('class="item"') == 0
            assert c.get("/caccia/test-mobo", params={"giorni": 1, "esito": "tutti"}).text.count('class="item"') >= 3
            api = c.get("/api/caccia/test-mobo").json()
            assert api[0]["score"] == 85 and api[0]["verifica"]["esito"] == "conforme"
            assert "Giri recenti" in c.get("/giri").text
            assert c.get("/caccia/inesistente").status_code == 404
            # niente HTML iniettato dai titoli
            assert "<script>x()" not in page.text
    finally:
        srv.shutdown()


def test_web_token(env, monkeypatch):
    import threading
    from dataclasses import replace
    from http.server import ThreadingHTTPServer
    from scovatore.web import make_handler

    _seeded_db(env)
    cfg = replace(env, web_token="segreto")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with httpx.Client(base_url=base) as c:
            assert c.get("/").status_code == 401
            r = c.get("/giri", params={"token": "segreto"})
            assert r.status_code == 303 and r.headers["location"] == "/giri"
            assert c.get("/giri").status_code == 200      # cookie impostato
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- piano: Palantir o Groq, query generiche
from scovatore.pipeline import get_plan, is_generic_query, pick_planner, plan_note  # noqa: E402


def test_generic_queries_are_dropped():
    assert is_generic_query("non funzionante")
    assert is_generic_query("Defekt für Bastler")
    assert is_generic_query("for parts / not working")
    assert not is_generic_query("amplificatore non funzionante")
    assert not is_generic_query("verstärker defekt")
    plan = normalize_plan({"query": {"it": ["non funzionante", "amplificatore guasto", "guasto"],
                                     "en": ["for parts", "amplifier for parts"]}}, ["it", "en"], 4)
    assert plan["query"] == {"it": ["amplificatore guasto"], "en": ["amplifier for parts"]}
    assert len(plan["scartate"]) == 3


def test_plan_note_tells_condition_is_filtered():
    h = parse_hunt({"ricerca": "ampli", "ebay": {"condizioni": ["guasto"]}})
    assert "ricambi" in plan_note(h)
    assert "funzionanti" in plan_note(parse_hunt(HUNT))
    assert plan_note(parse_hunt({"ricerca": "x"})) == ""


def test_plan_llm_setting(env, monkeypatch):
    monkeypatch.setenv("SCOVATORE_PLAN_LLM", "groq")
    cfg = load_config(env_file=str(env.data_dir / "nessuno.env"))
    assert cfg.plan_llm == "groq"
    monkeypatch.setenv("SCOVATORE_PLAN_LLM", "chatgpt")
    with pytest.raises(ValueError):
        load_config(env_file=str(env.data_dir / "nessuno.env"))


def test_plan_generated_by_chosen_llm_and_cached_per_llm(env):
    from dataclasses import replace
    pal_fake, groq_fake = FakeLLM(), FakeLLM()
    pal = LLMClient(env.palantir, transport=httpx.MockTransport(pal_fake))
    groq = LLMClient(env.groq, transport=httpx.MockTransport(groq_fake))
    db = DB(env.db_path)
    h = parse_hunt(HUNT)

    cfg_groq = replace(env, plan_llm="groq")
    plan = get_plan(h, db, pick_planner(cfg_groq, pal, groq))
    assert groq_fake.calls["plan"] == 1 and pal_fake.calls["plan"] == 0
    assert plan["generato_da"].startswith("groq:")
    get_plan(h, db, pick_planner(cfg_groq, pal, groq))          # dalla cache
    assert groq_fake.calls["plan"] == 1

    # cambiare modello invalida la cache: il piano di Groq non viene riusato per Palantir
    plan = get_plan(h, db, pick_planner(env, pal, groq))
    assert pal_fake.calls["plan"] == 1 and plan["generato_da"].startswith("palantir:")

    # groq richiesto ma non configurato: ripiega su Palantir
    assert pick_planner(cfg_groq, pal, None) is pal
    assert pick_planner(env, None, groq) is groq


def test_run_uses_groq_for_plan(env):
    from dataclasses import replace
    fake, pal_fake, groq_fake = FakeEbay(), FakeLLM(), FakeLLM()
    ebay = EbayClient("app", "cert", env.ebay_api_base, "IT", "10100", 1000, transport=httpx.MockTransport(fake))
    pal = LLMClient(env.palantir, transport=httpx.MockTransport(pal_fake))
    groq = LLMClient(env.groq, transport=httpx.MockTransport(groq_fake))
    run_hunt(parse_hunt(HUNT), replace(env, plan_llm="groq"), DB(env.db_path), ebay, pal, groq)
    assert groq_fake.calls["plan"] == 1 and pal_fake.calls["plan"] == 0
    assert pal_fake.calls["screen"] >= 1          # la scrematura resta a Palantir


# ---------------------------------------------------------------- interventi dell'operatore
def _web(cfg):
    import threading
    from http.server import ThreadingHTTPServer
    from scovatore.web import make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _row(cfg, lid):
    db = DB(cfg.db_path)
    try:
        return dict(db.get_item("test-mobo", lid))
    finally:
        db.close()


def test_web_actions(env):
    _seeded_db(env)          # 1 conforme (85), 2 incerto (40), 5 scartato dalla scrematura
    srv, base = _web(env)
    try:
        with httpx.Client(base_url=base) as c:
            # correzione singola: il 2 era incerto per il modello, l'operatore dice conforme
            r = c.post("/caccia/test-mobo/azioni", data={"azione": "conforme:2", "back": "esito=tutti&min=0"})
            assert r.status_code == 303
            loc = r.headers["location"]
            assert loc.startswith("/caccia/test-mobo?") and "esito=tutti" in loc and "msg=" in loc
            row = _row(env, "2")
            assert row["manual_verdict"] == "conforme" and row["verdict"] == "incerto"   # il modello resta visibile
            page = c.get(loc).text
            assert "1 annuncio segnato come conforme" in page and "il modello diceva: incerto" in page
            assert "Gigabyte" in c.get("/caccia/test-mobo", params={"esito": "manuali"}).text
            assert "Gigabyte" in c.get("/caccia/test-mobo", params={"esito": "conforme"}).text

            # eliminazione multipla, con nota
            r = c.post("/caccia/test-mobo/azioni", data={"multipla": "1", "azione_multipla": "elimina",
                                                         "id": ["1", "5"], "nota": "doppioni"})
            assert _row(env, "1")["hidden"] == 1 and _row(env, "5")["hidden"] == 1
            tutti = c.get("/caccia/test-mobo", params={"esito": "tutti"}).text
            assert "Xeon E5-1650" not in tutti and "Custodia" not in tutti
            elim = c.get("/caccia/test-mobo", params={"esito": "eliminati"}).text
            assert "Xeon E5-1650" in elim and "Ripristina" in elim
            assert "2 eliminati a mano" in c.get("/").text

            c.post("/caccia/test-mobo/azioni", data={"azione": "ripristina:1"})
            assert _row(env, "1")["hidden"] == 0

            # riverifica: anche uno scartato dalla scrematura torna in coda per Groq
            c.post("/caccia/test-mobo/azioni", data={"azione": "riverifica:5"})
            row = _row(env, "5")
            assert row["screen_verdict"] == "si" and row["score"] is None and row["verify_hash"] is None

            # togli correzione
            c.post("/caccia/test-mobo/azioni", data={"azione": "annulla:2"})
            assert _row(env, "2")["manual_verdict"] is None

            # input sporco: azioni sconosciute, id non numerici, nessuna selezione, altra caccia
            assert "non+riconosciuta" in c.post("/caccia/test-mobo/azioni",
                                                data={"azione": "drop:1"}).headers["location"]
            c.post("/caccia/test-mobo/azioni", data={"azione": "elimina:1 OR 1=1"})
            assert _row(env, "1")["hidden"] == 0
            assert "Nessun+annuncio" in c.post("/caccia/test-mobo/azioni",
                                               data={"multipla": "1", "azione_multipla": "elimina"}).headers["location"]
            c.post("/caccia/altra/azioni", data={"azione": "elimina:1"})
            assert _row(env, "1")["hidden"] == 0

            # form inviato da un altro sito: rifiutato
            r = c.post("/caccia/test-mobo/azioni", data={"azione": "elimina:1"},
                       headers={"Origin": "http://sito-cattivo.example"})
            assert r.status_code == 403 and _row(env, "1")["hidden"] == 0
    finally:
        srv.shutdown()


def test_pipeline_respects_operator(env, monkeypatch):
    fake, llm = FakeEbay(), FakeLLM()
    ebay, pal, groq = make_clients(env, fake, llm)
    db = DB(env.db_path)
    h = parse_hunt(HUNT)
    run_hunt(h, env, db, ebay, pal, groq)
    verifies = llm.calls["verify"]

    db.set_manual(h.nome, ["1"], "non_conforme", "CPU sbagliata in foto")
    db.set_hidden(h.nome, ["2"], True)
    # prezzi cambiati del 20%: senza l'operatore entrambi verrebbero riverificati
    for it in CATALOG[:2]:
        it["price"]["value"] = str(float(it["price"]["value"]) * 0.8)
    try:
        stats = run_hunt(h, env, db, ebay, pal, groq)
    finally:
        CATALOG[0]["price"]["value"], CATALOG[1]["price"]["value"] = "80", "60"
    assert llm.calls["verify"] == verifies            # nessuna nuova verifica
    assert stats.eliminati_a_mano == 1
    row = db.get_item(h.nome, "1")
    assert row["manual_verdict"] == "non_conforme" and row["manual_note"] == "CPU sbagliata in foto"
    assert db.get_item(h.nome, "2")["hidden"] == 1
    assert all(r["legacy_id"] != "2" for r in db.results(h.nome, include_unverified=True))

    # le notifiche ignorano eliminati e corretti a mano
    from dataclasses import replace
    from scovatore.notify import notify
    sent = []
    cfg = replace(env, ntfy_url="http://ntfy.local/t", notify_min_score=0)
    db.conn.execute("UPDATE items SET notified=0, verdict='conforme', score=90 WHERE hunt=?", (h.nome,))
    db.conn.commit()
    notify(h, db, cfg, transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    notified = {r["legacy_id"] for r in db.conn.execute("SELECT legacy_id FROM items WHERE notified=1")}
    assert "1" not in notified and "2" not in notified and len(sent) == len(notified)


# ---------------------------------------------------------------- spedizione verso l'Italia e link
from scovatore.ebay import ships_to  # noqa: E402
from scovatore.pipeline import queries_for  # noqa: E402


def test_ships_to():
    assert ships_to({"shipToLocations": {"regionExcluded": [{"regionType": "COUNTRY", "regionId": "IT"}]}}, "IT") is False
    assert ships_to({"shipToLocations": {"regionIncluded": [{"regionType": "COUNTRY", "regionId": "DE"},
                                                            {"regionType": "COUNTRY", "regionId": "AT"}]}}, "IT") is False
    assert ships_to({"shipToLocations": {"regionIncluded": [{"regionType": "WORLDWIDE", "regionId": "WORLDWIDE"}]}}, "IT")
    assert ships_to({"shipToLocations": {"regionIncluded": [{"regionType": "WORLD_REGION", "regionId": "EUROPE"}]}}, "IT")
    assert ships_to({"shippingOptions": [{"shippingCost": {"value": "9"}}]}, "IT") is True
    assert ships_to({}, "IT") is None                           # nessun dato: non si scarta
    # esclusione esplicita vince anche con opzioni di spedizione presenti
    assert ships_to({"shippingOptions": [{}], "shipToLocations": {
        "regionExcluded": [{"regionType": "COUNTRY", "regionId": "IT"}]}}, "IT") is False


def test_unknown_shipping_from_abroad_is_dropped():
    from scovatore.ebay import parse_summary
    h = parse_hunt(HUNT)                            # default spedizione_ignota: scarta_estero
    de = parse_summary(summary(20, "X79 bundle", 50, ship=None, country="DE"), "EBAY_IT", "q")
    it = parse_summary(summary(21, "X79 bundle", 50, ship=None, country="IT"), "EBAY_IT", "q")
    assert local_reject_reason(de, h, []).startswith("non spedisce in IT")
    assert local_reject_reason(it, h, []) is None   # in Italia puo' essere ritiro a mano: si tiene
    keep = parse_hunt({**HUNT, "ebay": {**HUNT["ebay"], "spedizione_ignota": "tieni"}})
    assert local_reject_reason(de, keep, []) is None


def test_queries_all_languages_on_single_marketplace():
    h = parse_hunt({"ricerca": "x", "ebay": {"marketplaces": ["EBAY_IT"]}, "query_extra": ["manuale"]})
    plan = {"query": {"it": ["i1", "i2"], "de": ["d1"], "pl": ["p1"], "en": ["e1", "e2"]}}
    qs = queries_for("it", plan, h)
    assert qs[0] == "manuale" and set(qs) == {"manuale", "i1", "i2", "d1", "p1", "e1", "e2"}
    # alternanza: la prima di ogni lingua prima delle seconde
    assert qs.index("d1") < qs.index("i2") and qs.index("p1") < qs.index("e2")


class FakeEbayNoShip(FakeEbay):
    """Come FakeEbay, ma il dettaglio dell'annuncio 2 dice che non spedisce in Italia."""
    def __call__(self, req):
        if "/item/" in req.url.path and "%7C2%7C" in str(req.url):
            self.details += 1
            return httpx.Response(200, json={"description": "ok", "shipToLocations": {
                "regionIncluded": [{"regionType": "COUNTRY", "regionId": "DE"}]}})
        return super().__call__(req)


def test_verify_skips_items_not_shipping_here(env):
    fake, llm = FakeEbayNoShip(), FakeLLM()
    ebay, pal, groq = make_clients(env, fake, llm)
    db = DB(env.db_path)
    h = parse_hunt(HUNT)
    stats = run_hunt(h, env, db, ebay, pal, groq)
    assert stats.non_spediscono == 1
    row = db.get_item(h.nome, "2")
    assert row["screen_verdict"] == "no" and "non spedisce in IT" in row["screen_reason"]
    assert row["score"] is None                       # Groq non l'ha visto
    assert llm.calls["verify"] == stats.verificati == 1


def test_links_point_to_ebay_it(env):
    _seeded_db(env)
    srv, base = _web(env)
    try:
        with httpx.Client(base_url=base) as c:
            page = c.get("/caccia/test-mobo").text
            assert 'href="https://www.ebay.it/itm/1"' in page
            assert c.get("/api/caccia/test-mobo").json()[0]["link"] == "https://www.ebay.it/itm/1"
    finally:
        srv.shutdown()
    assert env.item_link("123") == "https://www.ebay.it/itm/123"
