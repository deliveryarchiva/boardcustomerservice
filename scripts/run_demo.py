"""Wrapper per avviare uvicorn in modalità DEMO (senza fetch Jira reale).

Imposta JIRA_API_TOKEN vuoto PRIMA di importare l'app, così `is_demo_mode()`
ritorna True e l'app usa `demo_data.py`. Usato dal preview integrato per non
attendere ~40s del primo fetch reale.
"""
import os
import sys
from pathlib import Path

# Working directory del worktree (parent della cartella scripts/)
WORKTREE = Path(__file__).resolve().parent.parent
os.chdir(WORKTREE)
sys.path.insert(0, str(WORKTREE))

os.environ["JIRA_API_TOKEN"] = ""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=8765,
        log_level="warning",
        reload=False,
    )
