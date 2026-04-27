"""Backend package — Customer Service Board.

Carica il file ``.env`` PRIMA di qualunque import che legga env var a
tempo di import (es. ``jira_client.py``, ``transform.py``, ``snapshot.py``).
Senza questo, ``load_dotenv()`` chiamato dentro ``main.py`` arriva troppo
tardi e l'app finisce in modalità DEMO anche con il token configurato.
"""
from dotenv import load_dotenv

load_dotenv()
