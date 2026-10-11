#!/usr/bin/env python3
"""
Sentinella di disponibilità Apple Store (ritiro in negozio) con notifiche ntfy
==============================================================================

Controlla se un prodotto è disponibile per il ritiro in un Apple Store e invia
una notifica push all'app ntfy (iPhone/Android) quando lo diventa.

Configurazione predefinita (dati verificati sul sito Apple l'11/10/2026):
  prodotto  MJXT4QL/A  iPhone 18 Pro Max 512GB nero
  negozio   R625       Apple Nave de Vero, Marghera (VE)

Uso:
  python3 sentinella.py --topic NOME --test    notifica di prova con lo stato attuale
  python3 sentinella.py --topic NOME           un solo controllo (GitHub Actions, cron)
  python3 sentinella.py --topic NOME --ciclo   controllo continuo, ogni 2 minuti circa

Il topic può essere indicato anche con la variabile d'ambiente NTFY_TOPIC.
Requisiti: Python 3.8 o successivo; nessuna libreria aggiuntiva.
"""

import argparse
import html
import http.client
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

VERSIONE = "1.0.0"

# --- Dati del prodotto e del negozio ----------------------------------------
PRODOTTO_PREDEFINITO = "MJXT4QL/A"  # iPhone 18 Pro Max 512GB nero
NEGOZIO_PREDEFINITO = "R625"        # Apple Nave de Vero
PAGINA_PRODOTTO = ("https://www.apple.com/it/shop/buy-iphone/iphone-18-pro/"
                   "display-da-6,9%22-512gb-nero")

# --- Endpoint Apple ----------------------------------------------------------
# Il primo è quello verificato (risposta in body.stores); il secondo è usato
# dal sito e viene tentato solo se il primo non risponde correttamente.
ENDPOINT = (
    "https://www.apple.com/it/shop/retail/pickup-message",
    "https://www.apple.com/it/shop/fulfillment-messages",
)
INTESTAZIONI_APPLE = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                   "Version/18.5 Safari/605.1.15"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9",
    "Referer": PAGINA_PRODOTTO,
}

# --- Parametri di funzionamento ---------------------------------------------
TIMEOUT_S = 20
FILE_STATO_PREDEFINITO = ".stato-sentinella.json"
INTERVALLO_PREDEFINITO_S = 120
INTERVALLO_MINIMO_S = 60
PROMEMORIA_PREDEFINITO_MIN = 30
SOGLIA_ERRORI = 3        # verifiche fallite di fila...
SOGLIA_ERRORI_MIN = 30   # ...e minuti senza verifiche riuscite, prima dell'avviso
RE_TOPIC = re.compile(r"^[-_A-Za-z0-9]{1,64}$")

try:  # orari in ora italiana anche su GitHub (che usa UTC)
    from zoneinfo import ZoneInfo  # novermin (assente in 3.8: si usa l'ora locale)
    FUSO = ZoneInfo("Europe/Rome")
except Exception:  # Python 3.8 o sistema senza database dei fusi: ora locale
    FUSO = None


class ErroreVerifica(Exception):
    """La disponibilità non è stata determinata."""


class Esito:
    def __init__(self, disponibile, codice, testo, titolo):
        self.disponibile = disponibile  # True solo se pickupDisplay == "available"
        self.codice = codice            # valore grezzo di pickupDisplay
        self.testo = testo              # es. "Oggi presso Apple Nave de Vero"
        self.titolo = titolo            # es. "iPhone 18 Pro Max 512GB nero"


# --- Utilità -----------------------------------------------------------------
def orario(ts=None, formato="%d/%m/%Y %H:%M:%S"):
    ts = time.time() if ts is None else ts
    dt = datetime.fromtimestamp(ts, FUSO) if FUSO else datetime.fromtimestamp(ts)
    return dt.strftime(formato)


def ora_breve(ts=None):
    return orario(ts, "%H:%M")


def log(messaggio):
    print(f"[{orario()}] {messaggio}", flush=True)


def pulisci(testo):
    """Rimuove tag HTML ed entità, normalizza gli spazi."""
    if not testo:
        return ""
    testo = re.sub(r"<[^>]+>", " ", str(testo))
    return re.sub(r"\s+", " ", html.unescape(testo)).strip()


def scrivi_output_github(nome, valore):
    percorso = os.environ.get("GITHUB_OUTPUT")
    if percorso:
        with open(percorso, "a", encoding="utf-8") as f:
            f.write(f"{nome}={valore}\n")


def riepilogo_github(riga):
    percorso = os.environ.get("GITHUB_STEP_SUMMARY")
    if percorso:
        with open(percorso, "a", encoding="utf-8") as f:
            f.write(f"- {orario()} — {riga}\n")


# --- Lettura della disponibilità --------------------------------------------
def scarica_json(url):
    richiesta = urllib.request.Request(url, headers=INTESTAZIONI_APPLE)
    host = urllib.parse.urlsplit(url).netloc
    try:
        with urllib.request.urlopen(richiesta, timeout=TIMEOUT_S) as risposta:
            grezzo = risposta.read()
    except urllib.error.HTTPError as e:
        raise ErroreVerifica(f"HTTP {e.code} da {host}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise ErroreVerifica(
                "certificato SSL non verificabile (su Mac, con Python installato "
                "da python.org, eseguire 'Install Certificates.command')") from None
        raise ErroreVerifica(f"rete non raggiungibile ({e.reason})") from None
    except (OSError, http.client.HTTPException) as e:
        raise ErroreVerifica(f"errore di rete ({e})") from None
    try:
        return json.loads(grezzo.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        anteprima = pulisci(grezzo[:100].decode("utf-8", "replace"))
        raise ErroreVerifica(f"risposta non JSON da {host} ({anteprima!r})") from None


def interpreta(dati, prodotto, negozio):
    if not isinstance(dati, dict):
        raise ErroreVerifica("risposta inattesa (non è un oggetto JSON)")
    stato_head = str((dati.get("head") or {}).get("status", "200"))
    if stato_head != "200":
        raise ErroreVerifica(f"Apple ha risposto con stato {stato_head}")
    corpo = dati.get("body") or {}
    negozi = corpo.get("stores")
    if negozi is None:  # struttura dell'endpoint fulfillment-messages
        negozi = ((corpo.get("content") or {}).get("pickupMessage") or {}).get("stores")
    if not isinstance(negozi, list):
        raise ErroreVerifica("risposta senza elenco dei negozi")
    voce = next((n for n in negozi
                 if isinstance(n, dict) and n.get("storeNumber") == negozio), None)
    if voce is None:
        raise ErroreVerifica(f"negozio {negozio} assente dalla risposta")
    dettaglio = (voce.get("partsAvailability") or {}).get(prodotto)
    if not isinstance(dettaglio, dict) or "pickupDisplay" not in dettaglio:
        raise ErroreVerifica(f"prodotto {prodotto} assente dalla risposta")
    codice = str(dettaglio.get("pickupDisplay"))
    regolare = (dettaglio.get("messageTypes") or {}).get("regular") or {}
    testo = (pulisci(regolare.get("storePickupQuote"))
             or pulisci(dettaglio.get("pickupSearchQuote")) or codice)
    titolo = pulisci(regolare.get("storePickupProductTitle")) or prodotto
    return Esito(codice == "available", codice, testo, titolo)


def verifica(prodotto, negozio):
    """Restituisce un Esito oppure solleva ErroreVerifica."""
    errori = []
    for indice, base in enumerate(ENDPOINT):
        parametri = {"pl": "true", "parts.0": prodotto, "store": negozio}
        if "fulfillment-messages" in base:
            parametri["mts.0"] = "regular"
        url = base + "?" + urllib.parse.urlencode(parametri)
        tentativi = 2 if indice == 0 else 1
        for tentativo in range(tentativi):
            try:
                return interpreta(scarica_json(url), prodotto, negozio)
            except ErroreVerifica as e:
                errori.append(str(e))
                if tentativo + 1 < tentativi:
                    time.sleep(random.uniform(3, 6))
    raise ErroreVerifica("; ".join(dict.fromkeys(errori)))


# --- Notifiche ntfy ----------------------------------------------------------
def notifica(cfg, titolo, messaggio, priorita=3, tag=None, collegamento=True):
    """Pubblica su ntfy (formato JSON, POST all'URL radice del server)."""
    corpo = {"topic": cfg.topic, "title": titolo, "message": messaggio,
             "priority": priorita, "tags": tag or []}
    if collegamento:
        corpo["click"] = cfg.pagina
        corpo["actions"] = [{"action": "view", "label": "Apri la pagina Apple",
                             "url": cfg.pagina}]
    intestazioni = {"Content-Type": "application/json",
                    "User-Agent": "sentinella-apple/" + VERSIONE}
    if cfg.token:
        intestazioni["Authorization"] = "Bearer " + cfg.token
    richiesta = urllib.request.Request(cfg.server.rstrip("/") + "/",
                                       data=json.dumps(corpo).encode("utf-8"),
                                       headers=intestazioni, method="POST")
    try:
        with urllib.request.urlopen(richiesta, timeout=TIMEOUT_S) as risposta:
            risposta.read()
        log(f"Notifica inviata: {titolo}")
        return True
    except urllib.error.HTTPError as e:
        nota = ""
        if e.code == 429:
            nota = (" (limite di ntfy.sh superato: creare un account ntfy e "
                    "impostare il token in NTFY_TOKEN)")
        elif e.code in (401, 403):
            nota = " (topic protetto o token NTFY_TOKEN errato)"
        log(f"ERRORE nell'invio della notifica: HTTP {e.code}{nota}")
    except (OSError, http.client.HTTPException) as e:
        log(f"ERRORE nell'invio della notifica: {e}")
    return False


# --- Stato persistente --------------------------------------------------------
STATO_INIZIALE = {
    "versione": 1,
    "prodotto": None,
    "negozio": None,
    "disponibile": None,          # None = mai verificato
    "ultimo_avviso": None,        # ora dell'ultimo avviso di disponibilità
    "errori_consecutivi": 0,
    "errore_dal": None,           # ora del primo errore della serie in corso
    "avviso_errore_inviato": False,
}


def carica_stato(percorso, prodotto, negozio):
    stato = dict(STATO_INIZIALE)
    try:
        with open(percorso, encoding="utf-8") as f:
            letto = json.load(f)
        # Uno stato relativo ad altro prodotto/negozio viene ignorato.
        if (isinstance(letto, dict) and letto.get("prodotto") == prodotto
                and letto.get("negozio") == negozio):
            stato.update({k: letto[k] for k in STATO_INIZIALE if k in letto})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log(f"Stato precedente illeggibile, si riparte da zero ({e})")
    stato["prodotto"], stato["negozio"] = prodotto, negozio
    return stato


def salva_stato(percorso, stato):
    temporaneo = percorso + ".tmp"
    with open(temporaneo, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=2)
    os.replace(temporaneo, percorso)


# --- Logica del controllo -----------------------------------------------------
def esegui_controllo(cfg, stato):
    """Un controllo con aggiornamento dello stato. Restituisce False se una
    notifica dovuta non è stata consegnata (verrà ritentata al giro successivo)."""
    ora = time.time()
    try:
        esito = verifica(cfg.prodotto, cfg.negozio)
    except ErroreVerifica as e:
        stato["errori_consecutivi"] += 1
        if stato["errore_dal"] is None:
            stato["errore_dal"] = ora
        log("Verifica non riuscita ({} di fila): {}".format(
            stato["errori_consecutivi"], e))
        riepilogo_github(f"verifica non riuscita: {e}")
        minuti_ciechi = (ora - stato["errore_dal"]) / 60
        if (not stato["avviso_errore_inviato"]
                and stato["errori_consecutivi"] >= SOGLIA_ERRORI
                and minuti_ciechi >= SOGLIA_ERRORI_MIN):
            ok = notifica(
                cfg, "Sentinella: verifica non riuscita",
                "Dalle {} non è possibile leggere la disponibilità sul sito Apple "
                "(ultimo errore: {}). I controlli continuano; un nuovo avviso "
                "segnalerà il ripristino.".format(ora_breve(stato["errore_dal"]), e),
                priorita=3, tag=["warning"], collegamento=False)
            stato["avviso_errore_inviato"] = ok
            return ok
        return True

    consegna_ok = True
    if stato["avviso_errore_inviato"]:
        consegna_ok &= notifica(
            cfg, "Sentinella di nuovo operativa",
            f"La verifica funziona di nuovo. Stato attuale: {esito.testo}.",
            priorita=2, tag=["white_check_mark"], collegamento=False)
    stato.update(errori_consecutivi=0, errore_dal=None, avviso_errore_inviato=False)

    log(f"{esito.titolo}: {esito.testo} [{esito.codice}]")
    riepilogo_github(f"{esito.titolo}: {esito.testo} [{esito.codice}]")
    precedente = stato["disponibile"]

    if esito.disponibile:
        if precedente is not True:
            ok = notifica(
                cfg, f"DISPONIBILE: {esito.titolo}",
                f"{esito.testo}. Per bloccarlo, ordinalo subito online scegliendo il ritiro in "
                "negozio: tocca la notifica per aprire la pagina Apple.",
                priorita=5, tag=["rotating_light", "iphone"])
            if ok:  # se l'invio fallisce, l'avviso viene ritentato al giro successivo
                stato.update(disponibile=True, ultimo_avviso=ora)
            consegna_ok &= ok
        elif (cfg.promemoria_min > 0 and stato["ultimo_avviso"] is not None
              and ora - stato["ultimo_avviso"] >= cfg.promemoria_min * 60):
            ok = notifica(
                cfg, f"Ancora disponibile: {esito.titolo}",
                f"{esito.testo} (controllo delle {ora_breve(ora)}).",
                priorita=4, tag=["iphone"])
            if ok:
                stato["ultimo_avviso"] = ora
            consegna_ok &= ok
    else:
        if precedente is True:
            consegna_ok &= notifica(
                cfg, f"Non più disponibile: {esito.titolo}",
                f"{esito.testo}. La sentinella continua a controllare.",
                priorita=3, tag=["x"], collegamento=False)
        stato.update(disponibile=False, ultimo_avviso=None)
    return consegna_ok


def esegui_singolo(cfg):
    stato = carica_stato(cfg.file_stato, cfg.prodotto, cfg.negozio)
    prima = dict(stato)
    ok = esegui_controllo(cfg, stato)
    modificato = stato != prima
    if modificato:
        salva_stato(cfg.file_stato, stato)
    scrivi_output_github("stato_modificato", "true" if modificato else "false")
    return 0 if ok else 1


def esegui_ciclo(cfg):
    log(f"Sentinella {VERSIONE} avviata: prodotto {cfg.prodotto}, negozio {cfg.negozio}, "
        f"controllo ogni {cfg.intervallo} s circa. Ctrl+C per fermare.")
    stato = carica_stato(cfg.file_stato, cfg.prodotto, cfg.negozio)
    try:
        while True:
            prima = dict(stato)
            esegui_controllo(cfg, stato)
            if stato != prima:
                salva_stato(cfg.file_stato, stato)
            time.sleep(cfg.intervallo * random.uniform(0.85, 1.15))
    except KeyboardInterrupt:
        log("Sentinella fermata.")
        return 0


def esegui_test(cfg):
    try:
        esito = verifica(cfg.prodotto, cfg.negozio)
        verifica_ok = True
        log(f"{esito.titolo}: {esito.testo} [{esito.codice}]")
        riepilogo_github(f"test: {esito.titolo}: {esito.testo} [{esito.codice}]")
        titolo = "Test sentinella riuscito"
        messaggio = (f"Le notifiche funzionano. {esito.titolo}: {esito.testo} "
                     f"(controllo delle {ora_breve()}).")
        tag = ["test_tube", "white_check_mark"]
    except ErroreVerifica as e:
        verifica_ok = False
        log(f"Verifica non riuscita: {e}")
        riepilogo_github(f"test: verifica non riuscita: {e}")
        titolo = "Test sentinella: verifica Apple non riuscita"
        messaggio = ("Le notifiche funzionano, ma la lettura della disponibilità "
                     f"non è riuscita: {e}.")
        tag = ["test_tube", "warning"]
    inviata = notifica(cfg, titolo, messaggio, priorita=3, tag=tag)
    return 0 if (inviata and verifica_ok) else 1


# --- Configurazione -----------------------------------------------------------
def _intero_da_ambiente(nome, predefinito):
    valore = os.environ.get(nome, "").strip()
    try:
        return int(valore) if valore else predefinito
    except ValueError:
        sys.exit(f"Valore non valido per {nome}: {valore!r}")


def leggi_configurazione(argv=None):
    p = argparse.ArgumentParser(
        description="Sentinella di disponibilità Apple Store (ritiro in negozio) "
                    "con notifiche push ntfy.")
    p.add_argument("--topic", default=os.environ.get("NTFY_TOPIC", ""),
                   help="topic ntfy che riceve le notifiche (o variabile NTFY_TOPIC)")
    p.add_argument("--server", default=os.environ.get("NTFY_SERVER") or "https://ntfy.sh",
                   help="server ntfy (predefinito: https://ntfy.sh)")
    p.add_argument("--token", default=os.environ.get("NTFY_TOKEN", ""),
                   help="token di accesso ntfy, facoltativo (o variabile NTFY_TOKEN)")
    p.add_argument("--prodotto",
                   default=os.environ.get("SENTINELLA_PRODOTTO") or PRODOTTO_PREDEFINITO,
                   help="codice Apple del prodotto (predefinito: %(default)s)")
    p.add_argument("--negozio",
                   default=os.environ.get("SENTINELLA_NEGOZIO") or NEGOZIO_PREDEFINITO,
                   help="numero del negozio Apple (predefinito: %(default)s)")
    p.add_argument("--pagina", default=os.environ.get("SENTINELLA_PAGINA") or PAGINA_PRODOTTO,
                   help="pagina aperta toccando la notifica")
    p.add_argument("--stato", dest="file_stato",
                   default=os.environ.get("SENTINELLA_STATO") or FILE_STATO_PREDEFINITO,
                   help="file dello stato (predefinito: %(default)s)")
    p.add_argument("--promemoria", dest="promemoria_min", type=int,
                   default=_intero_da_ambiente("SENTINELLA_PROMEMORIA_MIN",
                                               PROMEMORIA_PREDEFINITO_MIN),
                   help="minuti tra i promemoria finché resta disponibile; 0 = nessuno "
                        "(predefinito: %(default)s)")
    p.add_argument("--intervallo", type=int, default=INTERVALLO_PREDEFINITO_S,
                   help="secondi tra i controlli in modalità --ciclo "
                        f"(minimo {INTERVALLO_MINIMO_S}, predefinito: %(default)s)")
    modalita = p.add_mutually_exclusive_group()
    modalita.add_argument("--test", action="store_true",
                          help="invia una notifica di prova con lo stato attuale")
    modalita.add_argument("--ciclo", action="store_true",
                          help="controllo continuo (computer acceso)")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSIONE)
    cfg = p.parse_args(argv)

    cfg.topic = cfg.topic.strip()
    cfg.token = cfg.token.strip()
    cfg.prodotto = cfg.prodotto.strip().upper()
    cfg.negozio = cfg.negozio.strip().upper()
    if not cfg.topic:
        p.error("manca il topic ntfy: usare --topic NOME o la variabile NTFY_TOPIC "
                "(su GitHub: secret NTFY_TOPIC)")
    if not RE_TOPIC.match(cfg.topic):
        p.error("topic ntfy non valido: solo lettere, cifre, '-' e '_', massimo 64 caratteri")
    if cfg.intervallo < INTERVALLO_MINIMO_S:
        p.error(f"--intervallo deve essere di almeno {INTERVALLO_MINIMO_S} secondi")
    if cfg.promemoria_min < 0:
        p.error("--promemoria non può essere negativo")
    return cfg


def main(argv=None):
    cfg = leggi_configurazione(argv)
    if cfg.test:
        return esegui_test(cfg)
    if cfg.ciclo:
        return esegui_ciclo(cfg)
    return esegui_singolo(cfg)


if __name__ == "__main__":
    sys.exit(main())
