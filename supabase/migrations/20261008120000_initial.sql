-- Fase 1: almacén inicial.
-- Las tablas *_snapshot son append-only: una fila por entidad y día
-- (`snapshot_date`), nunca se actualizan. Las dimensiones (players, managers,
-- teams, gameweeks, fixtures) sí se actualizan con el último valor conocido.
-- RLS activado sin políticas: la API pública de Supabase (anon/authenticated)
-- no ve nada; el job se conecta como propietario y no le afecta.

create table raw_responses (
    id            bigserial primary key,
    source        text        not null,
    route         text        not null,
    params        jsonb       not null default '{}',
    params_key    text        not null,
    run_date      date        not null,
    captured_at   timestamptz not null default now(),
    status_code   integer     not null,
    body_json     jsonb,
    body_text     text,
    unique (source, route, params_key, run_date)
);

create table job_runs (
    id           bigserial primary key,
    job          text        not null,
    run_date     date        not null,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    status       text        not null default 'running'
                 check (status in ('running', 'ok', 'partial', 'failed')),
    requests     integer     not null default 0,
    errors       jsonb       not null default '[]'
);
create index job_runs_job_date on job_runs (job, run_date);

create table teams (
    id          integer primary key,
    name        text        not null,
    updated_at  timestamptz not null default now()
);

create table players (
    mister_player_id  integer primary key,
    name              text        not null,
    short_name        text,
    slug              text,
    position          smallint    check (position between 1 and 4),
    team_id           integer,
    first_seen_at     timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create table managers (
    mister_manager_id  integer primary key,
    community_id       integer     not null,
    name               text        not null,
    slug               text,
    is_me              boolean     not null default false,
    first_seen_at      timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create table gameweeks (
    id              integer primary key,
    number          smallint    not null,
    season          text,
    type            text,
    status          text,
    first_match_at  timestamptz,
    last_match_at   timestamptz,
    updated_at      timestamptz not null default now()
);

create table fixtures (
    id               integer primary key,
    gameweek_id      integer     not null,
    home_team_id     integer     not null,
    away_team_id     integer     not null,
    kickoff_at       timestamptz,
    status           text,
    goals_home       smallint,
    goals_away       smallint,
    sofascore_id     bigint,
    updated_at       timestamptz not null default now()
);
create index fixtures_gameweek on fixtures (gameweek_id);

create table manager_snapshot (
    manager_id       integer     not null,
    snapshot_date    date        not null,
    captured_at      timestamptz not null default now(),
    season_rank      smallint,
    season_points    integer,
    team_value       bigint,
    team_value_prev  bigint,
    squad_size       smallint,
    -- Solo para el propio mánager: los saldos rivales están ocultos en la liga.
    balance          bigint,
    balance_future   bigint,
    max_bid          bigint,
    primary key (manager_id, snapshot_date)
);

create table squad_snapshot (
    player_id          integer     not null,
    snapshot_date      date        not null,
    captured_at        timestamptz not null default now(),
    manager_id         integer     not null,
    market_value       bigint,
    clause_value       bigint,
    clause_floor       bigint,
    clause_multiplier  numeric(4, 2),
    clause_shield      smallint,
    transfer_origin    text,
    on_sale_price      bigint,
    in_lineup          boolean     not null default false,
    primary key (player_id, snapshot_date)
);
create index squad_snapshot_manager on squad_snapshot (manager_id, snapshot_date);

create table market_snapshot (
    player_id         integer     not null,
    snapshot_date     date        not null,
    captured_at       timestamptz not null default now(),
    market_value      bigint,
    value_trend       smallint    check (value_trend in (-1, 0, 1)),
    points            integer,
    avg_points        numeric(5, 2),
    on_sale           boolean     not null,
    sale_price        bigint,
    -- null = lo vende el juego
    seller_manager_id integer,
    sale_ends_at      timestamptz,
    primary key (player_id, snapshot_date)
);

create table league_events (
    event_key          text primary key,
    community_id       integer     not null,
    feed_card_id       bigint      not null,
    category           text        not null,
    event_type         text,
    occurred_at        timestamptz not null,
    player_id          integer,
    from_manager_id    integer,
    to_manager_id      integer,
    price              bigint,
    payload            jsonb       not null,
    captured_at        timestamptz not null default now()
);
create index league_events_occurred on league_events (occurred_at);
create index league_events_player on league_events (player_id);

create table player_gameweek (
    player_id          integer     not null,
    gameweek_id        integer     not null,
    match_id           integer,
    team_id            integer,
    position           smallint,
    minutes            smallint,
    -- picas AS y estrellas Marca/MD (0-4); null = S.C.
    rating_as          smallint,
    rating_marca       smallint,
    rating_md          smallint,
    rating_sofascore   numeric(3, 1),
    points_as          smallint,
    points_marca       smallint,
    points_md          smallint,
    points_sofascore   smallint,
    points_mix         smallint,
    points_final       smallint,
    goals              smallint    not null default 0,
    penalty_goals      smallint    not null default 0,
    own_goals          smallint    not null default 0,
    assists            smallint    not null default 0,
    yellow_cards       smallint    not null default 0,
    double_yellow      smallint    not null default 0,
    red_cards          smallint    not null default 0,
    missed_penalties   smallint    not null default 0,
    saved_penalties    smallint    not null default 0,
    sub_in_minute      smallint,
    sub_out_minute     smallint,
    has_manual_rating  boolean     not null default false,
    market_value       bigint,
    graded_at          timestamptz,
    captured_at        timestamptz not null default now(),
    primary key (player_id, gameweek_id)
);
create index player_gameweek_gameweek on player_gameweek (gameweek_id);

alter table raw_responses    enable row level security;
alter table job_runs         enable row level security;
alter table teams            enable row level security;
alter table players          enable row level security;
alter table managers         enable row level security;
alter table gameweeks        enable row level security;
alter table fixtures         enable row level security;
alter table manager_snapshot enable row level security;
alter table squad_snapshot   enable row level security;
alter table market_snapshot  enable row level security;
alter table league_events    enable row level security;
alter table player_gameweek  enable row level security;
