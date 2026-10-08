"""Modelos Pydantic de las respuestas de Mister usadas hasta ahora.

Solo se tipan los campos que el código consume; el resto se ignora y queda en la
respuesta cruda. Ver docs/mister-endpoints.md para la forma completa.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Balance(_Model):
    current: int
    future: int
    max_debt: int = Field(alias="maxDebt")


class CommunityRef(_Model):
    id: int


class FeedContext(_Model):
    communities: list[CommunityRef] = []
    community: CommunityRef | None = None


class FeedCfg(_Model):
    context: FeedContext = FeedContext()
    market_date: str | None = None


class FeedPage(_Model):
    data: list[dict[str, Any]] = []
    cfg: FeedCfg = FeedCfg()


class Prizes(_Model):
    points: int = 0
    goals: int = 0
    positions: dict[int, int] = {}


class LeagueSettings(_Model):
    """`data.community` de /ajax/sw/admin: configuración de la liga."""

    id: int
    name: str
    provider: str
    type: str | None = None
    mode: str | None = None
    max_users: int | None = None
    starting_balance: int | None = Field(default=None, alias="startingBalance")
    starting_players: int | None = Field(default=None, alias="startingPlayers")
    team_limit: int | None = None
    sale_limit: int | None = None
    market_speed: int | None = None
    market_stay: int | None = None
    clauses: int | None = None
    clauses_block: int | None = None
    clauses_gameweek: int | None = None
    clauses_daily: int | None = None
    max_inbound_clauses: int | None = None
    transfer_wait: int | None = None
    loans: int | None = None
    salaries: int | None = None
    max_debt: int | None = None
    show_balances: int | None = None
    is_captain_enabled: int | None = None
    live_changes: int | None = None
    prizes: Prizes = Prizes()
