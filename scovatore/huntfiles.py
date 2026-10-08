"""Operazioni sui file YAML delle cacce, per l'editor dell'interfaccia web.

Regole che questo modulo fa rispettare:
- un file si salva solo se supera la stessa validazione che usa il caricamento delle cacce;
- il nome della caccia non cambia modificando un file (dati, piani e giri sono legati al nome);
- prima di ogni modifica il testo precedente finisce in `cacce/.storico/` (ultime KEEP_BACKUPS copie);
- si scrive solo dentro la cartella delle cacce, con nomi file ricavati dal nome (a-z, 0-9, trattino);
- `dati_riferimento` non puo' puntare fuori da `dati/` e dalla cartella delle cacce: altrimenti
  chi puo' salvare un file potrebbe far leggere un file qualsiasi del server e mandarlo a Groq;
- il confronto con l'impronta del testo letto impedisce di sovrascrivere una modifica fatta nel frattempo.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .hunt import Hunt, HuntError, parse_hunt, slug

HISTORY_DIR = ".storico"
KEEP_BACKUPS = 20
MAX_BYTES = 100_000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
BACKUP_RE = re.compile(r"^(?P<stem>[a-z0-9._-]+)\.(?P<ts>\d{8}-\d{6})\.yaml$")

TEMPLATE = """# Nuova caccia. Il nome (a-z, 0-9, trattino) non si puo' piu' cambiare dopo il salvataggio.
nome: nuova-caccia
attiva: false                    # parte spenta: accendila dalla home quando e' pronta
ogni_minuti: 240
fonti: [ebay, vinted, subito]

ebay:
  marketplaces: [EBAY_IT]
  prezzo_max: 100
  regione: EUROPEAN_UNION
  consegna_paese: IT

ricerca: >
  Descrivi a parole cosa cerchi, solo gli elementi che finiscono nei titoli degli annunci.

requisiti_avanzati: |
  - Criteri che richiedono di leggere la descrizione.
"""


class HuntFileError(Exception):
    """Errore da mostrare all'operatore (testo gia' in italiano)."""


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def normalize(text: str) -> str:
    """Fine riga Unix e una sola riga vuota finale: i browser inviano CRLF."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def parse_text(text: str, hunts_dir: Path, path: Path | None = None) -> Hunt:
    """Valida il testo di una caccia. Alza HuntFileError con un messaggio comprensibile."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise HuntFileError(f"Il file supera i {MAX_BYTES // 1000} KB.")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" alla riga {mark.line + 1}" if mark else ""
        raise HuntFileError(f"YAML non valido{where}: {getattr(exc, 'problem', None) or exc}") from exc
    if not isinstance(data, dict):
        raise HuntFileError("Il file deve essere un dizionario YAML (campo: valore).")
    if not data.get("nome"):
        raise HuntFileError("Manca il campo 'nome'.")
    try:
        hunt = parse_hunt(data, path or hunts_dir / "nuova.yaml")
    except HuntError as exc:
        raise HuntFileError(str(exc)) from exc
    if not NAME_RE.match(hunt.nome):
        raise HuntFileError(f"Il nome '{hunt.nome}' non e' valido: usa solo a-z, 0-9 e trattino (max 63).")
    ref = hunt.dati_riferimento
    if ref:
        base = (path or hunts_dir / "nuova.yaml").parent
        target = (base / ref).resolve()
        roots = [(hunts_dir.parent / "dati").resolve(), hunts_dir.resolve()]
        if not any(target == r or r in target.parents for r in roots):
            raise HuntFileError("dati_riferimento deve stare nella cartella dati/ (o in quella delle cacce).")
    return hunt


def find_file(hunts_dir: Path, name: str) -> Path | None:
    """File YAML della caccia `name` (il nome e' quello slugificato del campo `nome`)."""
    try:
        files = sorted(hunts_dir.glob("*.y*ml"))
    except OSError:
        return None
    for p in files:
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            n = slug(str(data.get("nome") or p.stem)) if isinstance(data, dict) else slug(p.stem)
        except Exception:
            n = slug(p.stem)
        if n == name:
            return p
    return None


def read(hunts_dir: Path, name: str) -> tuple[Path, str]:
    p = find_file(hunts_dir, name)
    if p is None:
        raise HuntFileError("File della caccia non trovato.")
    return p, p.read_text(encoding="utf-8")


def _history(hunts_dir: Path) -> Path:
    return hunts_dir / HISTORY_DIR


def _atomic_write(path: Path, text: str, exclusive: bool = False) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if exclusive and path.exists():
            raise HuntFileError("Esiste gia' un file con questo nome.")
        os.replace(tmp, path)
    except HuntFileError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HuntFileError("Non riesco a scrivere nella cartella delle cacce: se e' montata in sola lettura "
                            "(:ro) il servizio web non puo' salvare.") from exc


def _backup(hunts_dir: Path, path: Path, current: str) -> None:
    hist = _history(hunts_dir)
    try:
        hist.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        (hist / f"{path.stem}.{ts}.yaml").write_text(current, encoding="utf-8")
        mine = sorted(p for p in hist.iterdir()
                      if (m := BACKUP_RE.match(p.name)) and m["stem"] == path.stem)
        for old in mine[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        raise HuntFileError("Non riesco a salvare la copia di sicurezza: modifica annullata.") from exc


def backups(hunts_dir: Path, name: str) -> list[tuple[str, str]]:
    """[(nome file, data leggibile)] delle copie di una caccia, dalla piu' recente."""
    p = find_file(hunts_dir, name)
    if p is None or not _history(hunts_dir).is_dir():
        return []
    out = []
    for f in sorted(_history(hunts_dir).iterdir(), reverse=True):
        m = BACKUP_RE.match(f.name)
        if m and m["stem"] == p.stem:
            d = datetime.strptime(m["ts"], "%Y%m%d-%H%M%S")
            out.append((f.name, d.strftime("%d/%m/%Y %H:%M:%S") + " UTC"))
    return out


def read_backup(hunts_dir: Path, name: str, filename: str) -> str:
    p = find_file(hunts_dir, name)
    m = BACKUP_RE.match(filename or "")
    if p is None or not m or m["stem"] != p.stem:
        raise HuntFileError("Versione non trovata.")
    f = _history(hunts_dir) / filename
    try:
        return f.read_text(encoding="utf-8")
    except OSError as exc:
        raise HuntFileError("Versione non trovata.") from exc


def save(hunts_dir: Path, name: str, text: str, base_digest: str) -> tuple[Hunt, bool]:
    """Salva il testo di una caccia esistente. Ritorna (caccia, modificato)."""
    path, current = read(hunts_dir, name)
    if digest(current) != base_digest:
        raise HuntFileError("Il file e' cambiato mentre lo modificavi (un'altra modifica o un git pull): "
                            "ricarica la pagina e rifai la modifica. Il tuo testo e' ancora nell'editor.")
    text = normalize(text)
    hunt = parse_text(text, hunts_dir, path)
    if hunt.nome != name:
        raise HuntFileError(f"Il nome non si cambia (qui e' '{name}'): dati e giri sono legati al nome. "
                            f"Per una caccia con un altro nome crea una nuova caccia.")
    if text == normalize(current):
        return hunt, False
    _backup(hunts_dir, path, current)
    _atomic_write(path, text)
    return hunt, True


def create(hunts_dir: Path, text: str) -> Hunt:
    """Crea il file di una nuova caccia, con il nome ricavato dal campo `nome`."""
    text = normalize(text)
    hunt = parse_text(text, hunts_dir)
    if find_file(hunts_dir, hunt.nome) is not None:
        raise HuntFileError(f"Esiste gia' una caccia chiamata '{hunt.nome}': cambia il campo 'nome'.")
    target = hunts_dir / f"{hunt.nome}.yaml"
    if target.exists():
        raise HuntFileError(f"Esiste gia' il file {target.name}.")
    try:
        hunts_dir.mkdir(exist_ok=True)
    except OSError:
        pass
    _atomic_write(target, text, exclusive=True)
    return hunt


def set_active(hunts_dir: Path, name: str, active: bool) -> bool:
    """Cambia solo `attiva:` nel file (commenti compresi). Ritorna False se era gia' cosi'."""
    path, current = read(hunts_dir, name)
    value = "true" if active else "false"
    line = re.search(r"^attiva:[^\n]*$", current, re.M)
    if line:
        comment = re.search(r"\s+#.*$", line.group(0))
        new_line = f"attiva: {value}" + (comment.group(0) if comment else "")
        text = current[:line.start()] + new_line + current[line.end():]
    else:
        nome = re.search(r"^nome:[^\n]*$", current, re.M)
        if not nome:
            raise HuntFileError("Nel file non trovo la riga 'nome:'.")
        text = current[:nome.end()] + f"\nattiva: {value}" + current[nome.end():]
    hunt, changed = save(hunts_dir, name, text, digest(current))
    return changed


def template(hunts_dir: Path) -> str:
    """Testo di partenza per una nuova caccia: esempio-fonti.yaml se c'e', altrimenti quello interno."""
    ex = hunts_dir / "esempio-fonti.yaml"
    try:
        text = ex.read_text(encoding="utf-8")
    except OSError:
        return TEMPLATE
    text = re.sub(r"^nome:[^\n]*$", "nome: nuova-caccia", text, count=1, flags=re.M)
    return re.sub(r"^attiva:[^\n]*$", "attiva: false", text, count=1, flags=re.M)
