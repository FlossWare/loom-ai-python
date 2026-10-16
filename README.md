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

- loom_submit_intent -> POST /intents
- loom_get_execution -> GET /executions/{execution_id}
- loom_continue_execution -> POST /executions/{execution_id}/continue
- loom_cancel_execution -> POST /executions/{execution_id}/cancel

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

## Development

    python -m ruff format --check .
    python -m ruff check .
    python -m pytest -q
    python -m build --wheel --sdist

See FlossWare engineering standard ADR-0024 for the contract-centric repository
naming and layering convention.


## Python import identity

The distribution is named flossware-loom-ai-python to make the implementation boundary explicit.

The Python import package remains loom_ai for compatibility with the existing Python implementation API. This import name is a language-specific implementation detail and is **not** the architectural identity or authority of the AI-domain contract.

A future loom-ai-java or loom-ai-erlang implementation does not share or depend on the Python import namespace.
