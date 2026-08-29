"""
Bot Telegram "news digest": una volta al giorno raccoglie le notizie
importanti del giorno da Olanda (generale + Amsterdam/AT5), Italia
(generale), mondo (BBC), AI globale (VentureBeat + The Verge), tech globale
(TechCrunch + Ars Technica + Hacker News) e finanza globale (Yahoo
Finance) — e le manda su Telegram **una alla volta, ogni notizia come
messaggio a sé** — così ogni link ha la sua anteprima (e "Instant View" se
il sito la supporta) invece di finire tutto attaccato in un unico blocco.

Ordine di invio (priorità, dall'alto verso il basso):
1. Olanda (NOS generale + AT5 Amsterdam)
2. Italia (ANSA)
3. Mondo (BBC)
4. AI (VentureBeat + The Verge AI)
5. Tech (TechCrunch + Ars Technica + Hacker News) — AI e Tech ora sono
   adiacenti, non più separati dalla Finanza
6. Finanza (Yahoo Finance) — ultima

"Solo le notizie importanti": ogni feed configurato è già la selezione
editoriale "in evidenza" della testata (non un ordine puramente
cronologico), quindi prendere solo le prime N notizie di ciascun feed
(MAX_ITEMS_PER_CATEGORY, default 3) equivale a prendere solo le notizie
più rilevanti del giorno secondo quella fonte.

"Niente duplicati": AI e Tech usano più fonti apposta (VentureBeat/The
Verge/arXiv per AI, TechCrunch/Ars Technica/The Guardian/Hacker News per
tech) invece di più feed della stessa testata, che si sovrappongono per
natura. In più c'è un controllo di deduplicazione globale (per link
esatto e per titolo molto simile) che scarta un item se una notizia
praticamente identica è già stata mandata in una categoria precedente
nello stesso digest.

Questo controllo vale anche TRA un giorno e l'altro: i link e i titoli
mandati vengono salvati in `SENT_STATE_FILE` (default `.state/sent.json`)
e ricaricati al run successivo, con scadenza a `SENT_STATE_TTL_DAYS`
giorni. Serve perché gli schedule di GitHub Actions slittano e ogni tanto
saltano un giro: senza memoria tra i run, un digest partito in ritardo
riproporrebbe notizie già lette, e uno saltato ne farebbe perdere altre.
Con la memoria attiva `HOURS_LOOKBACK` può stare largo (30h) e coprire i
buchi senza il rischio di ripetere niente.

"Paper AI (arXiv)": categoria bonus, diversa dalle altre — non è
giornalismo ma gli ultimi paper accademici cs.AI pubblicati su arXiv
(API pubblica, nessuna chiave). Titoli spesso molto tecnici, per chi
vuole seguire anche la ricerca e non solo le notizie.

"The Guardian" (sezione Technology): fonte editoriale aggiuntiva per
Tech, tramite il suo feed RSS pubblico — nessuna chiave, nessuna
registrazione richiesta (a differenza dell'Open Platform API del
Guardian, scartata apposta per questo motivo).

Per le notizie olandesi (fonte NOS/AT5), ogni messaggio contiene sia il
titolo originale in olandese sia la sua traduzione in inglese. La
traduzione prova, in ordine, tre motori (tutti via `deep-translator`):
Google Translate -> MyMemory (entrambi gratuiti, senza API key, ma con
quote condivise che a volte si esauriscono) -> DeepL (serve una
`DEEPL_API_KEY` gratuita in `.env`, molto più affidabile; se non
impostata questo terzo tentativo viene saltato). Se falliscono tutti e
tre, il messaggio parte comunque con solo il titolo originale.

"Una sola notifica al giorno": Telegram manda un avviso (suono/vibrazione)
per ogni messaggio ricevuto, a meno che il messaggio non sia mandato con
`disable_notification=True` (in tal caso arriva comunque in chat, ma senza
avviso). Per evitare 30+ notifiche di fila, SOLO il primo messaggio del
digest ("📰 Digest notizie — ...") viene mandato con notifica attiva; tutti
i messaggi successivi (intestazioni di categoria + singole notizie) sono
silenziosi. Risultato: una sola notifica al giorno, il resto arriva senza
disturbare.

Non è un bot che "ascolta" messaggi in continuazione: è uno script che gira
UNA VOLTA e poi finisce — a farlo ripartire ogni mattina ci pensa il
workflow `.github/workflows/daily-digest.yml` su GitHub Actions (in locale
va bene un cron/systemd timer / Utilità di pianificazione di Windows,
vedi README.md).

IMPORTANTE: questo script deve girare su una macchina con accesso reale a
internet (il tuo PC, o una VPS come Oracle Cloud) — non funziona da un
ambiente sandbox con rete bloccata verso api.telegram.org.

Setup rapido:
1. pip install -r requirements.txt
2. Copia .env.example in .env e riempi i valori (o usa il tuo .env già pronto)
3. python news_digest_bot.py   (esecuzione singola, per testare)
4. Programmalo per farlo girare ogni mattina da solo (vedi README.md).
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("news-digest-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# 30 e non 24 apposta: gli schedule di GitHub Actions slittano (anche di
# mezz'ora abbondante) e ogni tanto un run viene saltato del tutto. Con 24
# esatte un run in ritardo lascia scoperto un buco di notizie mai viste. Le
# 6 ore di sovrapposizione lo coprono, e le notizie gia' mandate ieri non
# tornano perche' le filtra lo stato persistente della deduplicazione
# (vedi SENT_STATE_FILE). Non alzarlo senza quello stato attivo.
HOURS_LOOKBACK = int(os.environ.get("HOURS_LOOKBACK", "30"))
# Ridotto apposta: ogni feed qui sotto è già la selezione "in evidenza"
# della testata (non semplice ordine cronologico), quindi prendere solo le
# prime 3 significa prendere solo le notizie più importanti del giorno.
MAX_ITEMS_PER_CATEGORY = int(os.environ.get("MAX_ITEMS_PER_CATEGORY", "3"))
SECONDS_BETWEEN_MESSAGES = float(os.environ.get("SECONDS_BETWEEN_MESSAGES", "1.0"))
# Sopra questa soglia di similarità tra titoli (0-1), un item viene
# considerato un duplicato di uno già mandato e viene scartato.
DUPLICATE_TITLE_THRESHOLD = float(os.environ.get("DUPLICATE_TITLE_THRESHOLD", "0.6"))
# Dove salvare i link/titoli gia' mandati, per non ripeterli al run
# successivo. Senza questo file la deduplicazione vale solo DENTRO un
# singolo digest, e il "non ripetere" tra un giorno e l'altro dipende solo
# dal fatto che HOURS_LOOKBACK combaci con la cadenza del cron - cosa che
# smette di valere appena un run parte in ritardo. Su GitHub Actions il
# file viene conservato tra un run e l'altro da actions/cache (vedi
# .github/workflows/daily-digest.yml); in locale sta semplicemente nella
# cartella del progetto ed e' escluso da .gitignore.
SENT_STATE_FILE = Path(os.environ.get("SENT_STATE_FILE", ".state/sent.json"))
# Dopo quanti giorni una notizia gia' mandata viene dimenticata. 7 e' un
# compromesso: abbastanza lungo da coprire run saltati per giorni, ma non
# tanto da far crescere il file all'infinito (il confronto per similarita'
# di titolo e' lineare sul numero di voci ricordate).
SENT_STATE_TTL_DAYS = int(os.environ.get("SENT_STATE_TTL_DAYS", "7"))
# Opzionale: se impostata, abilita DeepL come terzo fallback di traduzione
# (più affidabile di Google/MyMemory). Vedi .env.example per come ottenerla
# gratis. Se lasciata vuota, il terzo fallback viene semplicemente saltato.
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "").strip()

# Fonti verificate — vedi il documento Notion "Bot Telegram: guida, esempi
# e progetti" per le note su ciascuna (uso personale non commerciale, ecc.).
# "lang": "nl" attiva la traduzione automatica in inglese per quella categoria.
# "source": "hackernews" salta il parsing RSS e usa l'API ufficiale di HN.
#
# ORDINE = ORDINE DI INVIO SU TELEGRAM. Rispetta questa priorità:
# Olanda -> Italia -> Mondo -> AI -> Tech -> Finanza.
# AI e Tech sono adiacenti apposta (più fonti ciascuna, ma raggruppate),
# la Finanza resta sempre per ultima e non le separa più.
FEEDS = [
    # --- 1. Olanda ---
    {"category": "🇳🇱 Olanda — generale (NOS)", "url": "https://feeds.nos.nl/nosnieuwsalgemeen", "lang": "nl"},
    {"category": "🏙️ Amsterdam (AT5)", "url": "https://www.at5.nl/rss", "lang": "nl"},
    # --- 2. Italia ---
    {"category": "🇮🇹 Italia — generale (ANSA)", "url": "https://www.ansa.it/sito/notizie/topnews/topnews_rss.xml", "lang": "it"},
    # --- 3. Mondo ---
    {"category": "🌍 Mondo (BBC)", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "lang": "en"},
    # --- 4. AI (globale, più fonti per non dipendere da una sola testata) ---
    {"category": "🤖 AI (VentureBeat)", "url": "https://venturebeat.com/category/ai/feed/", "lang": "en"},
    {"category": "🤖 AI (The Verge)", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "lang": "en"},
    # Bonus: paper di ricerca (non giornalismo) — arXiv espone un feed
    # Atom via query URL, gestito da feedparser come un normale "RSS".
    {
        "category": "📄 Paper AI (arXiv)",
        "url": "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=10",
        "lang": "en",
    },
    # --- 5. Tech (globale, adiacente all'AI, non più separata dalla finanza) ---
    {"category": "💻 Tech (TechCrunch)", "url": "https://techcrunch.com/feed/", "lang": "en"},
    {"category": "💻 Tech (Ars Technica)", "url": "https://arstechnica.com/feed/", "lang": "en"},
    {"category": "💻 Tech (The Guardian)", "url": "https://www.theguardian.com/uk/technology/rss", "lang": "en"},
    {"category": "💻 Tech — Hacker News (community)", "source": "hackernews", "lang": "en"},
    # --- 6. Finanza (sempre per ultima) ---
    {"category": "💰 Finanza globale (Yahoo Finance)", "url": "https://finance.yahoo.com/news/rssindex", "lang": "en"},
]

HACKER_NEWS_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HACKER_NEWS_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HACKER_NEWS_ITEMS_TO_FETCH = 3


def fetch_feed_items(url: str) -> list[dict]:
    """Scarica un feed RSS e ritorna gli item delle ultime HOURS_LOOKBACK ore
    (senza applicare ancora il limite per categoria: quello viene fatto
    DOPO la deduplicazione, per non scartare per errore un item unico solo
    perché uno duplicato lo precedeva)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_LOOKBACK)
    items = []
    try:
        parsed = feedparser.parse(url)
        for entry in parsed.entries:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
            items.append({"title": title, "link": entry.get("link", "")})
    except Exception as exc:  # noqa: BLE001
        log.warning("Errore nel feed %s: %s", url, exc)
    return items


def fetch_hacker_news_top() -> list[dict]:
    """Scarica i top post di Hacker News tramite l'API ufficiale (gratuita, no key)."""
    items = []
    try:
        top_ids = requests.get(HACKER_NEWS_TOP_URL, timeout=10).json()[:HACKER_NEWS_ITEMS_TO_FETCH]
        for item_id in top_ids:
            data = requests.get(HACKER_NEWS_ITEM_URL.format(id=item_id), timeout=10).json()
            items.append(
                {
                    "title": data.get("title", ""),
                    "link": data.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Errore nel recupero di Hacker News: %s", exc)
    return items


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _not_expired(entries: dict | None, cutoff: datetime) -> dict[str, str]:
    """Tiene solo le voci "chiave -> data ISO di invio" piu' recenti di
    `cutoff`. Una voce con data mancante o malformata viene scartata: se il
    file si corrompe e' meglio rimandare una notizia vecchia che portarsi
    dietro voci che non scadranno mai piu'."""
    kept: dict[str, str] = {}
    for key, sent_at in (entries or {}).items():
        try:
            if datetime.fromisoformat(sent_at) >= cutoff:
                kept[key] = sent_at
        except (TypeError, ValueError):
            continue
    return kept


class Deduplicator:
    """Tiene traccia dei link e dei titoli già mandati, per scartare
    notizie sostanzialmente identiche riportate da fonti diverse (es. la
    stessa notizia AI su VentureBeat e The Verge lo stesso giorno).

    Ricorda su due livelli: dentro il run corrente (due fonti che battono
    la stessa notizia stamattina) e TRA run diversi — `load()` rilegge cosa
    è stato mandato nei giorni scorsi e `save()` lo riscrive, scartando le
    voci più vecchie di `SENT_STATE_TTL_DAYS`. È il secondo livello a
    rendere il digest immune ai run che slittano o saltano: senza, il
    "non ripetere" dipende solo dal fatto che HOURS_LOOKBACK combaci con la
    cadenza del cron.

    Se lo stato manca o è illeggibile si riparte da zero senza fallire: la
    conseguenza peggiore è un digest che ripete qualcosa, non un digest
    che non parte."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        # Mappe "chiave -> data ISO di invio" (non set/list) perché serve
        # sapere quando una voce è stata mandata, per farla scadere.
        self.seen_links: dict[str, str] = {}
        self.seen_titles: dict[str, str] = {}

    def is_duplicate(self, title: str, link: str) -> bool:
        if link and link in self.seen_links:
            return True
        normalized = _normalize_title(title)
        for seen in self.seen_titles:
            if SequenceMatcher(None, normalized, seen).ratio() >= self.threshold:
                return True
        return False

    def remember(self, title: str, link: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if link:
            self.seen_links[link] = now
        self.seen_titles[_normalize_title(title)] = now

    def load(self, path: Path, ttl_days: int) -> None:
        """Ricarica lo stato dei run precedenti, ignorando le voci scadute."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("Nessuno stato in %s: primo run, o cache non ripristinata.", path)
            return
        except (OSError, ValueError) as exc:  # noqa: BLE001
            log.warning("Stato %s illeggibile (%s): riparto da zero.", path, exc)
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        self.seen_links = _not_expired(raw.get("links"), cutoff)
        self.seen_titles = _not_expired(raw.get("titles"), cutoff)
        log.info(
            "Stato caricato: %s link e %s titoli gia' mandati negli ultimi %s giorni.",
            len(self.seen_links),
            len(self.seen_titles),
            ttl_days,
        )

    def save(self, path: Path, ttl_days: int) -> None:
        """Riscrive lo stato potato delle voci scadute. Non solleva mai: a
        questo punto il digest è già stato inviato, e un salvataggio fallito
        costa al massimo qualche ripetizione domani."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        payload = {
            "links": _not_expired(self.seen_links, cutoff),
            "titles": _not_expired(self.seen_titles, cutoff),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            log.info("Stato salvato in %s (%s link ricordati).", path, len(payload["links"]))
        except OSError as exc:  # noqa: BLE001
            log.warning("Non sono riuscito a salvare lo stato in %s: %s", path, exc)


# Codici lingua nel formato che si aspetta MyMemory (fallback), diverso da
# quello di Google Translate (che usa solo "nl", "it", ecc.).
_MYMEMORY_LANG_CODES = {"nl": "nl-NL", "it": "it-IT"}


def translate_to_english(text: str, source_lang: str) -> str | None:
    """Prova, in ordine, Google Translate -> MyMemory -> DeepL (se
    `DEEPL_API_KEY` è impostata). I primi due sono gratuiti senza API key
    ma con quote condivise che possono esaurirsi o bloccare richieste
    automatiche (capita periodicamente, è un limite dei servizi gratuiti,
    non un bug qui). DeepL è opzionale ma molto più affidabile (500.000
    caratteri/mese gratis con una key personale). Ritorna None solo se
    TUTTI i tentativi disponibili falliscono, così il chiamante può
    mandare solo l'originale senza far fallire l'intero digest."""
    try:
        from deep_translator import GoogleTranslator

        result = GoogleTranslator(source=source_lang, target="en").translate(text)
        if result:
            return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Google Translate fallito per %r: %s", text[:50], exc)

    try:
        from deep_translator import MyMemoryTranslator

        mm_source = _MYMEMORY_LANG_CODES.get(source_lang, source_lang)
        result = MyMemoryTranslator(source=mm_source, target="en-GB").translate(text)
        if result:
            return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Anche il fallback MyMemory è fallito per %r: %s", text[:50], exc)

    if DEEPL_API_KEY:
        try:
            from deep_translator import DeeplTranslator

            result = DeeplTranslator(
                api_key=DEEPL_API_KEY, source=source_lang, target="en", use_free_api=True
            ).translate(text)
            if result:
                return result
        except Exception as exc:  # noqa: BLE001
            log.warning("Anche il fallback DeepL è fallito per %r: %s", text[:50], exc)

    return None


def send_telegram_message(text: str, silent: bool = True) -> bool:
    """Manda un singolo messaggio su Telegram. Gestisce il rate limit (429)
    riprovando dopo il tempo indicato da Telegram. Ritorna True se inviato.

    `silent=True` (default) manda il messaggio con `disable_notification`,
    così non genera un avviso sonoro/vibrazione — solo il primo messaggio
    del digest viene mandato con `silent=False`, per avere una sola
    notifica al giorno invece di una per ogni notizia."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[:4096],
                "disable_web_page_preview": False,
                "disable_notification": "true" if silent else "false",
            },
            timeout=15,
        )
        if resp.ok:
            return True
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
            log.warning("Rate limit Telegram, riprovo tra %s secondi...", retry_after)
            time.sleep(retry_after + 1)
            continue
        log.error("Errore invio Telegram: %s %s", resp.status_code, resp.text)
        return False
    return False


def send_category(category: str, items: list[dict], lang: str, dedup: Deduplicator) -> bool:
    """Filtra i duplicati, applica il limite per categoria e manda i
    messaggi (tutti silenziosi: solo il primo messaggio del digest, in
    main(), genera una notifica). Ritorna True se almeno una notizia è
    stata inviata."""
    fresh_items = []
    for item in items:
        if dedup.is_duplicate(item["title"], item["link"]):
            log.info("Scartato duplicato: %s", item["title"][:70])
            continue
        dedup.remember(item["title"], item["link"])
        fresh_items.append(item)
        if len(fresh_items) >= MAX_ITEMS_PER_CATEGORY:
            break

    if not fresh_items:
        return False

    send_telegram_message(f"— {category} —", silent=True)
    time.sleep(SECONDS_BETWEEN_MESSAGES)

    for item in fresh_items:
        title = item["title"]
        link = item["link"]
        if lang == "nl":
            translation = translate_to_english(title, source_lang="nl")
            if translation:
                message = f"🇳🇱 {title}\n🇬🇧 {translation}\n\n{link}"
            else:
                message = f"🇳🇱 {title}\n\n{link}"
        else:
            message = f"{title}\n\n{link}"

        send_telegram_message(message, silent=True)
        time.sleep(SECONDS_BETWEEN_MESSAGES)

    return True


def main() -> None:
    log.info("Raccolta notizie dalle ultime %s ore...", HOURS_LOOKBACK)

    today = datetime.now().strftime("%d/%m/%Y")
    # Unico messaggio "rumoroso" del digest: genera la sola notifica del
    # giorno. Tutto il resto (sotto) viene mandato silenzioso.
    send_telegram_message(f"📰 Digest notizie — {today}", silent=False)
    time.sleep(SECONDS_BETWEEN_MESSAGES)

    dedup = Deduplicator(threshold=DUPLICATE_TITLE_THRESHOLD)
    dedup.load(SENT_STATE_FILE, SENT_STATE_TTL_DAYS)
    any_sent = False

    for feed in FEEDS:
        if feed.get("source") == "hackernews":
            items = fetch_hacker_news_top()
        else:
            items = fetch_feed_items(feed["url"])
        if send_category(feed["category"], items, feed["lang"], dedup):
            any_sent = True

    if not any_sent:
        send_telegram_message("Nessuna notizia nuova trovata nelle ultime ore.", silent=True)

    # Sempre dopo l'invio, mai prima: se il bot muore a metà digest è meglio
    # ripetere domani qualche notizia che marcarne come "mandate" alcune che
    # non sono mai arrivate.
    dedup.save(SENT_STATE_FILE, SENT_STATE_TTL_DAYS)

    log.info("Digest inviato su Telegram (una sola notifica, resto silenzioso, duplicati scartati).")


if __name__ == "__main__":
    main()
