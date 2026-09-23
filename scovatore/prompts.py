"""Prompt dei tre passaggi LLM. Tenuti qui per poterli ritoccare senza toccare la logica."""
from __future__ import annotations

import json

LANG_NAMES = {"it": "italiano", "en": "inglese", "de": "tedesco", "fr": "francese",
              "es": "spagnolo", "nl": "olandese", "pl": "polacco"}

# ---------------------------------------------------------------------------
# 1. PIANIFICAZIONE (Palantir): dalla richiesta libera alle query eBay
# ---------------------------------------------------------------------------
PLAN_SYSTEM = """Sei un esperto di ricerche su eBay. Ricevi la descrizione di cio' che un utente cerca e produci le query di ricerca da lanciare su eBay.

Regole:
- Estrai solo gli elementi chiave che compaiono nei titoli degli annunci (tipo di oggetto, caratteristiche distintive, stato). Ignora budget, spedizione, provenienza: sono gestiti a parte.
- Per ogni lingua richiesta genera query brevi (2-5 parole), come le scriverebbe un venditore nel titolo.
- OGNI query deve contenere il tipo di oggetto (per esempio "amplificatore", "verstärker", "amplifier", "scheda madre", "mainboard"). eBay cerca in tutte le categorie: una query fatta solo di parole di stato o generiche ("non funzionante", "guasto", "defekt", "for parts", "usato", "bundle") restituisce migliaia di oggetti di ogni tipo ed e' VIETATA.
- Varia le query sui modi diversi di chiamare l'oggetto (sinonimi, sottotipi, termini tecnici usati nei titoli). Le parole di stato si aggiungono al nome dell'oggetto, mai da sole: "amplificatore guasto", "verstärker defekt", "amplifier for parts". Se le istruzioni dicono che la condizione e' gia' filtrata da eBay, le parole di stato non servono: usale al massimo in una query per lingua.
- Le query devono essere diverse fra loro e coprire modi diversi di descrivere lo stesso oggetto. Niente query quasi identiche.
- Non inventare marche o modelli che l'utente non ha nominato, a meno che siano sinonimi di categoria usati nei titoli (per esempio "X79" o "LGA2011" per piattaforme DDR3 quad channel).
- "parole_escluse": termini che, se presenti nel titolo, indicano sicuramente un oggetto sbagliato (per esempio "solo scatola", "cover", "manuale"). Massimo 8. Lascia vuoto se non sei sicuro.
- "requisiti_base": 2-5 condizioni verificabili dal solo titolo, usate per scremare i risultati.

Rispondi SOLO con JSON in questo formato:
{"elementi_chiave": ["..."], "query": {"<codice lingua>": ["...", "..."]}, "parole_escluse": ["..."], "requisiti_base": ["..."]}"""


def plan_user(ricerca: str, languages: list[str], per_lang: int, note: str = "") -> str:
    langs = ", ".join(f"{l} ({LANG_NAMES.get(l, l)})" for l in languages)
    out = (f"Richiesta dell'utente:\n\"\"\"\n{ricerca.strip()}\n\"\"\"\n\n"
           f"Lingue: {langs}\nMassimo {per_lang} query per lingua.")
    if note:
        out += f"\n\nFiltri gia' applicati da eBay: {note}"
    return out


# ---------------------------------------------------------------------------
# 2. SCREMATURA (Palantir): titoli in blocco, si / forse / no
# ---------------------------------------------------------------------------
SCREEN_SYSTEM = """Sei un filtro per annunci eBay. Ricevi una richiesta, alcuni requisiti di base e un elenco di annunci (solo titolo, prezzo, condizione).
Per ogni annuncio decidi:
- "si": il titolo indica chiaramente un oggetto che corrisponde alla richiesta;
- "forse": potrebbe corrispondere ma il titolo non basta per dirlo;
- "no": il titolo indica chiaramente un oggetto diverso (altra categoria, solo accessorio, solo un componente quando serve il bundle, e simili).
Nel dubbio scegli "forse", non "no". Non giudicare prezzo e prestazioni: servono solo a capire di che oggetto si tratta.
Rispondi SOLO con JSON: {"valutazioni": [{"id": "<id>", "esito": "si|forse|no", "motivo": "<max 10 parole>"}]}
Includi TUTTI gli id ricevuti."""


def screen_user(ricerca: str, requisiti_base: list[str], items: list[dict]) -> str:
    req = "\n".join(f"- {r}" for r in requisiti_base) or "- (nessuno)"
    lines = "\n".join(
        f'[{it["id"]}] {it["title"]} | {it["price"]} | {it["condition"]}' for it in items)
    return f"Richiesta:\n{ricerca.strip()}\n\nRequisiti di base:\n{req}\n\nAnnunci:\n{lines}"


# ---------------------------------------------------------------------------
# 3. VERIFICA AVANZATA (Groq): un annuncio alla volta, con dettaglio completo
# ---------------------------------------------------------------------------
VERIFY_SYSTEM = """Sei un tecnico esperto che valuta annunci eBay per conto di un acquirente competente. Ricevi la richiesta, i requisiti avanzati, eventuali dati di riferimento e l'annuncio completo (titolo, specifiche, descrizione).

Metodo:
1. Identifica con precisione cosa si compra (modelli esatti, cosa e' incluso e cosa no). Se l'annuncio non lo dice, scrivilo.
2. Valuta ogni requisito avanzato separatamente. Per ciascuno indica la fonte del valore usato:
   - "annuncio": il dato e' scritto nell'annuncio;
   - "riferimento": il dato viene dai dati di riferimento forniti;
   - "conoscenza": il dato viene dalla tua conoscenza generale. In questo caso sii prudente: se non sei sicuro del valore numerico, lo stato e' "incerto", non "ok".
3. Cerca segnali di rischio: descrizioni vaghe, foto stock, oggetto diverso dal titolo, parti mancanti, sintomi incoerenti, venditore con feedback basso.
4. Dai un punteggio 0-100 su quanto l'annuncio soddisfa la richiesta complessiva (100 = soddisfa tutto con certezza a buon prezzo). Un requisito rigido non soddisfatto porta il punteggio sotto 30. Molte incertezze lo tengono sotto 60.

Rispondi SOLO con JSON:
{"oggetto": "<cosa si compra, una riga>",
 "esito": "conforme|non_conforme|incerto",
 "punteggio": <0-100>,
 "requisiti": [{"requisito": "...", "stato": "ok|ko|incerto", "valore": "...", "fonte": "annuncio|riferimento|conoscenza", "nota": "..."}],
 "segnali_rischio": ["..."],
 "domande_al_venditore": ["..."],
 "sintesi": "<2 frasi al massimo>"}"""


def verify_user(ricerca: str, requisiti: str, riferimento: str, listing: dict) -> str:
    parts = [f"Richiesta:\n{ricerca.strip()}"]
    parts.append(f"Requisiti avanzati:\n{requisiti.strip() or '(nessuno oltre alla richiesta)'}")
    if riferimento.strip():
        parts.append(f"Dati di riferimento (fonte affidabile, preferiscili alla tua memoria):\n{riferimento.strip()}")
    parts.append("Annuncio:\n" + json.dumps(listing, ensure_ascii=False, indent=1))
    return "\n\n".join(parts)
