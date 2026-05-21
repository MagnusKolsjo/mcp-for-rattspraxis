# MCP-server för Sveriges Domstolars rättspraxis

En MCP-server som ger AI-verktyg med stöd för MCP-protokollet tillgång till Domstolsverkets öppna rättspraxis-databas med drygt 17 000 vägledande avgöranden från svenska överrätter sedan 1981.

## Vad servern gör

Servern exponerar sex verktyg:

- **sok_rattpraxis** — söker i hela rättspraxis-databasen med filter på domstol, datum, SFS-nummer och rättsområde
- **hamta_avgorande** — hämtar ett fullständigt avgörande med metadata, lagrum, förarbeteshänvisningar och EU-rättshänvisningar
- **hamta_pdf** — hämtar och extraherar text ur PDF-bilaga (nödvändigt för HD och MÖD som saknar HTML-fulltext); extraherad text cachas lokalt
- **sok_rattpraxis_for_lagrum** — söker praxis kopplad till en specifik paragraf i en lag, t.ex. alla HD-domar om 36 § avtalslagen
- **hamta_avgorande_pa_beteckning** — söker på NJA-nummer (NJA 2025:67), HFD-referat (HFD 2026 ref. 1), HD:s kortnamn eller målnummer
- **sok_i_domtext** — söker fulltext inuti cachade domtexter; PostgreSQL ger avancerad FTS med relevansrankning och kontextutdrag, SQLite ger enklare LIKE-sökning

### Arkitektur: cache på begäran

Servern laddar inga avgöranden i förväg. Metadata och PDF-texter hämtas från Domstolsverkets API vid det första anropet och lagras sedan i databasen med konfigurerbar TTL. Sökning med `sok_i_domtext` täcker därför bara avgöranden som redan hämtats via `hamta_avgorande` eller `hamta_pdf` i tidigare sessioner.

### Korsreferenser i rättskedjan

Varje avgörande innehåller maskinläsbara hänvisningar som möjliggör navigering i hela rättskedjan:

- `lagrumLista[].sfsNummer` — kopplar till lagstiftningen (SFSR-kompatibelt format)
- `forarbeteLista` — kopplar till propositioner och utredningar (riksdagsformat)
- `europarattsligaAvgorandenLista` — lista med strängar som beskriver typen av europarättslig koppling: `"Europarättsligt avgörande"` och/eller `"Mänskliga rättigheter"`. Tom lista om ingen europarättslig koppling finns. Faktiska CELEX- och ECLI-nummer återfinns enbart i domtexten (HTML-fulltext) eller i `hanvisadePubliceringarLista` som fritext.
- `hanvisadePubliceringarLista` — fritext med hänvisningar till andra avgöranden och källor, inklusive EU-domstolens CELEX-beteckningar (t.ex. `C-30/19, EU:C:2021:269`) och Europadomstolens målnummer (t.ex. `Application no. 44306/98`)

## Krav

- Python 3.11 eller senare
- PostgreSQL (för `sok_i_domtext` med fulltext-sökning) eller SQLite (med enklare LIKE-sökning)
- Internetanslutning mot `https://rattspraxis.etjanst.domstol.se`

## Installation

```bash
git clone https://github.com/MagnusKolsjo/mcp-for-rattspraxis.git
cd mcp-for-rattspraxis
python3 -m venv .venv --without-pip
.venv/bin/python3 -m ensurepip
.venv/bin/python3 -m pip install mcp requests python-dotenv pymupdf4llm psycopg2-binary starlette uvicorn
cp config.example.env .env
```

Redigera `.env` och ange din `DATABASE_URL`.

## Konfiguration i MCP-kompatibla AI-verktyg

Lägg till följande block i konfigurationsfilen för ditt AI-verktyg:

```json
"rattspraxis": {
  "command": "/absolut/sökväg/till/.venv/bin/python3",
  "args": ["/absolut/sökväg/till/mcp_server.py"],
  "cwd": "/absolut/sökväg/till/katalogen"
}
```

## Databasschema

Servern skapar automatiskt schemat `rattspraxis` i din PostgreSQL-databas (eller tabellerna direkt om SQLite används) vid första uppstarten:

- `rattspraxis.avgorande_cache` — cachad metadata per avgörande (TTL-styrd)
- `rattspraxis.pdf_cache` — extraherad PDF-text med GIN-index för svensk fulltext-sökning

## Licens

AGPLv3 — se LICENSE.

Domstolsverkets rättspraxis är offentliga handlingar.
API: https://rattspraxis.etjanst.domstol.se/api/v1/
