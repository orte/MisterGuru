-- Fase 6: agente conversacional.

-- Ajustes de disponibilidad sacados de noticias (ruedas de prensa, partes
-- médicos). Los usa el modelo de minutos mientras estén vigentes.
create table availability_overrides (
    id            bigserial primary key,
    created_at    timestamptz not null default now(),
    player_id     integer     not null,
    status        text        not null check (status in ('baja', 'duda', 'disponible')),
    p_play        numeric(4, 3),          -- probabilidad de jugar, si la noticia la permite
    valid_until   date        not null,
    source        text        not null,   -- medio o persona que lo dice
    source_date   date        not null,
    quote         text        not null,   -- frase de la noticia que lo justifica
    active        boolean     not null default true
);
create index availability_overrides_player on availability_overrides (player_id, valid_until);

-- Cada pregunta al agente, con las herramientas que usó y las cifras de la
-- respuesta que no salen de ninguna herramienta (deberían ser 0).
create table agent_log (
    id             bigserial primary key,
    created_at     timestamptz not null default now(),
    chat_id        text,
    question       text        not null,
    answer         text        not null,
    tool_calls     jsonb       not null default '[]',
    untraceable    jsonb       not null default '[]',
    run_id         bigint,
    usage          jsonb       not null default '{}'
);

alter table availability_overrides enable row level security;
alter table agent_log              enable row level security;
