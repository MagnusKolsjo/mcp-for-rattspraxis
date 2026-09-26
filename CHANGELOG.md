# Ändringslogg

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.0.0] — 2026-09-26

### Tillagt

- **Minnesvakt, sidblock och OCR-kö i PDF-extraktionen.** `pdftext.py` extraherar nu
  via `pdftext_skydd.py`: varje PDF läses blockvis (`RP_PDF_SIDBLOCK`, standard 20
  sidor) i en egen process, med en vakt i föräldraprocessen som avbryter blocket om
  det passerar en minnesgräns (`RP_PDF_MAX_MINNE_MB`, standard 3000 MB) eller
  tidsgräns (`RP_PDF_TIDSGRANS_S`, standard 300 s) — en enskild bildtung PDF kan då
  inte längre fälla processen. Ett block som passerar gränsen läses i stället med ren
  textutvinning. Dokument där någon sida saknar textlager, eller där ett block fick
  läsas med ren textutvinning, noteras i en OCR-kö (`ocr_ko/ko.jsonl` +
  `ocr_ko/filer/`, mapp konfigurerbar med `RP_OCR_KO_MAPP`) för att köras genom en
  bättre OCR senare.
- **Daglig synk av hela korpusen.** `01_synka_publiceringar.py` hämtar Domstolsverkets
  publiceringar via `GET /publiceringar` (sorterat på publiceringstid, med
  `publicerad_fran_och_med` och sidindelning) och lagrar dem i `avgorande_cache`.
  Första körningen är en fullsynk (drygt 17 000 publiceringar, ungefär 10–15 minuter);
  därefter hämtas bara det som publicerats sedan förra körningen. `synk_daglig.sh`
  kör synken med loggning, och `--installera-schema` lägger in den i launchd eller cron.
  Wrappern läser inte in `.env` med `source`, så värden med `&`, `$` eller citattecken
  (t.ex. i `DATABASE_URL`) går bra; sökvägar med blanksteg citeras i cron-raden.
- **PDF-texterna i synken.** Med `--med-pdf` (eller `--bara-pdf`) hämtar
  `01_synka_publiceringar.py` även PDF-bilagorna till publiceringar som saknar
  HTML-fulltext — domar och beslut från bland andra HD och MÖD, omkring 700 filer — och
  lagrar texten i `pdf_cache`, så att de blir sökbara i fulltext med `sok_i_domtext`.
  Steget är återupptagbart, pausar två sekunder mellan filerna och körs av
  `synk_daglig.sh`. `tackning` i `sok_i_domtext` visar hur stor andel av
  PDF-publiceringarna som har sin text lagrad.
- **`sok_i_domtext` söker i hela korpusen** efter en fullsynk: benämning, referatnummer,
  sammanfattning, nyckelord och HTML-fulltext för varje lokalt lagrat avgörande, utöver
  PDF-texterna. Varje träff har fältet `kalla` (`avgorande` eller `pdf`), och svaret
  har fältet `tackning` som visar hur mycket av korpusen som finns lokalt. Sökningen
  räknas som heltäckande bara efter en fullsynk och om den senaste lyckade synken är
  högst `RP_TACKNING_MAX_DAGAR` (standard 3) dagar gammal; `tackning` visar datumet
  för den senaste lyckade synken och synkens status.
- **Sökförfiningar i `sok_rattpraxis`.** Den nya valfria parametern `forfiningar`
  (standard `false`) ger fältet `forfiningar`: antal träffar per domstol, SFS-nummer,
  rättsområde, nyckelord, avgörandetyp och publiceringsform, via `POST /sokforfiningar`.
- `max_tecken` och `fran_tecken` i `hamta_avgorande`. HTML-fulltexten (`innehall_html`)
  kapas som standard vid 60 000 tecken, även i `hamta_avgorande_pa_beteckning`, eftersom
  svaret nu skickas både som text och som strukturerat innehåll. En kapad text avslutas
  med en rad som anger hur mycket som visas och hur resten läses, och de nya fälten
  `innehall_tecken_totalt` och `innehall_trunkerad` visar läget. Kapningen sker aldrig
  inne i en HTML-tagg.
- Verktygen har titlar, MCP-annotationer (alla är läsande) och utdataschema; svaren
  skickas även som `structuredContent`. `hamta_pdf` returnerar domtexten som ren text.

### Ändrat

- Texterna är produktneutrala: README, konfigurationsexempel, kommentarer och äldre CHANGELOG-poster nämner MCP-klienten i stället för en viss klient.
- User-Agent-strängen följer huvudversionen: `mcp-for-rattspraxis/2.0`.
- **Brytande:** servern kräver `mcp>=2.0,<3` och bygger på `MCPServer`.
- **Brytande:** http-läget kräver `MCP_API_KEY`. Utan nyckel startar servern inte
  (exitkod 2); tidigare startade den utan autentisering med en varning. Fel nyckel ger
  403, saknad header 401.
- **Brytande:** förväntade fel returneras som verktygsfel (`isError`) i stället för som
  text i ett lyckat svar. Det gäller bland annat okänt `avgorande_id`,
  `hamta_avgorande_pa_beteckning` utan träff (tidigare `{"hittades": false, ...}`),
  PDF-fel och källan som inte svarar.
- Kompanjonen (dom eller beslut ↔ referat) hämtas via `GET /publiceringar/grupp/{id}`
  i stället för en sökning på benämningen. Avgöranden utan benämning, som de flesta från
  HFD, får nu också sin kompanjon.
- `sok_i_domtext` med SQLite returnerar samma fält som med PostgreSQL (utom `relevans`).
- Anropen mot källan är samlade i `klient.py` och PDF-extraktionen i `pdftext.py`,
  så att servern och synken använder samma kod.
- PDF-extraktionen körs under ett lås, eftersom verktygen körs på arbetstrådar och
  PyMuPDF inte är trådsäkert.
- Databasschemat: kolumnen `sokbar_text` (med GIN-index i PostgreSQL) i
  `avgorande_cache` och tabellen `synk_status`, som migrationer. Befintliga rader fylls
  i vid första start.

### Rättat

- **OCR-språket i PDF-extraktionen var engelska.** `pymupdf4llm` OCR:ar sidor utan
  textlager på engelska om inget annat anges, vilket gav felaktiga tecken i svensk
  text. `pdftext.py` anger nu uttryckligen `swe+eng` (`RP_OCR_SPRAK`).
- Läs vidare-raden i ett kapat svar från `hamta_pdf` pekade på
  `fran_tecken + max_tecken`. Kapningen sker på ordgräns, så nästa utdrag hoppade
  över det avkapade ordet, och med `fran_tecken` nära slutet pekade raden bortom
  texten. Raden anger nu utdragets faktiska slut och är ett komplett anrop med
  `fillagring_id`, `max_tecken` och, när det angetts, `avgorande_id`. Det sista
  utdraget har ingen läs vidare-rad.
- `datum_fran` och `datum_till` i `sok_rattpraxis` och `sok_rattpraxis_for_lagrum`
  ignorerades av API:et, som läser datumen ur `filter.intervall`. Sökningarna var i
  praktiken ofiltrerade i tid.
- `ar_vagledande` ignorerades som filter (API:et har inget sådant fält) och var alltid
  `null` i svaren. Det uttrycks nu som avgörandetyp: prejudikat och vägledande
  avgöranden mot ej vägledande avgöranden och beslut om prövningstillstånd.
- `hamta_pdf` fick 406 från API:et för varje PDF som inte redan låg i cachen, eftersom
  bilagor begärdes som `application/octet-stream` i stället för `application/pdf`.
- Ett okänt `avgorande_id` gav ett JSON-tolkningsfel; API:et svarar med tom kropp.
- Ett databasfel i `sok_i_domtext` visas som ett begripligt fel; databasens eget
  felmeddelande loggas i stället för att skickas till klienten.
- `DATABASE_URL=sqlite:////absolut/sökväg.db` tolkades som en sökväg relativt
  servermappen.

### Borttaget

- `pdftext.py`s egna fd-omdirigering (`_tysta_fd1`) kring PDF-extraktionen.
  Extraktionen körs nu i en egen process i `pdftext_skydd.py`, så den behövs inte
  längre för att hålla `pymupdf4llm`s utskrifter borta från stderr.
- Den egna Starlette-appen för http-läget; transporten sköts av `mcp_transport.py`.
- `starlette` och `uvicorn` som egna rader i `requirements.txt` (de följer med `mcp`).

## [1.2.0] — 2026-08-10

### Tillagt

- **`max_tecken` och `fran_tecken` i `hamta_pdf`.** Verktyget returnerade hela den
  extraherade domtexten utan möjlighet att begränsa, vilket för långa avgöranden
  riskerade att överskrida MCP-protokollets storleksgräns per svar utan väg runt.
  Ett kapat svar avslutas med en rad i klartext:
  `[Visar tecken 1–246 av 6 360. Läs vidare: hamta_pdf(fillagring_id="…", fran_tecken=250)]`.
  Kapningen sker på ord- eller radgräns, aldrig mitt i ett ord.

  PDF-cachen lagrar fortfarande hela texten — trunkeringen gäller bara svaret till
  anroparen, så `sok_i_domtext` påverkas inte.

### Bakgrund

Genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet — storlek,
trunkering, adressering och sökning"). Additiva parametrar — inga brytande ändringar.
Att en dom kan kapas utan att det syns är särskilt allvarligt i den här strömmen,
eftersom domskäl citeras ordagrant.

## [1.1.1] — 2026-05-22

### Åtgärdat

- `.DS_Store` borttagen ur git-historiken; `.gitignore` uppdaterad

## [1.1.0] — 2026-05-22

### Tillagt

- `db.py` — databaslagret bryts ut till en separat modul för tydligare ansvarsfördelning
- `hamta_avgorande` exponerar nu tre tidigare dolda fält: `publiceringstid`, `litteratur`
  (litteraturlista) och `ecli_nummer`
- Arkitekturavsnitt i README som förklarar cache-på-begäran-designen

### Ändrat

- HTTP-transport migrerad från SSE (`/sse`-endpoint) till Streamable HTTP
  (`/mcp`-endpoint via `StreamableHTTPSessionManager`). **Brytande ändring** — kräver
  uppdatering av MCP-klienter som använder HTTP-transport.
- `hamta_avgorande`: parametern `id` omdöpt till `avgorande_id`. **Brytande ändring** —
  kräver uppdatering av klienter som anropar verktyget direkt.
- `sok_rattpraxis_for_lagrum`: paragraffilter kontrollerar nu både `referens`- och
  `sfsNummer`-fälten, vilket ger fler korrekta träffar vid paragrafnivåsökning
- `_ar_postgres()`: identifierar nu även `postgres://`-URL:er (utan `-ql`-suffix) som
  PostgreSQL-anslutningar
- `_hamta_db()`: SQLite-sökvägsextrahering använder nu `urlparse` för korrekt hantering
  av `sqlite:///`-URI:er
- `_MAX_PER_SIDA` kommenterad med motiveringen bakom värdet 50
- `README.md`: `europarattsligaAvgorandenLista` beskrivs nu korrekt som en lista med
  strängvärden (`"Europarättsligt avgörande"`, `"Mänskliga rättigheter"`) i stället för
  som ett binärt flaggfält
- `README.md`: terminologin "rekommenderas"/"fallback" ersatt med en neutral beskrivning
  av respektive backends egenskaper
- `config.example.env`: omstrukturerad med ASCII-avgränsare, domänprefixade grupper,
  `<VERSALER>`-platshållare och generationskommando för `MCP_API_KEY`

### Åtgärdat

- Schemainitiering (`_sakerstall_schema`) är nu kommenterad med baseline-version och
  ett tomt migreringsblock, i enlighet med projektets migrationsmönster

## [1.0.2] — 2026-05-15

### Åtgärdat

- `sok_i_domtext` returnerade `null` för `domstolkod`, `avgorandedatum`, `benamning` och
  `sammanfattning` när `hamta_pdf` anropats utan föregående `hamta_avgorande`. Joinen mot
  `avgorande_cache` gav inga träffar eftersom metadata aldrig cachats.
  **Fix:** ny hjälpfunktion `_sakerstall_avgorande_cache(avgorande_id)` anropas från
  `hamta_pdf` i båda flödena — vid ny nedladdning och vid cache-träff (retroaktiv fyllning).
  Metadata hämtas från API:et och skrivs till `avgorande_cache` om posten saknas.

## [1.0.1] — 2026-05-11

### Tillagt

- Automatisk alias-expansion för historiska domstolsnamn:
  `HFD` ↔ `REGR` (Regeringsrätten, namnbyte 1 jan 2011) och
  `MMOD` ↔ `MOD` (Miljööverdomstolen, namnbyte 2 maj 2011).
  Ange endera koden för att söka hela beståndet oavsett vilket år avgörandet gäller.
- `_expandera_domstolkoder()` tillämpas i `sok_rattpraxis` och `sok_i_domtext`.

## [1.0.0] — 2026-05-11

### Tillagt

- `sok_rattpraxis` — fritextsökning med filter (domstol, datum, SFS-nummer, rättsområde, nyckelord)
- `hamta_avgorande` — hämtar fullständigt avgörande via UUID, med metadata-cache (TTL-styrd)
- `hamta_pdf` — hämtar och extraherar PDF-text via pymupdf4llm, med lokal PDF-textcache (TTL-styrd)
- `sok_rattpraxis_for_lagrum` — paragrafnivåsökning: SFS-nummer + valfri paragraf med klientsidefiltrering
- `hamta_avgorande_pa_beteckning` — sökning på NJA-nummer, HFD-referat, kortnamn och målnummer
- `sok_i_domtext` — fulltext-sökning inuti cachade domtexter (PostgreSQL FTS med ts_headline; SQLite LIKE)
- Intern `_hamta_grupp_kompanjon()` — slår upp syskonpublicering (DOM_ELLER_BESLUT ↔ REFERAT) via benamning
- PostgreSQL-schema `rattspraxis` med tabellerna `avgorande_cache` och `pdf_cache`
- `pdf_cache.text_tsv` — genererad tsvector-kolumn med GIN-index för svensk FTS
- Stöd för stdio- och HTTP-transport (MCP_TRANSPORT i .env)
- Bearer-token-autentisering i HTTP-läge (MCP_API_KEY)
- FD 1-skydd runt pymupdf4llm-anrop via `_tysta_fd1()`
