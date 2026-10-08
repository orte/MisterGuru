-- Fase 4: valores de mercado diarios y recomendaciones.

-- Valor de mercado por jugador y día. Se rellena con la serie de un año de
-- /ajax/sw/players (backfill) y cada día con los snapshots de plantillas y mercado.
create table player_value_daily (
    player_id    integer     not null,
    value_date   date        not null,
    value        bigint      not null,
    source       text        not null,
    captured_at  timestamptz not null default now(),
    primary key (player_id, value_date)
);
create index player_value_daily_date on player_value_daily (value_date);

-- Toda recomendación emitida, con los números que la justifican y, más
-- adelante, qué pasó (auditoría de la Fase 5).
create table recommendations (
    id            bigserial primary key,
    created_at    timestamptz not null default now(),
    report_date   date        not null,
    kind          text        not null
                  check (kind in ('fichaje', 'puja', 'clausula_ofensiva',
                                  'clausula_defensiva', 'venta')),
    player_id     integer     not null,
    manager_id    integer,
    rank          smallint,
    score         numeric(10, 2),
    numbers       jsonb       not null,
    message       text        not null,
    outcome       jsonb,
    outcome_at    timestamptz
);
create index recommendations_date on recommendations (report_date, kind);
create index recommendations_player on recommendations (player_id);

alter table player_value_daily enable row level security;
alter table recommendations    enable row level security;
