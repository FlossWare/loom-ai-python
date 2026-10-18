# Distributed Worker HTTP Reference Binding

This module provides a minimal authenticated HTTP adapter and an independently
runnable polling Worker agent over the fleet state machine in `loom_ai.fleet`.
It is a reference binding for development and dogfood, not a production fleet
deployment.

## Coordinator

Create a coordinator and bind the HTTP adapter to loopback:

```python
from loom_ai.fleet import FleetCoordinator
from loom_ai.fleet_http import FleetHTTPServer

service = FleetHTTPServer(
    FleetCoordinator(),
    bearer_token=token_from_operator_configuration,
    host="127.0.0.1",
    port=8768,
)
http_server = service.start()
http_server.serve_forever()
```

The health endpoint is `GET /health`. All other routes require
`Authorization: Bearer <token>`:

- `POST /workers/register`: register stable Worker identity and capabilities.
- `POST /workers/{worker_id}/heartbeat`: refresh Worker liveness.
- `POST /workers/{worker_id}/claim`: claim one eligible task.
- `POST /workers/{worker_id}/drain`: stop new assignments.
- `POST /tasks`: submit an Intent with execution correlation, requirements,
  idempotency key, attempt limit, and optional deadline.
- `POST /tasks/{task_id}/renew`: renew a current lease using its identifier and
  fencing generation.
- `POST /tasks/{task_id}/result`: submit a result from the current lease holder.
- `GET /tasks/{task_id}`: observe task state and the recorded result.

## Worker agent

A process constructs its trusted Worker implementation and runs the agent:

```python
from loom_ai.fleet_http import FleetWorkerAgent

agent = FleetWorkerAgent(
    coordinator_url,
    token_from_operator_configuration,
    configured_worker,
    implementation_id="example.worker.v1",
    capabilities=["repository.read"],
    lease_seconds=15,
)
agent.run_forever()
```

The agent renews its lease while synchronous Worker execution is in progress. If
renewal fails, it discards the local result instead of submitting a stale result.
Worker exceptions are reported as retryable failures; retry limits remain
enforced by the coordinator. The runtime must not claim cancellation semantics
until a cancellation endpoint and worker-side cancellation propagation are
implemented.

## Security and durability boundary

The adapter uses a shared bearer token as a minimal bootstrap authentication
mechanism. It does not provide per-Worker credentials, token rotation, delegated
capability credentials, or TLS itself. Remote deployments MUST place it behind
an authenticated TLS boundary and must not expose the plain HTTP listener
directly to an untrusted network. A future production binding should replace the
shared-token model with deployment-appropriate peer identity and authorization.

The current coordinator state is in memory. Restart loses queued tasks, leases,
and registrations. The owning Loom Orchestrator remains responsible for durable
execution/result state; this reference adapter alone does not claim durable fleet
recovery or multi-host availability. Those are tracked in issue #935, along with
multi-process/live fleet qualification and Orchestrator proxy integration.
