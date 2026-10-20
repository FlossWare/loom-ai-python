# loom-ai-python

**Python implementation of the AI-domain contracts defined by FlossWare/loom-ai.**

Repository layering:

    loom
      |
      +-- loom-python
      |
      +-- loom-ai
             |
             +-- loom-ai-python

- loom defines the language-neutral Loom protocol and semantic contract.
- loom-python implements the foundational Loom contract in Python.
- loom-ai defines AI-domain contracts and semantics.
- loom-ai-python implements those AI-domain contracts in Python.

Python is one implementation language among peers. Future implementations such as
loom-ai-java and loom-ai-erlang implement the same loom-ai contracts without
depending on Python internals.

## Ownership

This repository owns:

- Python implementations of AI-domain contracts.
- Python runtime bindings and transport implementations.
- Python-specific tests and packaging.
- Python-specific dogfood and development tooling.

FlossWare/loom-ai remains authoritative for AI-domain contract semantics.
Generic Loom protocol semantics remain authoritative in FlossWare/loom.

## Current implementation

The Python implementation currently provides:

- Intent
- Worker and WorkerContext/WorkerResult
- Arbiter composition
- Orchestrator application service for execution, durable state, continuation,
  cooperative cancellation, and absolute deadlines
- Sequential multi-worker arbitration pipelines with context-preserving stage handoffs
- provider-neutral ModelProvider boundary
- ModelWorker
- deterministic FakeModelProvider
- HTTP transport binding
- conformance-oriented tests and dogfood

These are Python realizations. They do not define the underlying AI-domain contracts.

The sequential pipeline executes every configured stage unless a selected result
is cancelled. Each stage's evaluator selects one of its actual Worker Results;
the next stage receives that structured result, prior stage results, accumulated
evidence, and the original execution context. Worker contexts are isolated from
nested mutable state changes by sibling Workers.

## Architectural boundary

AI-specific behavior may extend the Loom execution model, but it must not redefine
or contradict generic Loom semantics.

Provider SDKs, credentials, model routing, budgets, caching, evaluation,
optimization strategies, and other reusable capabilities remain separate
capabilities rather than becoming implicit responsibilities of this repository.

## Running the HTTP boundary

Install the package, then choose one durable execution-state backend.

Filesystem snapshots:

```sh
loom-server --host 127.0.0.1 --port 8000 \
  --state-dir "$HOME/.local/state/flossware-loom/executions"
```

PostgreSQL JSONB snapshots:

```sh
pip install -e '.[postgres]'
export FLOSSWARE_LOOM_POSTGRES_DSN='postgresql://loom_user@db-host:5433/loom'
loom-server --host 127.0.0.1 --port 8000 --postgres-schema loom_runtime
```

The PostgreSQL adapter creates a dedicated `loom_runtime.execution_states`
table. It persists the complete versioned execution envelope, including intent,
state, result, evidence, evaluations, metadata, and provenance. The execution ID
is a primary key; create-only submissions cannot silently overwrite an existing
identity. The database DSN is read from the environment or can be supplied with
`--postgres-dsn`. Use a least-privilege database role and a secret manager or
service environment rather than putting credentials in shell history or source.

Use either `--state-dir` or `--postgres-dsn`, not both. Omitting both deliberately
disables durable observation and continuation; the server will still respond to
`/health` and accept ephemeral smoke-test submissions. The database backend
shares the existing `ExecutionStateStore` port with the filesystem adapter, so
Orchestrator lifecycle logic does not depend on the backend. Fleet snapshots
have their own `FleetStateStore` port and can independently use PostgreSQL
through `--fleet-postgres-dsn` or `FLOSSWARE_LOOM_FLEET_POSTGRES_DSN`. The two
tables are `loom_runtime.execution_states` and `loom_runtime.fleet_states`;
they persist distinct lifecycle boundaries and are not one cross-table
transaction.

The unit tests use a fake connection and run in the normal quality gate. For a
live process-independent database acceptance test, install the optional driver
and run this against a disposable database:

```sh
pip install -e '.[postgres,dev]'
export FLOSSWARE_LOOM_POSTGRES_DSN='postgresql://loom_user@db-host:5433/loom'
export FLOSSWARE_LOOM_FLEET_POSTGRES_DSN="$FLOSSWARE_LOOM_POSTGRES_DSN"
bash scripts/test-postgres-recovery.sh
```

The live tests create, reload, update, and remove uniquely named execution and
fleet snapshots. CI skips these checks unless both DSNs are configured; a green
unit-test run is not evidence that the deployed database is reachable.

**Important limitation:** without fleet configuration, the CLI's built-in Worker is
a no-op transport smoke Worker. To route ordinary REST Intent submissions through
independently running fleet Workers, configure both durable state stores and the
fleet bearer token:

```sh
export LOOM_FLEET_TOKEN='replace-with-a-long-random-token'
loom-server \
  --host 127.0.0.1 --port 8000 \
  --postgres-schema loom_runtime \
  --fleet-state-dir "$HOME/.local/state/flossware-loom/fleet" \
  --fleet-host 127.0.0.1 --fleet-port 8001
```

The server submits each `POST /intents` execution through a FleetDispatchWorker,
which creates a correlated task in the coordinator and waits for a remote Worker
result. For PostgreSQL-backed execution and fleet recovery, install the optional
driver and configure both state stores:

```sh
pip install -e '.[postgres]'
export FLOSSWARE_LOOM_POSTGRES_DSN='postgresql://loom_user@db-host:5433/loom'
export FLOSSWARE_LOOM_FLEET_POSTGRES_DSN="$FLOSSWARE_LOOM_POSTGRES_DSN"
export LOOM_FLEET_TOKEN='replace-with-a-long-random-token'
loom-server --host 127.0.0.1 --port 8000 --fleet-host 127.0.0.1 --fleet-port 8001
```

The fleet store defaults to the separate `FLOSSWARE_LOOM_FLEET_POSTGRES_DSN`
variable so filesystem and database choices remain explicit. Both database
tables are created idempotently in the configured schema. Workers register with
the same token and advertise the `loom.execute` capability. Install the package
in the Worker environment and run the independent process with a module/attribute
reference to a Worker instance or class:

```sh
export LOOM_FLEET_TOKEN='the-same-long-random-token'
export LOOM_FLEET_URL='http://127.0.0.1:8001'
loom-fleet-worker \
  --worker my_worker:worker \
  --implementation-id my-worker-0.1 \
  --capability loom.execute
```

The Worker module must be importable in that environment. The runner handles
registration, polling, lease renewal, result reporting, and graceful drain on
interrupt. For a remote host, set `LOOM_FLEET_URL` to the fleet listener's
reachable address and secure the connection with a trusted network or TLS proxy.

The worker-facing HTTP service accepts registration, heartbeat, claim, lease-renewal,
result, and observation operations. It deliberately does not expose task submission:
ordinary task creation stays inside the Orchestrator boundary. The filesystem
coordinator store supports one active coordinator only; it is not a multi-leader
protocol. For remote hosts, use a trusted network or terminate TLS at a correctly
configured reverse proxy because bearer tokens do not encrypt HTTP traffic.


## Generic MCP interface

For the current route inventory, JSON representations, error behavior, compatibility constraints, and ADR-0031 adoption gaps, see [REST API contract inventory](docs/REST_API.md).

Loom exposes a generic MCP server through scripts/loom_mcp_server.py. MCP is a
protocol boundary, not a client-specific adapter. Any MCP-capable host can consume
the same Loom server; Crush and Claude are examples of clients, not special cases.

The HTTP server is deliberately thin and delegates submit, observe, continue, and
cancel operations to the Orchestrator application service. The MCP adapter maps
tools to the same public Loom HTTP boundary. Submit accepts an optional absolute
RFC 3339 `deadline`; cancellation is cooperative and reports whether termination
is still pending.

Continuation is valid only for an execution explicitly marked `interrupted`
because more work or external input is required. A completed execution is
terminal and cannot be reopened; a continuation request against it returns
`409 Conflict`. An Arbiter can interrupt a successful phase without declaring
the overall execution complete, preserving the same identity and durable state
for a later continuation.

- loom_submit_intent -> POST /intents
- loom_get_execution -> GET /executions/{execution_id}
- loom_continue_execution -> POST /executions/{execution_id}/continue
- loom_cancel_execution -> POST /executions/{execution_id}/cancel
- loom_record_evaluation -> POST /executions/{execution_id}/evaluations

Durable observation returns separate `lifecycle`, legacy-compatible `status`,
structured `result`, and `evaluation` / `evaluations` fields. `status` is
retained for existing clients and describes the prior Worker outcome; it must not
be used alone as proof that task acceptance criteria were met. The lifecycle
indicates whether the execution is running, interrupted, or terminal. Evaluation
status is one of `met`, `partially_met`, `not_met`, `inconclusive`, or
`not_evaluated`. A completed execution defaults to `not_evaluated` until an
explicit assessment is recorded. New assessments are appended and do not rewrite
the execution lifecycle or Result.

The MCP server does not import Loom execution classes, own execution state, replay
client transcripts, or execute repository work. Loom remains responsible for Intent
execution, Worker/Arbiter orchestration, durable execution state, verification,
and evidence/provenance.

Install the MCP extra before running the server:

    python -m pip install 'flossware-loom-ai-python[mcp]'

Then configure any MCP-capable host to launch:

    python3 /path/to/loom-ai-python/scripts/loom_mcp_server.py

with:

    LOOM_URL=http://127.0.0.1:8000

The MCP adapter allows up to 900 seconds per HTTP request by default so a
synchronous Loom submission or continuation is not prematurely abandoned.
Override this with the positive-integer environment variable
`LOOM_HTTP_TIMEOUT` when configuring the MCP server. The timeout bounds how
long the client waits; it does not cancel an execution that may still be running
on the Loom server.

For real submit/observe/continue behavior across process boundaries, point
LOOM_URL at a Loom server profile backed by a durable ExecutionStateStore (for
example, the durable dogfood profile). The default Loom transport-smoke server
is useful for protocol testing but is not itself a durable-storage guarantee.

For hosts that support project-local MCP configuration, the same command and
environment are used regardless of which host consumes Loom.

## Durable process-boundary dogfood

The default `loom-server` command remains a transport-smoke entrypoint. The
durable dogfood profile is a separate qualification tool that composes the public
HTTP boundary, `FileExecutionStateStore`, a bounded repository-changing Worker,
and a verification Worker.

Run the process-boundary qualification from a checkout with Git and Python 3.11+:

    bash scripts/dogfood-process-boundary.sh

The script clones a fresh runtime checkout and a separate task checkout, submits a
real task through HTTP, verifies the result, terminates the server process,
restarts it, observes the same `execution_id`, continues the execution without
replaying a transcript, and verifies the follow-up change. The script modifies
only its temporary task checkout and removes its temporary workspace on exit.

Optional ref/port overrides:

- `LOOM_DOGFOOD_REF`: runtime branch or tag (default `main`)
- `LOOM_DOGFOOD_TASK_REF`: task repository branch or tag (default `main`)
- `LOOM_DOGFOOD_PORT`: loopback port (default `18766`)

This is a qualification profile, not a production workflow or a deployed-service
restart test. Systemd/deployment qualification is tracked separately in
`FlossWare/loom-setup`.

## Real Crush-to-Loom dogfood

To qualify a fresh Crush session against a real checkout of `FlossWare/loom-ai`,
use:

    python scripts/dogfood-crush-mcp-real.py --timeout-seconds 900

The harness creates a disposable project-local `.crushrc` that explicitly
configures the generic Loom MCP server and a FlossWare OpenAI-compatible model
provider. The default gateway is `http://127.0.0.1:8765/v1`, with model
`flossware/flossware`. Ensure that gateway is running and reachable from the
dogfood host. Override the defaults with `FLOSSWARE_CRUSH_BASE_URL` and
`FLOSSWARE_CRUSH_MODEL`, or pass `--provider-base-url` and `--model`
directly.

The harness uses an isolated temporary Crush data directory, so it does not
depend on provider settings in the user's global Crush profile. It makes one
bounded documentation change in the real `loom-ai` repository, submits and
continues the execution through generic MCP, then independently checks the
persisted execution status, intent identity, provenance, changed-file scope,
and `git diff --check`.

Use `--expect-failure` to qualify the negative path. That run must preserve a
failed verification result rather than misreporting success. A successful unit
or MCP smoke test is not a substitute for this fresh-agent qualification; record
the execution ID and evidence when the full path passes.

## Development

    python -m ruff format --check .
    python -m ruff check .
    python -m pytest -q
    python -m build --wheel --sdist

See FlossWare/standards ADR-0024 for the contract-centric repository
naming and layering convention.


## Python import identity

The distribution is named flossware-loom-ai-python to make the implementation boundary explicit.

The Python import package remains loom_ai for compatibility with the existing Python implementation API. This import name is a language-specific implementation detail and is **not** the architectural identity or authority of the AI-domain contract.

A future loom-ai-java or loom-ai-erlang implementation does not share or depend on the Python import namespace.
