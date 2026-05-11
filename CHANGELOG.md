# Ändringslogg

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/).

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
