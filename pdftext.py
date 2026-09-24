#!/usr/bin/env python3
"""
Textextraktion ur Domstolsverkets PDF-bilagor.

Används av både MCP-servern (hamta_pdf) och synkskriptet, så att texten som
lagras i pdf_cache blir densamma oavsett vem som hämtade PDF:en.
"""

import contextlib
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_LOGS_DIR = Path(__file__).parent.resolve() / "logs"

# PyMuPDF är inte trådsäkert, och MCP-verktygen körs på arbetstrådar. Låset
# serialiserar extraktionen. Det skyddar också _tysta_fd1, som flyttar
# processens gemensamma filhandtag: två samtidiga omdirigeringar skulle
# återställa handtagen i fel ordning.
_pdf_las = threading.Lock()


class PdfFel(RuntimeError):
    """PDF:en gick inte att läsa, eller pymupdf4llm saknas."""


@contextlib.contextmanager
def _tysta_fd1():
    """
    Redirigerar FD 1 och FD 2 till loggfil under anrop som skriver direkt
    till filhandtagen (pymupdf4llm och dess C-bindningar).

    MCP-protokollet skyddas redan av SDK:ns stdio-transport, som läser och
    skriver på egna kopior av handtagen. Omdirigeringen håller i stället
    extraktionens utskrifter borta från stderr, som klienten loggar.
    Anropas bara med _pdf_las taget.
    """
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logg = _LOGS_DIR / "subprocess.log"
    spara_ut = os.dup(1)
    spara_fel = os.dup(2)
    fd = os.open(str(logg), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        yield
    finally:
        os.dup2(spara_ut, 1)
        os.dup2(spara_fel, 2)
        os.close(spara_ut)
        os.close(spara_fel)
        os.close(fd)


def extrahera_text(pdf_bytes: bytes) -> str:
    """Extraherar PDF:ens text som markdown. Kastar PdfFel om det inte går."""
    # pymupdf4llm importeras först här — det krävs bara när en PDF läses.
    try:
        import fitz
        import pymupdf4llm
    except ImportError as e:
        raise PdfFel(
            "pymupdf4llm är inte installerat. Kör: pip install pymupdf4llm "
            "och starta om servern."
        ) from e

    try:
        with _pdf_las:
            dok = fitz.open(stream=pdf_bytes, filetype="pdf")
            try:
                with _tysta_fd1():
                    return pymupdf4llm.to_markdown(dok)
            finally:
                dok.close()
    except Exception as e:
        raise PdfFel(
            f"Texten i PDF:en kunde inte extraheras ({e}). Filen kan vara "
            "skadad eller bara innehålla inskannade bilder."
        ) from e
