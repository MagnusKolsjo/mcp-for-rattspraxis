# MCP-server för Sveriges Domstolars rättspraxis

En MCP-server som ger AI-verktyg med stöd för MCP-protokollet tillgång till Domstolsverkets öppna rättspraxis-databas med drygt 17 000 vägledande avgöranden från svenska överrätter sedan 1981.

## Vad servern gör

Servern exponerar sex verktyg:

- **sok_rattpraxis** — söker i hela rättspraxis-databasen med filter på domstol, datum, SFS-nummer, rättsområde och nyckelord. Med `forfiningar=true` redovisas också hur träffarna fördelar sig på domstolar, lagar, rättsområden, nyckelord, avgörandetyper och publiceringsformer, med antal per värde — underlag för att snäva in en bred sökning
- **hamta_avgorande** — hämtar ett fullständigt avgörande med metadata, lagrum, förarbeteshänvisningar och EU-rättshänvisningar, och vid behov syskonpubliceringen (dom eller beslut ↔ referat)
- **hamta_pdf** — hämtar och extraherar text ur PDF-bilaga (nödvändigt för HD och MÖD som saknar HTML-fulltext); extraherad text cachas lokalt. Tar `max_tecken` och `fran_tecken` för långa domar — ett kapat svar avslutas med en rad som anger hur mycket som visas och hur resten hämtas
- **sok_rattpraxis_for_lagrum** — söker praxis kopplad till en specifik paragraf i en lag, t.ex. alla HD-domar om 36 § avtalslagen
- **hamta_avgorande_pa_beteckning** — söker på NJA-nummer (NJA 2025:67), HFD-referat (HFD 2026 ref. 1), HD:s kortnamn eller målnummer
- **sok_i_domtext** — söker fulltext i de avgöranden som finns i den lokala databasen: HTML-fulltext, sammanfattning och benämning, samt PDF-texter som hämtats med `hamta_pdf`. PostgreSQL ger fulltextsökning med relevansrankning och kontextutdrag, SQLite enklare delsträngssökning. Svaret visar i fältet `tackning` hur mycket av korpusen som finns lokalt

Alla verktyg är läsande och bär MCP-annotationer. Förväntade fel — okänt id, ingen träff på en beteckning, källan svarar inte — returneras som verktygsfel (`isError`) med ett meddelande på svenska.

### Arkitektur: cache och daglig synk

Metadata och PDF-texter hämtas från Domstolsverkets API vid det första anropet och lagras i databasen med konfigurerbar TTL. Synkskriptet `01_synka_publiceringar.py` fyller dessutom databasen med samtliga publiceringar, så att `sok_i_domtext` söker i hela korpusen och inte bara i det som råkat hämtas tidigare.

`sok_i_domtext` blir heltäckande först efter en första fullsynk. Utan synk söker verktyget bara i de avgöranden som hämtats med `hamta_avgorande`, `hamta_avgorande_pa_beteckning` eller `hamta_pdf`, och svaret säger det. Domar och beslut från HD och MÖD som bara finns som PDF är sökbara på sammanfattningen tills PDF:en hämtats med `hamta_pdf`; synken hämtar inga PDF-filer.

### Korsreferenser i rättskedjan

Varje avgörande innehåller maskinläsbara hänvisningar som möjliggör navigering i hela rättskedjan:

- `lagrumLista[].sfsNummer` — kopplar till lagstiftningen (SFSR-kompatibelt format)
- `forarbeteLista` — kopplar till propositioner och utredningar (riksdagsformat)
- `europarattsligaAvgorandenLista` — lista med strängar som beskriver typen av europarättslig koppling: `"Europarättsligt avgörande"` och/eller `"Mänskliga rättigheter"`. Tom lista om ingen europarättslig koppling finns. Faktiska CELEX- och ECLI-nummer återfinns enbart i domtexten (HTML-fulltext) eller i `hanvisadePubliceringarLista` som fritext.
- `hanvisadePubliceringarLista` — fritext med hänvisningar till andra avgöranden och källor, inklusive EU-domstolens CELEX-beteckningar (t.ex. `C-30/19, EU:C:2021:269`) och Europadomstolens målnummer (t.ex. `Application no. 44306/98`)

## Krav

- Python 3.11 eller senare
- MCP-biblioteket `mcp` version 2.x (`mcp>=2.0,<3`)
- PostgreSQL (för `sok_i_domtext` med fulltext-sökning) eller SQLite (med enklare delsträngssökning)
- Internetanslutning mot `https://rattspraxis.etjanst.domstol.se`

## Installation

```bash
git clone https://github.com/MagnusKolsjo/mcp-for-rattspraxis.git
cd mcp-for-rattspraxis
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
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

## HTTP-läge

Med `MCP_TRANSPORT=http` körs servern som en långlivad process med Streamable HTTP på `http://<MCP_HOST>:<MCP_PORT>/mcp` (standard `127.0.0.1:8005`). Läget kräver `MCP_API_KEY`: utan nyckel avbryts uppstarten med exitkod 2. Klienten skickar nyckeln som `Authorization: Bearer <nyckel>`; ett anrop utan header ger 401 och ett med fel nyckel 403.

## Daglig synk

```bash
# Första körningen: fullsynk av hela korpusen
.venv/bin/python3 01_synka_publiceringar.py

# Därefter dagligen via launchd (macOS) eller cron (Linux)
.venv/bin/python3 01_synka_publiceringar.py --installera-schema
```

Skriptet hämtar `GET /publiceringar` sorterat på publiceringstid, 100 publiceringar per sida med två sekunders paus mellan sidorna (`RP_SYNK_PAUS_SEKUNDER`), och skriver varje sida till `avgorande_cache`. Läget sparas i `synk_status` efter varje sida: en avbruten körning fortsätter där den slutade, och de dagliga körningarna hämtar bara det som publicerats sedan förra gången. `--sedan ÅÅÅÅ-MM-DD` hämtar om från ett datum och `--alla` gör en ny fullsynk.

`--installera-schema` lägger in `synk_daglig.sh` i launchd eller cron enligt `SCHEMALAGGARE` och `CRON_SCHEMA` (standard 04:15). Wrappern loggar till `logs/synk-ÅÅÅÅ-MM-DD.log` och rensar loggar äldre än `LOGGRADER_BEHALL_DAGAR`.

**Storlek och tid för fullsynken.** Källan har drygt 17 000 publiceringar (17 366 i september 2026), vilket blir omkring 175 sidanrop och 300–400 MB att hämta. Med pausen mellan sidorna tar fullsynken ungefär 10–15 minuter. Databasen växer med i storleksordningen 0,5–1 GB i PostgreSQL, fulltextindexet inräknat. De dagliga körningarna hämtar en eller ett par sidor.

Publiceringar som ändras hos källan utan att få en ny publiceringstid fångas inte av den inkrementella synken. De uppdateras när cachens TTL gått ut och avgörandet hämtas på nytt, eller vid en ny fullsynk med `--alla`. Efter en fullsynk jämför skriptet antalet lokala avgöranden med källans totalsiffra och loggar om några saknas.

## Databasschema

Servern skapar automatiskt schemat `rattspraxis` i din PostgreSQL-databas (eller tabellerna direkt om SQLite används) vid uppstart, och lägger till nya kolumner och tabeller i befintliga databaser:

- `rattspraxis.avgorande_cache` — publiceringarna, med metadata, hela API-svaret och en sökbar text (`sokbar_text`, med GIN-index för svensk fulltext-sökning)
- `rattspraxis.pdf_cache` — extraherad PDF-text med GIN-index för svensk fulltext-sökning
- `rattspraxis.synk_status` — läget för synkskriptet

## Licens

AGPLv3 — se LICENSE.

Domstolsverkets rättspraxis är offentliga handlingar.
API: https://rattspraxis.etjanst.domstol.se/api/v1/
