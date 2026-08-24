-- Portfolio Control v3.5 encrypted cloud sync
-- Run this once in Supabase Dashboard -> SQL Editor.

create table if not exists public.portfolio_sync (
  user_id uuid primary key references auth.users(id) on delete cascade,
  revision bigint not null default 1,
  ciphertext text not null,
  nonce text not null,
  kdf_salt text not null,
  state_hash text not null,
  device_id text,
  app_version text,
  updated_at timestamptz not null default now()
);

alter table public.portfolio_sync enable row level security;

drop policy if exists "portfolio_sync_select_own" on public.portfolio_sync;
create policy "portfolio_sync_select_own"
on public.portfolio_sync for select
to authenticated
using (auth.uid() = user_id);

drop policy if exists "portfolio_sync_insert_own" on public.portfolio_sync;
create policy "portfolio_sync_insert_own"
on public.portfolio_sync for insert
to authenticated
with check (auth.uid() = user_id);

drop policy if exists "portfolio_sync_update_own" on public.portfolio_sync;
create policy "portfolio_sync_update_own"
on public.portfolio_sync for update
to authenticated
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "portfolio_sync_delete_own" on public.portfolio_sync;
create policy "portfolio_sync_delete_own"
on public.portfolio_sync for delete
to authenticated
using (auth.uid() = user_id);

create or replace function public.portfolio_sync_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists portfolio_sync_set_updated_at on public.portfolio_sync;
create trigger portfolio_sync_set_updated_at
before update on public.portfolio_sync
for each row execute function public.portfolio_sync_touch_updated_at();
