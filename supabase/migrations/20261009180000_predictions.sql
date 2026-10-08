-- Fase 3: predicciones, once recomendado e informes enviados.
-- Toda predicción se guarda antes del primer partido de la jornada para poder
-- evaluarla después (PLAN §1, principio 4). Nada de esto se actualiza.

create table prediction_runs (
    id                bigserial primary key,
    gameweek_id       integer     not null,
    model_version     text        not null,
    created_at        timestamptz not null default now(),
    first_kickoff_at  timestamptz,
    -- false si se generó con la jornada ya empezada (no sirve para evaluar)
    before_kickoff    boolean     not null,
    trigger           text        not null,
    inputs            jsonb       not null default '{}'
);
create index prediction_runs_gameweek on prediction_runs (gameweek_id, created_at);

create table predictions (
    run_id          bigint      not null references prediction_runs (id),
    player_id       integer     not null,
    gameweek_id     integer     not null,
    fixture_id      integer,
    team_id         integer,
    position        smallint,
    p_start         numeric(4, 3) not null,
    p_sub           numeric(4, 3) not null,
    p_play          numeric(4, 3) not null,
    exp_minutes     numeric(5, 1) not null,
    exp_points      numeric(5, 2) not null,
    p20             numeric(5, 1) not null,
    p80             numeric(5, 1) not null,
    components      jsonb       not null default '{}',
    primary key (run_id, player_id)
);
create index predictions_player on predictions (player_id, gameweek_id);

create table lineup_recommendations (
    run_id               bigint      primary key references prediction_runs (id),
    manager_id           integer     not null,
    gameweek_id          integer     not null,
    formation            text        not null,
    player_ids           integer[]   not null,
    expected_points      numeric(6, 2) not null,
    current_player_ids   integer[],
    current_expected     numeric(6, 2),
    fragile              jsonb       not null default '[]',
    created_at           timestamptz not null default now()
);

create table report_log (
    gameweek_id   integer     not null,
    slot          text        not null,
    run_id        bigint      references prediction_runs (id),
    sent_at       timestamptz not null default now(),
    delivered     boolean     not null,
    primary key (gameweek_id, slot)
);

alter table prediction_runs         enable row level security;
alter table predictions             enable row level security;
alter table lineup_recommendations  enable row level security;
alter table report_log              enable row level security;
