-- Fase 5: resultados de evaluación (backtest y evaluación semanal de lo emitido).

create table evaluations (
    id            bigserial primary key,
    created_at    timestamptz not null default now(),
    kind          text        not null check (kind in ('backtest', 'jornada', 'mercado')),
    gameweek_id   integer,
    model         text,
    metrics       jsonb       not null,
    summary       text        not null
);
create index evaluations_kind on evaluations (kind, created_at);

alter table evaluations enable row level security;
