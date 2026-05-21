# Ändringslogg

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
