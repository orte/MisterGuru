-- Fase 2: fuentes externas e identidades.
-- lineup_forecast y odds son append-only (una fila por captura). Las filas de
-- fuentes externas se guardan con el id externo, aunque aún no estén emparejadas:
-- el emparejado (player_xref) se puede rehacer sin volver a pedir nada.

create table team_xref (
    team_id        integer     not null,
    source         text        not null,
    external_id    text        not null,
    external_name  text        not null,
    method         text        not null,
    updated_at     timestamptz not null default now(),
    primary key (team_id, source),
    unique (source, external_id)
);

create table player_xref (
    player_id      integer     not null,
    source         text        not null,
    external_id    text        not null,
    external_name  text        not null,
    external_slug  text,
    confidence     numeric(5, 2) not null,
    method         text        not null,
    -- true = revisado a mano (o emparejado exacto); nunca lo pisa el automático
    verified       boolean     not null default false,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    primary key (player_id, source),
    unique (source, external_id)
);

-- Cola de revisión: ids externos sin emparejar o con emparejado dudoso.
create table identity_review (
    source           text        not null,
    external_id      text        not null,
    external_name    text        not null,
    external_slug    text,
    team_id          integer,
    position_hint    text,
    candidates       jsonb       not null default '[]',
    status           text        not null default 'pending'
                     check (status in ('pending', 'resolved', 'ignored')),
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    primary key (source, external_id)
);

create table lineup_forecast (
    source          text        not null,
    external_id     text        not null,
    gameweek_id     integer     not null,
    captured_at     timestamptz not null,
    fixture_id      integer,
    team_id         integer,
    player_name     text,
    -- probabilidad de ser titular (0-1); null si la fuente no la da
    probability     numeric(4, 3),
    role            text        check (role in ('titular', 'suplente')),
    injury_code     smallint,
    confirmed       boolean     not null default false,
    primary key (source, external_id, gameweek_id, captured_at)
);
create index lineup_forecast_gameweek on lineup_forecast (gameweek_id, captured_at);

create table match_stats (
    player_id         integer     not null,
    fixture_id        integer     not null,
    gameweek_id       integer     not null,
    team_id           integer,
    minutes           smallint,
    xg                numeric(5, 3) not null default 0,
    xa                numeric(5, 3) not null default 0,
    shots             smallint    not null default 0,
    shots_on_target   smallint    not null default 0,
    key_passes        smallint    not null default 0,
    touches           smallint,
    source            text        not null default 'mister_sofascore',
    captured_at       timestamptz not null default now(),
    primary key (player_id, fixture_id)
);
create index match_stats_gameweek on match_stats (gameweek_id);

-- Cuotas: consenso entre casas (probabilidad sin margen) por evento y mercado.
create table odds (
    event_id        text        not null,
    captured_at     timestamptz not null,
    fixture_id      integer,
    home_team_id    integer,
    away_team_id    integer,
    commence_at     timestamptz,
    market          text        not null,
    outcome         text        not null,
    point           numeric(4, 2) not null default 0,
    price_avg       numeric(7, 3) not null,
    prob_fair       numeric(5, 4) not null,
    bookmakers      smallint    not null,
    primary key (event_id, captured_at, market, outcome, point)
);
create index odds_fixture on odds (fixture_id, captured_at);

alter table team_xref        enable row level security;
alter table player_xref      enable row level security;
alter table identity_review  enable row level security;
alter table lineup_forecast  enable row level security;
alter table match_stats      enable row level security;
alter table odds             enable row level security;
