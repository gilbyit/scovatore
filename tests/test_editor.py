"""Test dell'editor dei file di caccia: validazione, storico, conflitti, accendi/spegni, nuova caccia, token."""
from __future__ import annotations

import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from scovatore import huntfiles
from scovatore.huntfiles import HuntFileError
from scovatore.web import make_handler

from test_scovatore import env  # noqa: F401  (fixture)

BASE_YAML = """nome: prova
attiva: true      # commento da non perdere
ogni_minuti: 120
fonti: [ebay]
ebay: {marketplaces: [EBAY_IT], prezzo_max: 50}
ricerca: >
  Amplificatore hi-fi guasto.
"""


@pytest.fixture
def cartella(tmp_path):
    d = tmp_path / "cacce"
    d.mkdir()
    (tmp_path / "dati").mkdir()
    (tmp_path / "dati" / "rif.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (d / "prova.yaml").write_text(BASE_YAML, encoding="utf-8")
    return d


def _digest(d, name="prova"):
    return huntfiles.digest(huntfiles.read(d, name)[1])


# ------------------------------------------------------------------ modulo huntfiles
def test_validazione_messaggi_chiari(cartella):
    ok = huntfiles.parse_text(BASE_YAML, cartella)
    assert ok.nome == "prova" and ok.ogni_minuti == 120
    casi = {
        "nome: x\nricerca: [": "riga",                                     # YAML rotto, con la riga
        "- a\n- b\n": "dizionario",
        "ricerca: x\n": "Manca il campo 'nome'",
        "nome: x\n": "ricerca",
        "nome: x\nricerca: y\nfoo: 1\n": "sconosciuti",
        "nome: x\nricerca: y\nebay: {prezzo_massimo: 3}\n": "sconosciuti",
        "nome: \"***\"\nricerca: y\n": "nome",
    }
    for text, parte in casi.items():
        with pytest.raises(HuntFileError) as exc:
            huntfiles.parse_text(text, cartella)
        assert parte.lower() in str(exc.value).lower(), (text, str(exc.value))
    with pytest.raises(HuntFileError, match="supera"):
        huntfiles.parse_text("nome: x\nricerca: " + "a" * 200_000, cartella)


def test_dati_riferimento_solo_dentro_dati(cartella):
    base = "nome: x\nricerca: y\ndati_riferimento: {}\n"
    assert huntfiles.parse_text(base.format("../dati/rif.csv"), cartella).nome == "x"
    for fuori in ("/etc/passwd", "../.env", "../../etc/passwd", "../data/scovatore.db"):
        with pytest.raises(HuntFileError, match="dati_riferimento"):
            huntfiles.parse_text(base.format(fuori), cartella)


def test_salvataggio_con_storico_e_conflitto(cartella):
    base = _digest(cartella)
    nuovo = BASE_YAML.replace("prezzo_max: 50", "prezzo_max: 80")
    h, changed = huntfiles.save(cartella, "prova", nuovo.replace("\n", "\r\n"), base)     # arriva con CRLF dal browser
    assert changed and h.ebay.prezzo_max == 80
    assert "prezzo_max: 80" in (cartella / "prova.yaml").read_text() and "\r" not in (cartella / "prova.yaml").read_text()
    versioni = huntfiles.backups(cartella, "prova")
    assert len(versioni) == 1
    assert "prezzo_max: 50" in huntfiles.read_backup(cartella, "prova", versioni[0][0])   # la copia e' quella di prima

    # impronta vecchia: qualcuno ha cambiato il file nel frattempo, non si sovrascrive
    with pytest.raises(HuntFileError, match="cambiato"):
        huntfiles.save(cartella, "prova", nuovo.replace("80", "90"), base)
    assert "prezzo_max: 80" in (cartella / "prova.yaml").read_text()

    # stesso testo: nessuna copia inutile
    assert huntfiles.save(cartella, "prova", nuovo, _digest(cartella))[1] is False
    assert len(huntfiles.backups(cartella, "prova")) == 1


def test_nome_non_cambia_e_file_non_scritto_se_invalido(cartella):
    prima = (cartella / "prova.yaml").read_text()
    with pytest.raises(HuntFileError, match="nome non si cambia"):
        huntfiles.save(cartella, "prova", BASE_YAML.replace("nome: prova", "nome: altra"), _digest(cartella))
    with pytest.raises(HuntFileError):
        huntfiles.save(cartella, "prova", BASE_YAML + "campo_inventato: 1\n", _digest(cartella))
    assert (cartella / "prova.yaml").read_text() == prima and not huntfiles.backups(cartella, "prova")
    assert not [p for p in cartella.iterdir() if p.name.startswith(".") and p.name.endswith(".tmp")]


def test_storico_tiene_solo_le_ultime_copie(cartella, monkeypatch):
    monkeypatch.setattr(huntfiles, "KEEP_BACKUPS", 3)
    hist = cartella / huntfiles.HISTORY_DIR
    hist.mkdir()
    for i in range(6):                                                    # copie vecchie gia' presenti
        (hist / f"prova.2020010{i}-000000.yaml").write_text(BASE_YAML)
    (hist / "prova-altra.20200101-000000.yaml").write_text("x")           # di un'altra caccia con prefisso uguale
    huntfiles.save(cartella, "prova", BASE_YAML.replace("120", "130"), _digest(cartella))
    mie = [p.name for p in hist.iterdir() if p.name.startswith("prova.")]
    assert len(mie) == 3 and (hist / "prova-altra.20200101-000000.yaml").exists()


def test_read_backup_non_esce_dalla_cartella(cartella):
    for bad in ("../prova.yaml", "../../etc/passwd", "prova.20200101-000000.yaml/../../x", "", "prova.yaml"):
        with pytest.raises(HuntFileError):
            huntfiles.read_backup(cartella, "prova", bad)


def test_accendi_spegni_cambia_solo_attiva(cartella):
    assert huntfiles.set_active(cartella, "prova", False) is True
    t = (cartella / "prova.yaml").read_text()
    assert "attiva: false      # commento da non perdere" in t and t.count("attiva:") == 1
    assert huntfiles.parse_text(t, cartella).attiva is False
    assert huntfiles.set_active(cartella, "prova", False) is False        # gia' spenta
    assert huntfiles.set_active(cartella, "prova", True) is True
    # senza la riga attiva: viene aggiunta dopo il nome
    (cartella / "prova.yaml").write_text(BASE_YAML.replace("attiva: true      # commento da non perdere\n", ""))
    assert huntfiles.set_active(cartella, "prova", False) is True
    t = (cartella / "prova.yaml").read_text()
    assert t.startswith("nome: prova\nattiva: false\n") and huntfiles.parse_text(t, cartella).attiva is False


def test_nuova_caccia(cartella):
    h = huntfiles.create(cartella, huntfiles.template(cartella).replace("nuova-caccia", "Mia Caccia 2"))
    assert h.nome == "mia-caccia-2" and (cartella / "mia-caccia-2.yaml").exists() and h.attiva is False
    with pytest.raises(HuntFileError, match="Esiste"):
        huntfiles.create(cartella, BASE_YAML)                              # "prova" c'e' gia'
    # il nome del file viene dal nome slugificato: niente percorsi
    h = huntfiles.create(cartella, BASE_YAML.replace("nome: prova", "nome: ../../evil"))
    assert (cartella / f"{h.nome}.yaml").exists() and h.nome == "evil"
    assert not (cartella.parent / "evil.yaml").exists()
    assert huntfiles.parse_text(huntfiles.TEMPLATE, cartella).nome == "nuova-caccia"


def test_template_usa_esempio_se_c_e(cartella):
    (cartella / "esempio-fonti.yaml").write_text(BASE_YAML.replace("nome: prova", "nome: esempio-fonti"))
    t = huntfiles.template(cartella)
    assert "nome: nuova-caccia" in t and "attiva: false" in t


# ------------------------------------------------------------------ interfaccia web
def _serve(cfg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def web(env, cartella):
    cfg = replace(env, hunts_dir=cartella, web_token="segreto")
    srv, base = _serve(cfg)
    c = httpx.Client(base_url=base)
    c.get("/", params={"token": "segreto"})                                # cookie
    yield c, cfg, cartella
    c.close()
    srv.shutdown()


def test_web_vede_il_file_e_salva(web):
    c, cfg, d = web
    page = c.get("/caccia/prova/file").text
    assert "<textarea" in page and "Amplificatore hi-fi guasto" in page and "commento da non perdere" in page
    assert "Salva</button>" in page and "disabled" not in page.split("<textarea")[1].split("</form>")[0]
    base = huntfiles.digest((d / "prova.yaml").read_text())
    nuovo = BASE_YAML.replace("prezzo_max: 50", "prezzo_max: 75")
    r = c.post("/caccia/prova/file", data={"testo": nuovo, "base": base})
    assert r.status_code == 303 and r.headers["location"].startswith("/caccia/prova/file?msg=")
    assert "prezzo_max: 75" in (d / "prova.yaml").read_text()
    after = c.get(r.headers["location"]).text
    assert "File salvato" in after and "Versioni precedenti (1)" in after
    # carica una versione vecchia nell'editor senza salvarla
    nome = huntfiles.backups(d, "prova")[0][0]
    old = c.get("/caccia/prova/file", params={"versione": nome}).text
    assert "prezzo_max: 50" in old and "non e&#x27; ancora salvata" in old or "non e' ancora salvata" in old
    assert "prezzo_max: 75" in (d / "prova.yaml").read_text()


def test_web_errore_resta_nell_editor_col_testo(web):
    c, cfg, d = web
    prima = (d / "prova.yaml").read_text()
    sbagliato = BASE_YAML.replace("ogni_minuti: 120", "ogni_minuti: 120\ncampo_inventato: 1")
    r = c.post("/caccia/prova/file", data={"testo": sbagliato, "base": huntfiles.digest(prima)})
    assert r.status_code == 400 and "Non salvato" in r.text and "campo_inventato" in r.text
    assert (d / "prova.yaml").read_text() == prima
    # file cambiato nel frattempo: errore di conflitto, il testo dell'operatore non si perde
    r = c.post("/caccia/prova/file", data={"testo": BASE_YAML + "# mio\n", "base": "0000"})
    assert r.status_code == 400 and "cambiato" in r.text and "# mio" in r.text


def test_web_senza_token_solo_lettura(env, cartella):
    cfg = replace(env, hunts_dir=cartella, web_token="")
    srv, base = _serve(cfg)
    try:
        with httpx.Client(base_url=base) as c:
            page = c.get("/caccia/prova/file").text
            assert "readonly" in page and "SCOVATORE_WEB_TOKEN" in page and "<button disabled>Salva" in page
            base_d = huntfiles.digest((cartella / "prova.yaml").read_text())
            assert c.post("/caccia/prova/file", data={"testo": BASE_YAML + "# x\n", "base": base_d}).status_code == 400
            assert c.post("/caccia/prova/attiva", data={"valore": "0"}).status_code == 303
            assert c.post("/nuova", data={"testo": huntfiles.TEMPLATE}).status_code == 400
            assert "attiva: true" in (cartella / "prova.yaml").read_text()
            assert not (cartella / "nuova-caccia.yaml").exists()
            home = c.get("/").text
            assert "Vedi file" in home and "Spegni" not in home
    finally:
        srv.shutdown()


def test_web_scrittura_richiede_il_cookie_del_token(env, cartella):
    cfg = replace(env, hunts_dir=cartella, web_token="segreto")
    srv, base = _serve(cfg)
    try:
        with httpx.Client(base_url=base) as c:                            # senza cookie
            assert c.get("/caccia/prova/file").status_code == 401
            assert c.post("/caccia/prova/file", data={"testo": "x", "base": ""}).status_code == 403
            assert c.post("/caccia/prova/attiva", data={"valore": "0"}).status_code == 403
            assert c.post("/nuova", data={"testo": huntfiles.TEMPLATE}).status_code == 403
        assert "attiva: true" in (cartella / "prova.yaml").read_text()
    finally:
        srv.shutdown()


def test_web_accendi_spegni_dalla_home(web):
    c, cfg, d = web
    home = c.get("/").text
    assert "Spegni" in home and 'action="/caccia/prova/attiva"' in home and "Modifica" in home
    r = c.post("/caccia/prova/attiva", data={"valore": "0"})
    assert r.status_code == 303 and "spenta" in c.get(r.headers["location"]).text
    assert "attiva: false" in (d / "prova.yaml").read_text()
    home = c.get("/").text
    assert "Spente" in home and "Accendi" in home and 'class="card st-spenta"' in home
    c.post("/caccia/prova/attiva", data={"valore": "1"})
    assert "attiva: true" in (d / "prova.yaml").read_text()


def test_web_nuova_caccia(web):
    c, cfg, d = web
    page = c.get("/nuova").text
    assert "Nuova caccia" in page and "nome: nuova-caccia" in page
    r = c.post("/nuova", data={"testo": huntfiles.TEMPLATE.replace("nuova-caccia", "seconda")})
    assert r.status_code == 303 and r.headers["location"].startswith("/caccia/seconda/file")
    assert (d / "seconda.yaml").exists() and "Spente" in c.get("/").text and "seconda" in c.get("/").text
    # nome gia' usato o file invalido: resta nell'editor col testo
    r = c.post("/nuova", data={"testo": BASE_YAML})
    assert r.status_code == 400 and "Esiste gia" in r.text and "Amplificatore" in r.text
    r = c.post("/nuova", data={"testo": "nome: x\nricerca: ["})
    assert r.status_code == 400 and "YAML non valido" in r.text


def test_web_file_rotto_visibile_e_correggibile(web):
    c, cfg, d = web
    (d / "rotto.yaml").write_text("nome: rotto\nricerca: [\n", encoding="utf-8")
    home = c.get("/").text
    assert "file non valido" in home and 'href="/caccia/rotto/file"' in home          # anche se non ha mai girato
    page = c.get("/caccia/rotto/file").text
    assert "Il file attuale non e&#x27; valido" in page or "Il file attuale non e' valido" in page
    base = huntfiles.digest((d / "rotto.yaml").read_text())
    r = c.post("/caccia/rotto/file", data={"testo": "nome: rotto\nricerca: ok\n", "base": base})
    assert r.status_code == 303 and "file non valido" not in c.get("/").text


def test_web_percorsi_ostili(web):
    c, cfg, d = web
    for url in ("/caccia/..%2Fprova/file", "/caccia/%2e%2e/file", "/caccia/inesistente/file", "/caccia/prova%00/file"):
        assert c.get(url).status_code in (404, 400)
    r = c.post("/caccia/..%2F..%2Fx/file", data={"testo": BASE_YAML, "base": "0"})
    assert r.status_code in (400, 404, 303)
    assert not (d.parent / "x.yaml").exists() and not (d.parent.parent / "x.yaml").exists()
    # un nome con ../ nel testo non fa scrivere fuori dalla cartella
    c.post("/nuova", data={"testo": BASE_YAML.replace("nome: prova", "nome: ../../fuori")})
    assert not (d.parent / "fuori.yaml").exists() and (d / "fuori.yaml").exists()
