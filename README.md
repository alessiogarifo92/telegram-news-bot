# News Digest Bot — Telegram

Manda ogni mattina un digest delle notizie su Telegram, in quest'ordine di
priorità: Olanda (NOS + AT5 Amsterdam) → Italia (ANSA) → Mondo (BBC) → AI
(VentureBeat + The Verge + paper arXiv) → Tech (TechCrunch + Ars Technica +
The Guardian + Hacker News) → Finanza (Yahoo Finance). AI e Tech sono
raggruppate una dopo l'altra, non più separate dalla Finanza. Non è un bot
"in ascolto": è uno script che gira una volta al giorno (lo programmi tu
con cron/systemd) e poi finisce.

**Una sola notifica al giorno**: ogni categoria manda ancora un messaggio
per notizia (per avere l'anteprima/Instant View su ogni link), ma solo il
primissimo messaggio del digest genera un avviso sonoro su Telegram —
tutti gli altri sono mandati silenziosi (`disable_notification`). Quindi
in bacheca vedi comunque tutte le notizie separate, ma il telefono vibra
una volta sola.

## Passaggi che puoi fare solo tu (con il tuo account Telegram)

1. **Crea il bot**: apri Telegram, scrivi a `@BotFather`, manda `/newbot`,
   dai un nome e uno username al bot, copia il **token** che ti restituisce.
2. **Trova il tuo chat ID**: scrivi a `@userinfobot` su Telegram e copia il
   numero che ti dà (è il tuo ID numerico personale) — oppure scrivi
   `/start` al tuo nuovo bot e guarda i log al primo avvio dello script
   (vedi sotto, stampa un errore con l'update ricevuto se il chat ID non è
   ancora impostato correttamente).
3. **(Opzionale, consigliato) Chiave DeepL** per traduzioni NL→EN più
   affidabili: vai su https://www.deepl.com/pro#developer, crea un account
   gratuito piano "DeepL API Free" (nessuna carta richiesta), copia la key
   e incollala in `.env` come `DEEPL_API_KEY`. Senza questa key il bot usa
   solo Google Translate + MyMemory (gratuiti ma con quote condivise che
   ogni tanto si esauriscono) e, se falliscono entrambi, manda comunque il
   titolo originale in olandese senza traduzione.

## Setup ed esecuzione di prova

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# apri .env e incolla TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID
python news_digest_bot.py
```

Se tutto è configurato bene, entro pochi secondi ricevi il primo digest sul
tuo bot Telegram.

## ⚠️ Dove deve girare

Questo script deve girare su una macchina con **accesso reale a internet**
verso `api.telegram.org`: il tuo PC/Mac, oppure una VPS come **Oracle Cloud
Always Free** (vedi il documento Notion "OpenClaw: guida completa" per come
crearla gratis). Non funziona da ambienti sandbox con rete ristretta.

## Farlo girare da solo ogni giorno

**Opzione A — cron (Linux/macOS), es. ogni giorno alle 7:00:**

```bash
crontab -e
# aggiungi questa riga (adatta i percorsi):
0 7 * * * cd /home/ubuntu/telegram-news-bot && .venv/bin/python news_digest_bot.py >> digest.log 2>&1
```

**Opzione B — systemd timer (consigliata su una VPS Linux):**

```ini
# /etc/systemd/system/news-digest-bot.service
[Unit]
Description=News Digest Bot

[Service]
Type=oneshot
WorkingDirectory=/home/ubuntu/telegram-news-bot
ExecStart=/home/ubuntu/telegram-news-bot/.venv/bin/python news_digest_bot.py
EnvironmentFile=/home/ubuntu/telegram-news-bot/.env
```

```ini
# /etc/systemd/system/news-digest-bot.timer
[Unit]
Description=Esegue il News Digest Bot ogni mattina alle 7:00

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now news-digest-bot.timer
```

## Sul costo del riassunto AI

`USE_AI_SUMMARY=true` (con `ANTHROPIC_API_KEY` impostata) fa selezionare e
riassumere le notizie da Claude invece di mandarti titoli grezzi — molto
più leggibile. È fatturazione API separata dall'abbonamento Claude.ai, ma
per **una chiamata al giorno** il costo è irrisorio (si parla di centesimi
al mese, non di più). Se preferisci zero costi aggiuntivi, imposta
`USE_AI_SUMMARY=false`: ricevi comunque il digest, solo senza selezione né
riassunto — semplici titoli e link raggruppati per categoria.

## Estenderlo

- **Finanza**: non esiste un RSS gratuito affidabile per fonti finanziarie
  serie (Bloomberg/FT/WSJ sono a pagamento) — se ti serve, si può integrare
  Alpha Vantage (ha un tier gratuito) per dati/notizie di mercato.
- **Più fonti**: aggiungi voci alla lista `FEEDS` in `news_digest_bot.py`
  con qualunque feed RSS che vuoi seguire. L'ORDINE della lista è l'ordine
  di invio su Telegram: rispetta la priorità Olanda → Italia → Mondo → AI
  → Tech → Finanza (Finanza sempre per ultima). Altre fonti AI/Tech valide
  oltre a quelle già configurate: MIT Technology Review
  (`https://www.technologyreview.com/topic/artificial-intelligence/feed`
  per AI, `https://www.technologyreview.com/feed/` per tech), Wired
  (`https://www.wired.com/feed/tag/ai/latest/rss` per AI,
  `https://www.wired.com/feed/rss` per tech generale), The Register
  (`https://www.theregister.com/headlines.atom`).
- **Verificare un nuovo feed**: dal sandbox di Claude/Cowork non si può
  testare (rete bloccata anche verso i siti RSS, non solo verso
  Telegram) — prova il feed direttamente da PowerShell con
  `python -c "import feedparser; print(feedparser.parse('URL').entries[:2])"`
  prima di lasciarlo nello script.
- **Orario diverso o più digest al giorno**: cambia `OnCalendar` nel timer
  systemd (es. `07:00,19:00` per due digest al giorno).
