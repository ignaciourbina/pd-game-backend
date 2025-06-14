
"""
game_db.py

A minimal SQLite-backed helper for two‍player games (e.g. Rock‍Paper‍Scissors).

Key improvements over the original sketch
-----------------------------------------
* Context‍manager (`get_conn`) guarantees connections are always closed
  even when an exception occurs.
* PRAGMA `foreign_keys = ON` so `moves.session_id` respects the parent `sessions` row.
* `moves` has a `(session_id, player_id)` UNIQUE constraint so the same
  player cannot submit two moves for one game.
* `join_session()` now also returns a unique `player_id`, so callers have
  an opaque token they can pass back to `save_move()`.
* Extra safety checks raise clear `ValueError`s for illegal states
  (unknown session, session full, duplicate moves, etc.).
* Typed function signatures for easier integration & autocomplete.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------- #
# Paths & helpers
# --------------------------------------------------------------------------- #
_DB_DIR: Path = Path("/data")
_DB_FILE: Path = _DB_DIR / "game.db"


@contextmanager
def _get_conn() -> sqlite3.Connection:
    """Yield a SQLite connection with `foreign_keys` enabled."""
    conn = sqlite3.connect(_DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Schema initialisation
# --------------------------------------------------------------------------- #

def init_db() -> None:
    """Ensure the on‍disk database and tables exist."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)

    with _get_conn() as conn:
        cur = conn.cursor()
        # Sessions (a game waiting for 2 players)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id           TEXT PRIMARY KEY,
                player_count INTEGER NOT NULL DEFAULT 0
                                 CHECK(player_count BETWEEN 0 AND 2)
            )
            """
        )

        # Individual moves made inside a session
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS moves (
                session_id TEXT NOT NULL,
                player_id  TEXT NOT NULL,
                choice     TEXT NOT NULL,
                UNIQUE (session_id, player_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )

        # Helpful index for frequent look‍ups
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_moves_session ON moves(session_id)"
        )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def join_session() -> Tuple[str, str]:
    """
    Find (or create) a session that has fewer than 2 players and join it.

    Returns
    -------
    (session_id, player_id)
        Both are opaque UUID4 strings generated server‍side.
    """
    with _get_conn() as conn:
        cur = conn.cursor()

        # Attempt to join an existing session that has only 1 player so far
        cur.execute(
            """
            SELECT id
            FROM   sessions
            WHERE  player_count = 1
            LIMIT  1
            """
        )
        row = cur.fetchone()

        player_id: str = str(uuid.uuid4())

        if row:
            (session_id,) = row
            cur.execute(
                "UPDATE sessions SET player_count = player_count + 1 WHERE id = ?",
                (session_id,),
            )
        else:
            # Either no open sessions or every open session already full → create one
            session_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO sessions (id, player_count) VALUES (?, 1)",
                (session_id,),
            )

        return session_id, player_id


def get_state(session_id: str) -> Dict[str, int | str]:
    """
    Return a high‍level view of the session: number of players, number of moves, phase.
    """
    with _get_conn() as conn:
        cur = conn.cursor()

        cur.execute("SELECT player_count FROM sessions WHERE id = ?", (session_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Unknown session_id: {session_id}")

        players: int = row[0]

        cur.execute("SELECT COUNT(*) FROM moves WHERE session_id = ?", (session_id,))
        moves: int = cur.fetchone()[0]

    # Decide phase
    if players < 2:
        phase = "waiting_for_opponent"
    elif moves < players:
        phase = "waiting_for_moves"
    else:
        phase = "finished"

    return {"players": players, "moves": moves, "phase": phase}


def save_move(session_id: str, player_id: str, choice: str) -> None:
    """
    Persist a single move for a given player inside a session.

    Raises
    ------
    ValueError
        If the session does not exist, or the session is already finished,
        or the player already submitted a move.
    """
    with _get_conn() as conn:
        cur = conn.cursor()

        # Ensure session exists & not finished
        state = get_state(session_id)
        if state["phase"] == "waiting_for_opponent":
            raise ValueError("Cannot submit moves until two players have joined.")
        if state["phase"] == "finished":
            raise ValueError("Session already finished; no further moves accepted.")

        # Attempt insert
        try:
            cur.execute(
                """
                INSERT INTO moves (session_id, player_id, choice)
                VALUES (?, ?, ?)
                """,
                (session_id, player_id, choice),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Duplicate move or invalid session.") from exc


def get_results(session_id: str) -> List[Dict[str, str]]:
    """
    Fetch the list of moves for a session (order is insertion order).

    Returns
    -------
    [{"player": ..., "choice": ...}, ...]
    """
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT player_id, choice
            FROM   moves
            WHERE  session_id = ?
            ORDER BY ROWID
            """,
            (session_id,),
        )
        return [{"player": p, "choice": c} for p, c in cur.fetchall()]
