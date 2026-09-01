-- CashClose AI truth layer for PostgreSQL 15+ / Supabase.
-- Financial mutations are expected to pass through validated application tools.

begin;

create extension if not exists pgcrypto;

create type public.batch_status as enum (
  'UPLOADED',
  'VALIDATING',
  'NORMALIZING',
  'RECONCILING',
  'VERIFYING',
  'FORECASTING',
  'EVALUATING',
  'COMPLETED',
  'VALIDATION_FAILED',
  'PROCESSING_FAILED',
  'CANCELLED'
);

create type public.record_status as enum (
  'UNPROCESSED',
  'CANDIDATES_FOUND',
  'PROPOSED',
  'AUTO_RECONCILED',
  'NEEDS_REVIEW',
  'UNRESOLVED',
  'REJECTED'
);

create type public.money_direction as enum ('CREDIT', 'DEBIT');
create type public.cash_flow_direction as enum ('INFLOW', 'OUTFLOW');
create type public.invoice_status as enum ('OPEN', 'PARTIALLY_PAID', 'PAID', 'VOID', 'DUPLICATE');
create type public.proposal_status as enum ('PROPOSED', 'VERIFIED', 'REJECTED', 'COMMITTED');
create type public.reconciliation_decision_type as enum (
  'AUTO_RECONCILED',
  'NEEDS_REVIEW',
  'UNRESOLVED',
  'REJECTED'
);
create type public.exception_status as enum ('OPEN', 'IN_REVIEW', 'RESOLVED', 'DISMISSED');
create type public.run_status as enum ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED');
create type public.event_status as enum ('STARTED', 'SUCCEEDED', 'FAILED', 'SKIPPED');
create type public.forecast_line as enum ('CONFIRMED', 'EXPECTED', 'RISK_ADJUSTED');
create type public.evaluation_status as enum ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED');

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create or replace function public.enforce_batch_status_transition()
returns trigger
language plpgsql
as $$
begin
  if new.status = old.status then
    return new;
  end if;

  if not (
    (old.status = 'UPLOADED' and new.status in ('VALIDATING', 'CANCELLED')) or
    (old.status = 'VALIDATING' and new.status in ('NORMALIZING', 'VALIDATION_FAILED', 'PROCESSING_FAILED', 'CANCELLED')) or
    (old.status = 'NORMALIZING' and new.status in ('RECONCILING', 'PROCESSING_FAILED', 'CANCELLED')) or
    (old.status = 'RECONCILING' and new.status in ('VERIFYING', 'PROCESSING_FAILED', 'CANCELLED')) or
    (old.status = 'VERIFYING' and new.status in ('FORECASTING', 'PROCESSING_FAILED', 'CANCELLED')) or
    (old.status = 'FORECASTING' and new.status in ('EVALUATING', 'PROCESSING_FAILED', 'CANCELLED')) or
    (old.status = 'EVALUATING' and new.status in ('COMPLETED', 'PROCESSING_FAILED', 'CANCELLED'))
  ) then
    raise exception 'invalid batch status transition: % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create table public.organizations (
  id uuid primary key default gen_random_uuid(),
  external_id text not null unique,
  name text not null,
  base_currency char(3) not null default 'INR',
  accounting_timezone text not null default 'Asia/Kolkata',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint organizations_base_currency_check check (base_currency ~ '^[A-Z]{3}$')
);

create table public.organization_members (
  organization_id uuid not null references public.organizations(id) on delete cascade,
  user_id uuid not null,
  role text not null default 'MEMBER',
  created_at timestamptz not null default now(),
  primary key (organization_id, user_id),
  constraint organization_members_role_check check (role in ('OWNER', 'ADMIN', 'MEMBER', 'REVIEWER'))
);

create table public.batches (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete restrict,
  external_id text not null,
  name text not null,
  as_of_date date not null,
  status public.batch_status not null default 'UPLOADED',
  accounting_timezone text not null,
  policy_version text not null,
  source_seed bigint,
  started_at timestamptz,
  completed_at timestamptz,
  failure_code text,
  failure_detail text,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, external_id),
  constraint batches_completion_check check (
    (status = 'COMPLETED' and completed_at is not null)
    or status <> 'COMPLETED'
  )
);

create trigger batches_enforce_status_transition
before update of status on public.batches
for each row execute function public.enforce_batch_status_transition();

create table public.uploaded_files (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  file_kind text not null,
  storage_bucket text not null,
  storage_path text not null,
  original_filename text not null,
  content_type text not null default 'text/csv',
  byte_size bigint not null,
  row_count integer,
  sha256 char(64) not null,
  schema_version text not null default '1.0',
  validation_status text not null default 'PENDING',
  validation_errors jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  unique (batch_id, file_kind),
  unique (storage_bucket, storage_path),
  constraint uploaded_files_kind_check check (
    file_kind in (
      'BANK_TRANSACTIONS', 'INVOICES', 'LEDGER_ENTRIES', 'REMITTANCES',
      'CUSTOMERS', 'CUSTOMER_ALIASES', 'RECURRING_CASH_FLOWS'
    )
  ),
  constraint uploaded_files_size_check check (byte_size >= 0),
  constraint uploaded_files_rows_check check (row_count is null or row_count >= 0),
  constraint uploaded_files_hash_check check (sha256 ~ '^[0-9a-f]{64}$'),
  constraint uploaded_files_validation_check check (
    validation_status in ('PENDING', 'VALID', 'INVALID')
  )
);

create table public.customers (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete restrict,
  external_id text not null,
  canonical_name text not null,
  normalized_name text,
  default_currency char(3) not null,
  mean_payment_delay_days numeric(8, 3) not null default 0,
  payment_delay_stddev_days numeric(8, 3) not null default 0,
  payment_probability numeric(7, 6) not null default 1,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, external_id),
  constraint customers_currency_check check (default_currency ~ '^[A-Z]{3}$'),
  constraint customers_delay_check check (
    mean_payment_delay_days >= 0 and payment_delay_stddev_days >= 0
  ),
  constraint customers_probability_check check (payment_probability between 0 and 1)
);

create table public.customer_aliases (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  customer_id uuid not null references public.customers(id) on delete cascade,
  alias text not null,
  normalized_alias text not null,
  approved boolean not null default false,
  approved_by uuid,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  unique (organization_id, normalized_alias),
  constraint customer_aliases_approval_check check (
    (approved = false) or (approved_at is not null)
  )
);

create table public.bank_transactions (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  transaction_id text not null,
  customer_id uuid references public.customers(id) on delete set null,
  source_file_id uuid references public.uploaded_files(id) on delete set null,
  source_row integer,
  transaction_date date not null,
  booking_date date not null,
  value_date date,
  amount numeric(20, 2) not null,
  currency char(3) not null,
  direction public.money_direction not null,
  counterparty text not null,
  normalized_counterparty text,
  reference text not null default '',
  normalized_reference text,
  payment_reference text,
  status public.record_status not null default 'UNPROCESSED',
  raw_record jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (batch_id, transaction_id),
  constraint bank_transactions_amount_check check (amount > 0),
  constraint bank_transactions_currency_check check (currency ~ '^[A-Z]{3}$'),
  constraint bank_transactions_source_row_check check (source_row is null or source_row > 0)
);

create table public.invoices (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  invoice_id text not null,
  customer_id uuid not null references public.customers(id) on delete restrict,
  source_file_id uuid references public.uploaded_files(id) on delete set null,
  source_row integer,
  invoice_number text not null,
  issue_date date not null,
  due_date date not null,
  original_amount numeric(20, 2) not null,
  open_amount numeric(20, 2) not null,
  currency char(3) not null,
  status public.invoice_status not null default 'OPEN',
  payment_reference text,
  normalized_reference text,
  raw_record jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (batch_id, invoice_id),
  constraint invoices_date_check check (due_date >= issue_date),
  constraint invoices_amount_check check (
    original_amount > 0 and open_amount >= 0 and open_amount <= original_amount
  ),
  constraint invoices_currency_check check (currency ~ '^[A-Z]{3}$'),
  constraint invoices_source_row_check check (source_row is null or source_row > 0)
);

create index invoices_external_identity_idx
  on public.invoices (batch_id, customer_id, invoice_number);
create index invoices_open_search_idx
  on public.invoices (batch_id, currency, due_date)
  where status in ('OPEN', 'PARTIALLY_PAID');

create table public.ledger_entries (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  entry_id text not null,
  source_file_id uuid references public.uploaded_files(id) on delete set null,
  source_row integer,
  transaction_id uuid references public.bank_transactions(id) on delete set null,
  entry_date date not null,
  account_code text not null,
  direction public.money_direction not null,
  amount numeric(20, 2) not null,
  currency char(3) not null,
  reference text not null default '',
  normalized_reference text,
  counterparty text not null default '',
  status public.record_status not null default 'UNPROCESSED',
  raw_record jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (batch_id, entry_id),
  constraint ledger_entries_amount_check check (amount > 0),
  constraint ledger_entries_currency_check check (currency ~ '^[A-Z]{3}$'),
  constraint ledger_entries_source_row_check check (source_row is null or source_row > 0)
);

create table public.remittances (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  remittance_id text not null,
  transaction_id uuid references public.bank_transactions(id) on delete set null,
  source_file_id uuid references public.uploaded_files(id) on delete set null,
  source_row integer,
  received_at timestamptz not null,
  sender text not null default '',
  raw_text text not null,
  parsed_counterparty text,
  parsed_invoice_references jsonb not null default '[]'::jsonb,
  parsed_payment_type text,
  parsed_deduction_hint text,
  parser_model text,
  parser_schema_version text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (batch_id, remittance_id),
  constraint remittances_source_row_check check (source_row is null or source_row > 0)
);

create table public.recurring_cash_flows (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  external_id text not null,
  flow_type text not null,
  direction public.cash_flow_direction not null,
  amount numeric(20, 2) not null,
  currency char(3) not null,
  frequency text not null,
  day_rule integer,
  next_due_date date not null,
  counterparty text not null,
  committed boolean not null default false,
  active boolean not null default true,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, external_id),
  constraint recurring_cash_flows_amount_check check (amount > 0),
  constraint recurring_cash_flows_currency_check check (currency ~ '^[A-Z]{3}$'),
  constraint recurring_cash_flows_frequency_check check (
    frequency in ('DAILY', 'WEEKLY', 'MONTHLY', 'QUARTERLY', 'ANNUAL')
  ),
  constraint recurring_cash_flows_day_rule_check check (
    day_rule is null or day_rule between 1 and 31
  )
);

create table public.match_candidates (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  transaction_id uuid not null references public.bank_transactions(id) on delete cascade,
  invoice_id uuid references public.invoices(id) on delete cascade,
  ledger_entry_id uuid references public.ledger_entries(id) on delete cascade,
  rank integer not null,
  score numeric(7, 6) not null,
  feature_scores jsonb not null,
  risk_flags jsonb not null default '[]'::jsonb,
  policy_version text not null,
  created_at timestamptz not null default now(),
  constraint match_candidates_one_target_check check (
    num_nonnulls(invoice_id, ledger_entry_id) = 1
  ),
  constraint match_candidates_rank_check check (rank > 0),
  constraint match_candidates_score_check check (score between 0 and 1)
);

create unique index match_candidates_invoice_unique
  on public.match_candidates (transaction_id, invoice_id)
  where invoice_id is not null;
create unique index match_candidates_ledger_unique
  on public.match_candidates (transaction_id, ledger_entry_id)
  where ledger_entry_id is not null;
create index match_candidates_rank_idx
  on public.match_candidates (transaction_id, rank);

create table public.match_proposals (
  id uuid primary key default gen_random_uuid(),
  proposal_id text not null,
  batch_id uuid not null references public.batches(id) on delete cascade,
  transaction_id uuid not null references public.bank_transactions(id) on delete cascade,
  status public.proposal_status not null default 'PROPOSED',
  confidence numeric(7, 6) not null,
  transaction_amount numeric(20, 2) not null,
  total_allocated numeric(20, 2) not null,
  adjustment_total numeric(20, 2) not null default 0,
  currency char(3) not null,
  risk_flags jsonb not null default '[]'::jsonb,
  policy_version text not null,
  proposed_by text not null,
  verified_by text,
  verified_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (batch_id, proposal_id),
  constraint match_proposals_confidence_check check (confidence between 0 and 1),
  constraint match_proposals_amount_check check (
    transaction_amount > 0 and total_allocated >= 0
  ),
  constraint match_proposals_currency_check check (currency ~ '^[A-Z]{3}$'),
  constraint match_proposals_verification_check check (
    status not in ('VERIFIED', 'COMMITTED')
    or (verified_by is not null and verified_at is not null)
  )
);

create table public.match_allocations (
  id uuid primary key default gen_random_uuid(),
  proposal_id uuid not null references public.match_proposals(id) on delete cascade,
  invoice_id uuid not null references public.invoices(id) on delete restrict,
  amount numeric(20, 2) not null,
  currency char(3) not null,
  created_at timestamptz not null default now(),
  unique (proposal_id, invoice_id),
  constraint match_allocations_amount_check check (amount > 0),
  constraint match_allocations_currency_check check (currency ~ '^[A-Z]{3}$')
);

create table public.match_evidence (
  id uuid primary key default gen_random_uuid(),
  proposal_id uuid not null references public.match_proposals(id) on delete cascade,
  evidence_type text not null,
  source_table text not null,
  source_record_id uuid,
  evidence_reference text not null,
  feature_value numeric(9, 6),
  summary text not null,
  created_at timestamptz not null default now(),
  constraint match_evidence_reference_check check (length(evidence_reference) > 0)
);

create table public.reconciliation_decisions (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  transaction_id uuid not null references public.bank_transactions(id) on delete restrict,
  decision public.reconciliation_decision_type not null,
  confidence numeric(7, 6) not null,
  decision_source text not null,
  model_name text,
  policy_version text not null,
  proposal_id uuid references public.match_proposals(id) on delete restrict,
  committed_at timestamptz,
  idempotency_key text,
  created_at timestamptz not null default now(),
  constraint reconciliation_decisions_confidence_check check (confidence between 0 and 1),
  constraint reconciliation_decisions_commit_check check (
    (committed_at is null and idempotency_key is null)
    or (committed_at is not null and idempotency_key is not null and proposal_id is not null)
  )
);

create unique index reconciliation_decisions_idempotency_unique
  on public.reconciliation_decisions (idempotency_key)
  where idempotency_key is not null;
create unique index reconciliation_decisions_one_commit_per_transaction
  on public.reconciliation_decisions (transaction_id)
  where committed_at is not null;

create table public.exceptions (
  id uuid primary key default gen_random_uuid(),
  exception_id text not null,
  batch_id uuid not null references public.batches(id) on delete cascade,
  record_type text not null,
  record_id uuid not null,
  reason_code text not null,
  status public.exception_status not null default 'OPEN',
  severity text not null default 'ERROR',
  evidence_references jsonb not null default '[]'::jsonb,
  next_action text not null,
  assigned_to uuid,
  resolution text,
  resolved_by uuid,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (batch_id, exception_id),
  constraint exceptions_record_type_check check (
    record_type in ('BANK_TRANSACTION', 'INVOICE', 'LEDGER_ENTRY', 'REMITTANCE', 'BATCH')
  ),
  constraint exceptions_severity_check check (severity in ('WARNING', 'ERROR', 'CRITICAL')),
  constraint exceptions_resolution_check check (
    status <> 'RESOLVED'
    or (resolution is not null and resolved_at is not null)
  )
);

create index exceptions_inbox_idx
  on public.exceptions (batch_id, status, reason_code, created_at);

create table public.forecast_runs (
  id uuid primary key default gen_random_uuid(),
  forecast_id text not null,
  batch_id uuid not null references public.batches(id) on delete cascade,
  as_of_date date not null,
  horizon_days integer not null,
  opening_cash numeric(20, 2) not null,
  currency char(3) not null,
  simulation_count integer not null default 0,
  random_seed bigint,
  status public.run_status not null default 'PENDING',
  policy_version text not null,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  unique (batch_id, forecast_id),
  constraint forecast_runs_horizon_check check (horizon_days between 1 and 366),
  constraint forecast_runs_simulation_check check (simulation_count between 0 and 100000),
  constraint forecast_runs_currency_check check (currency ~ '^[A-Z]{3}$')
);

create table public.forecast_daily_positions (
  id uuid primary key default gen_random_uuid(),
  forecast_run_id uuid not null references public.forecast_runs(id) on delete cascade,
  position_date date not null,
  line public.forecast_line not null,
  opening_cash numeric(20, 2) not null,
  inflows numeric(20, 2) not null,
  outflows numeric(20, 2) not null,
  closing_cash numeric(20, 2) not null,
  p10_cash numeric(20, 2),
  p50_cash numeric(20, 2),
  p90_cash numeric(20, 2),
  currency char(3) not null,
  inputs_digest char(64) not null,
  created_at timestamptz not null default now(),
  unique (forecast_run_id, position_date, line),
  constraint forecast_daily_flows_check check (inflows >= 0 and outflows >= 0),
  constraint forecast_daily_currency_check check (currency ~ '^[A-Z]{3}$'),
  constraint forecast_daily_percentiles_check check (
    (p10_cash is null and p50_cash is null and p90_cash is null)
    or (p10_cash is not null and p50_cash is not null and p90_cash is not null
        and p10_cash <= p50_cash and p50_cash <= p90_cash)
  ),
  constraint forecast_daily_digest_check check (inputs_digest ~ '^[0-9a-f]{64}$')
);

create table public.forecast_scenarios (
  id uuid primary key default gen_random_uuid(),
  forecast_run_id uuid not null references public.forecast_runs(id) on delete cascade,
  scenario_id text not null,
  name text not null,
  action_type text not null,
  validated_parameters jsonb not null,
  result_summary jsonb not null,
  created_by uuid,
  created_at timestamptz not null default now(),
  unique (forecast_run_id, scenario_id)
);

create table public.agent_runs (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  agent_name text not null,
  model_name text not null,
  status public.run_status not null default 'PENDING',
  max_tool_calls integer not null,
  max_retries_per_strategy integer not null default 1,
  parent_run_id uuid references public.agent_runs(id) on delete set null,
  response_id text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  constraint agent_runs_tool_limit_check check (max_tool_calls between 1 and 10000),
  constraint agent_runs_retry_limit_check check (max_retries_per_strategy between 0 and 3)
);

create table public.agent_events (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  agent_run_id uuid not null references public.agent_runs(id) on delete cascade,
  agent_name text not null,
  event_type text not null,
  input_reference text,
  tool_name text,
  tool_result_reference text,
  occurred_at timestamptz not null default now(),
  latency_ms integer,
  status public.event_status not null,
  display_message text not null,
  sequence_number bigint not null,
  unique (agent_run_id, sequence_number),
  constraint agent_events_latency_check check (latency_ms is null or latency_ms >= 0)
);

create index agent_events_stream_idx
  on public.agent_events (batch_id, occurred_at, sequence_number);

create table public.tool_calls (
  id uuid primary key default gen_random_uuid(),
  agent_run_id uuid not null references public.agent_runs(id) on delete cascade,
  agent_event_id uuid references public.agent_events(id) on delete set null,
  tool_call_id text not null,
  tool_name text not null,
  validated_arguments jsonb not null,
  result_reference text,
  status public.event_status not null,
  started_at timestamptz not null,
  completed_at timestamptz,
  latency_ms integer,
  error_code text,
  created_at timestamptz not null default now(),
  unique (agent_run_id, tool_call_id),
  constraint tool_calls_latency_check check (latency_ms is null or latency_ms >= 0)
);

create table public.audit_events (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete restrict,
  batch_id uuid references public.batches(id) on delete restrict,
  actor_type text not null,
  actor_id text not null,
  action text not null,
  entity_type text not null,
  entity_id text not null,
  evidence_references jsonb not null default '[]'::jsonb,
  policy_version text,
  occurred_at timestamptz not null default now(),
  event_hash char(64) not null,
  previous_event_hash char(64),
  constraint audit_events_actor_type_check check (actor_type in ('USER', 'AGENT', 'SYSTEM', 'TOOL')),
  constraint audit_events_hash_check check (event_hash ~ '^[0-9a-f]{64}$'),
  constraint audit_events_previous_hash_check check (
    previous_event_hash is null or previous_event_hash ~ '^[0-9a-f]{64}$'
  )
);

create index audit_events_chain_idx
  on public.audit_events (organization_id, occurred_at, id);

create or replace function public.prevent_audit_event_mutation()
returns trigger
language plpgsql
as $$
begin
  raise exception 'audit_events are append-only' using errcode = 'insufficient_privilege';
end;
$$;

create trigger audit_events_are_immutable
before update or delete on public.audit_events
for each row execute function public.prevent_audit_event_mutation();

create table public.evaluation_runs (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  evaluator_version text not null,
  ground_truth_digest char(64) not null,
  status public.evaluation_status not null default 'PENDING',
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  constraint evaluation_runs_digest_check check (ground_truth_digest ~ '^[0-9a-f]{64}$')
);

create table public.evaluation_results (
  id uuid primary key default gen_random_uuid(),
  evaluation_run_id uuid not null references public.evaluation_runs(id) on delete cascade,
  metric_name text not null,
  metric_value numeric(28, 10) not null,
  numerator numeric(28, 10),
  denominator numeric(28, 10),
  currency char(3),
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (evaluation_run_id, metric_name),
  constraint evaluation_results_currency_check check (
    currency is null or currency ~ '^[A-Z]{3}$'
  )
);

-- Ground truth is deliberately outside public. The agent database role must not
-- receive USAGE on this schema. Only an independent evaluator credential should.
create schema if not exists private_evaluation;
revoke all on schema private_evaluation from public;

create table private_evaluation.ground_truth_matches (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  transaction_id uuid not null references public.bank_transactions(id) on delete cascade,
  scenario_codes text[] not null,
  expected_action text not null,
  transaction_amount numeric(20, 2) not null,
  currency char(3) not null,
  expected_allocations jsonb not null,
  expected_adjustments jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  unique (batch_id, transaction_id),
  constraint ground_truth_matches_amount_check check (transaction_amount > 0),
  constraint ground_truth_matches_currency_check check (currency ~ '^[A-Z]{3}$')
);

create table private_evaluation.ground_truth_exceptions (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  record_type text not null,
  record_id uuid not null,
  reason_code text not null,
  unsafe boolean not null default true,
  expected_evidence jsonb not null default '[]'::jsonb,
  expected_next_action text not null,
  created_at timestamptz not null default now(),
  unique (batch_id, record_type, record_id, reason_code)
);

create table private_evaluation.ground_truth_cash_positions (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  position_date date not null,
  opening_cash numeric(20, 2) not null,
  actual_inflows numeric(20, 2) not null,
  actual_outflows numeric(20, 2) not null,
  actual_closing_cash numeric(20, 2) not null,
  currency char(3) not null,
  created_at timestamptz not null default now(),
  unique (batch_id, position_date),
  constraint ground_truth_cash_flows_check check (actual_inflows >= 0 and actual_outflows >= 0),
  constraint ground_truth_cash_currency_check check (currency ~ '^[A-Z]{3}$')
);

revoke all on all tables in schema private_evaluation from public;
alter default privileges in schema private_evaluation revoke all on tables from public;

-- Timestamp maintenance for mutable records.
create trigger organizations_set_updated_at before update on public.organizations
for each row execute function public.set_updated_at();
create trigger batches_set_updated_at before update on public.batches
for each row execute function public.set_updated_at();
create trigger customers_set_updated_at before update on public.customers
for each row execute function public.set_updated_at();
create trigger bank_transactions_set_updated_at before update on public.bank_transactions
for each row execute function public.set_updated_at();
create trigger invoices_set_updated_at before update on public.invoices
for each row execute function public.set_updated_at();
create trigger ledger_entries_set_updated_at before update on public.ledger_entries
for each row execute function public.set_updated_at();
create trigger remittances_set_updated_at before update on public.remittances
for each row execute function public.set_updated_at();
create trigger recurring_cash_flows_set_updated_at before update on public.recurring_cash_flows
for each row execute function public.set_updated_at();
create trigger match_proposals_set_updated_at before update on public.match_proposals
for each row execute function public.set_updated_at();
create trigger exceptions_set_updated_at before update on public.exceptions
for each row execute function public.set_updated_at();

-- Supabase should expose these tables only through the backend/service role.
-- Enabling RLS with no permissive client policy makes direct anon/authenticated
-- access fail closed while server-side validated tools retain service-role access.
alter table public.organizations enable row level security;
alter table public.organization_members enable row level security;
alter table public.batches enable row level security;
alter table public.uploaded_files enable row level security;
alter table public.customers enable row level security;
alter table public.customer_aliases enable row level security;
alter table public.bank_transactions enable row level security;
alter table public.invoices enable row level security;
alter table public.ledger_entries enable row level security;
alter table public.remittances enable row level security;
alter table public.recurring_cash_flows enable row level security;
alter table public.match_candidates enable row level security;
alter table public.match_proposals enable row level security;
alter table public.match_allocations enable row level security;
alter table public.match_evidence enable row level security;
alter table public.reconciliation_decisions enable row level security;
alter table public.exceptions enable row level security;
alter table public.forecast_runs enable row level security;
alter table public.forecast_daily_positions enable row level security;
alter table public.forecast_scenarios enable row level security;
alter table public.agent_runs enable row level security;
alter table public.agent_events enable row level security;
alter table public.tool_calls enable row level security;
alter table public.audit_events enable row level security;
alter table public.evaluation_runs enable row level security;
alter table public.evaluation_results enable row level security;

comment on schema private_evaluation is
  'Evaluator-only ground truth. Never grant this schema to the agent runtime role.';
comment on column public.reconciliation_decisions.idempotency_key is
  'Required for committed decisions and globally unique to make commit_match idempotent.';
comment on column public.match_evidence.evidence_reference is
  'Stable reference to evidence; raw datasets should not be duplicated into logs.';
comment on table public.audit_events is
  'Append-only evidence and policy audit chain.';

commit;

