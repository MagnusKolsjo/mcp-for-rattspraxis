#!/usr/bin/env python3
"""
Klient för Domstolsverkets rättspraxis-API (PUH API v1).

API-beskrivning: https://rattspraxis.etjanst.domstol.se/openapi/puh-openapi.yaml

Används av både MCP-servern och synkskriptet, så att anropen, User-Agent
och felhanteringen är desamma oavsett vem som frågar källan.

Alla fel från källan kastas som KallaFel med ett svenskt meddelande som
kan visas för användaren som det är.
"""

import logging
import urllib.parse

import requests

log = logging.getLogger(__name__)

API_BAS = "https://rattspraxis.etjanst.domstol.se/api/v1"

# Projektidentifierande User-Agent. Skickas med på alla anrop mot källan.
HEADERS = {
    "User-Agent": "mcp-for-rattspraxis/1.0 (+https://github.com/MagnusKolsjo/mcp-for-rattspraxis)",
}


class KallaFel(RuntimeError):
    """Domstolsverkets API svarade inte, eller inte med det som väntades."""


class AvgorandeSaknas(KallaFel):
    """Källan känner inte till det efterfrågade id:t."""


def _anropa(metod: str, sokvag: str, timeout: float = 15, **kwargs) -> requests.Response:
    """Gör ett anrop mot API:et och översätter nät- och HTTP-fel till KallaFel."""
    url = f"{API_BAS}{sokvag}"
    headers = {**HEADERS, **kwargs.pop("headers", {})}
    try:
        svar = requests.request(metod, url, headers=headers, timeout=timeout, **kwargs)
    except requests.Timeout as e:
        log.warning("Timeout mot %s: %s", url, e)
        raise KallaFel(
            f"Domstolsverkets API svarade inte inom {timeout:g} sekunder. "
            "Försök igen om en stund."
        ) from e
    except requests.RequestException as e:
        log.warning("Anslutningsfel mot %s: %s", url, e)
        raise KallaFel(
            "Kunde inte nå Domstolsverkets API (rattspraxis.etjanst.domstol.se). "
            "Kontrollera nätverksanslutningen och försök igen."
        ) from e

    if svar.status_code == 404:
        raise AvgorandeSaknas("Domstolsverkets API hittade ingen post för anropet.")
    if svar.status_code == 429:
        raise KallaFel(
            "Domstolsverkets API begränsar antalet anrop just nu (HTTP 429). "
            "Vänta en stund och försök igen."
        )
    if svar.status_code >= 500:
        log.warning("Serverfel %s från %s", svar.status_code, url)
        raise KallaFel(
            f"Domstolsverkets API svarade med ett serverfel (HTTP {svar.status_code}). "
            "Försök igen senare."
        )
    if svar.status_code >= 400:
        log.warning("Klientfel %s från %s: %s", svar.status_code, url, svar.text[:300])
        raise KallaFel(
            f"Domstolsverkets API avvisade anropet (HTTP {svar.status_code})."
        )
    return svar


def _json(svar: requests.Response):
    """Tolkar svaret som JSON. Ett tomt svar blir None."""
    if not svar.content.strip():
        return None
    try:
        return svar.json()
    except ValueError as e:
        log.warning("Svaret från %s var inte JSON: %s", svar.url, svar.text[:300])
        raise KallaFel(
            "Domstolsverkets API svarade med något annat än JSON. "
            "Källan kan vara tillfälligt ur drift."
        ) from e


def sok(body: dict) -> dict:
    """POST /sok. Returnerar {total, publiceringLista}."""
    return _json(_anropa("POST", "/sok", json=body)) or {}


def sokforfiningar(body: dict) -> dict:
    """
    POST /sokforfiningar. Tar samma sökbegäran som /sok och returnerar
    antal träffar per värde, grupperat: sokordMap, rattsomradeMap,
    sfsnummerMap, avgorandetypMap, publiceringsformMap och domstolsidMap.
    """
    return _json(_anropa("POST", "/sokforfiningar", json=body)) or {}


def hamta_publicering(avgorande_id: str) -> dict:
    """
    GET /publiceringar/{id}. Returnerar hela publiceringen.

    API:et svarar 200 med tom kropp för ett okänt id, inte 404.
    """
    kodat = urllib.parse.quote(avgorande_id, safe="")
    data = _json(_anropa("GET", f"/publiceringar/{kodat}"))
    if not data:
        raise AvgorandeSaknas(
            f"Avgörandet med id '{avgorande_id}' finns inte hos Domstolsverket. "
            "Använd id-värdet från ett sökresultat (sok_rattpraxis) eller sök "
            "på beteckning med hamta_avgorande_pa_beteckning."
        )
    return data


def hamta_grupp(grupp_id: str) -> list[dict]:
    """
    GET /publiceringar/grupp/{id}. Returnerar alla publiceringar med samma
    gruppKorrelationsnummer — typiskt domen eller beslutet och det senare
    referatet av samma avgörande. Ett okänt grupp-id ger en tom lista.
    """
    kodat = urllib.parse.quote(grupp_id, safe="")
    data = _json(_anropa("GET", f"/publiceringar/grupp/{kodat}"))
    return data if isinstance(data, list) else []


def hamta_bilaga(fillagring_id: str) -> bytes:
    """GET /bilagor/{lagringId}. Returnerar PDF-filens innehåll."""
    kodat = urllib.parse.quote(fillagring_id, safe="")
    # API:et levererar bilagor som application/pdf och svarar 406 på
    # andra Accept-värden.
    try:
        svar = _anropa(
            "GET", f"/bilagor/{kodat}", timeout=30,
            headers={"Accept": "application/pdf"},
        )
    except AvgorandeSaknas as e:
        raise AvgorandeSaknas(
            f"Bilagan '{fillagring_id}' finns inte hos Domstolsverket. "
            "Hämta fillagring_id ur bilagor[] i svaret från hamta_avgorande."
        ) from e
    return svar.content
