# Ändringslogg

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [1.0.2] — 2026-05-15

### Åtgärdat

- `sok_i_domtext` returnerade `null` för `domstolkod`, `avgorandedatum`, `benamning` och
  `sammanfattning` när `hamta_pdf` anropats utan föregående `hamta_avgorande`. Joinen mot
  `avgorande_cache` gav inga träffar eftersom metadata aldrig cachats.
  **Fix:** ny hjälpfunktion `_sakerstall_avgorande_cache(avgorande_id)` anropas från
  `hamta_pdf` i båda flödena — vid ny nedladdning och vid cache-träff (retroaktiv fyllning).
  Metadata hämtas från API:et och skrivs till `avgorande_cache` om posten saknas.
  Verifierat: `domstolkod=HDO`, `avgorandedatum`, `benamning` och `sammanfattning` nu
  korrekt ifyllda i `sok_i_domtext`-svaret även utan föregående `hamta_avgorande`-anrop.

### Verifierat

- Alla 6 MCP-verktyg testade och godkända i Claude Desktop (Cowork-session 2026-05-15):
  - `sok_rattpraxis` — 1 393 HD-skadeståndsdomar returnerade korrekt
  - `hamta_avgorande_pa_beteckning` — NJA 2025:67 hämtad med HTML-fulltext och kompanjon
  - `sok_rattpraxis_for_lagrum` — paragraffiltrering verifierad (BrB 36 §, 5 träffar)
  - `hamta_avgorande` — fullständig metadata med alla fält hämtad korrekt
  - `hamta_pdf` — PDF-text extraherad och cachad (HD FT 9974-24, 11 sidor)
  - `sok_i_domtext` — FTS-sökning med fullständig metadata verifierad efter buggfix

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
- `hamta_avgorande` — hämta fullständigt avgörande via UUID, med metadata-cache (TTL-styrd)
- `hamta_pdf` — hämta och extrahera PDF-text via pymupdf4llm, med lokal PDF-textcache (TTL-styrd)
- `sok_rattpraxis_for_lagrum` — paragrafnivåsökning: SFS-nummer + valfri paragraf med klientsidefiltrering
- `hamta_avgorande_pa_beteckning` — sökning på NJA-nummer, HFD-referat, kortnamn och målnummer
- `sok_i_domtext` — fulltext-sökning inuti cachade domtexter (PostgreSQL FTS med ts_headline; SQLite LIKE-fallback)
- Intern `_hamta_grupp_kompanjon()` — slår upp syskonpublicering (DOM_ELLER_BESLUT ↔ REFERAT) via benamning
- PostgreSQL-schema `rattspraxis` med tabellerna `avgorande_cache` och `pdf_cache`
- `pdf_cache.text_tsv` — genererad tsvector-kolumn med GIN-index för svensk FTS
- Stöd för stdio- och HTTP-transport (MCP_TRANSPORT i .env)
- Bearer-token-autentisering i HTTP-läge (MCP_API_KEY)
- FD 1-skydd runt pymupdf4llm-anrop via `_tysta_fd1()`
