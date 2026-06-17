# Observability Stack (PR #12b)

Prometheus + Grafana + Loki + Promtail running alongside the Gateway
stack on a single Jetson. Read-only consumers of the `/metrics`
endpoints that Gateway (PR #12a) and vLLM (built-in) expose.

**Hardware metrics (CPU/GPU temp/power) are out of scope.** Those belong
to the other team's stack (jtop-exporter etc.). This stack only
visualises our application metrics + docker logs.

## What's where

```
ops/
├── prometheus/
│   └── prometheus.yml             # scrape gateway:8080 + vllm:8000
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/
│   │   │   └── datasources.yml    # Prometheus + Loki auto-wired
│   │   └── dashboards/
│   │       └── dashboards.yml     # auto-load from /etc/grafana/dashboards
│   └── dashboards/
│       ├── gateway-ops.json       # main SRE dashboard
│       └── vllm.json              # vLLM internals deep-dive
├── loki/
│   └── loki-config.yml            # filesystem TSDB, 7d retention
└── promtail/
    └── promtail-config.yml        # docker SD, opt-in via `logging=promtail` label

docker/
└── docker-compose.observability.yaml   # the 4 services above
```

## First-time setup

1. **Set Grafana password** in `docker/.env`:

   ```
   GRAFANA_PASSWORD=<strong-random-secret>
   ```

   Grafana refuses to start without this — no default `admin/admin`.

2. **Bring up the stack** (alongside the main one):

   ```bash
   cd docker
   docker compose \
     -f docker-compose.yaml \
     -f docker-compose.override.yaml \
     -f docker-compose.observability.yaml \
     up -d prometheus grafana loki promtail
   ```

   Or set in your shell profile so the flag set is implicit:

   ```bash
   export COMPOSE_FILE=docker-compose.yaml:docker-compose.override.yaml:docker-compose.observability.yaml
   ```

3. **Verify** (from the host):

   ```bash
   curl -fs http://localhost:9090/-/healthy        # Prometheus
   curl -fs http://localhost:3100/ready             # Loki
   curl -fs http://localhost:3000/api/health        # Grafana
   ```

4. **Open Grafana**: `http://localhost:3000`, login `admin` + the
   `GRAFANA_PASSWORD` you set. Dashboards auto-load under folder
   "Gateway".

## Adding a new dashboard

1. Build it in Grafana UI (right-click panel → Inspect → Panel JSON).
2. Export → Save JSON.
3. Drop into `ops/grafana/dashboards/`.
4. Grafana auto-reloads within 30s (no restart).

## Adding new docker services to log shipping

Add this label to the service in your compose file:

```yaml
labels:
  logging: promtail
```

Promtail discovers it within 15s.

## Query examples

**Gateway latency p99 last 5m:**

```promql
histogram_quantile(0.99,
  sum by (le, route) (
    rate(gateway_request_duration_seconds_bucket[5m])
  )
)
```

**Recent Gateway errors in Loki:**

```logql
{service="gateway", level="error"} | json | line_format "{{.event}} {{.error}}"
```

**Cross-service request trace by request_id:**

```logql
{service=~"gateway|dify"} |= "req_abc123"
```

## Ports

All bound to `127.0.0.1` by default — reach via SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 3100:127.0.0.1:3100 jetson
```

Then in your browser:
- http://localhost:3000 — Grafana
- http://localhost:9090 — Prometheus UI (rarely needed)
- http://localhost:3100 — Loki (no UI; query via Grafana)

**Never** expose 3000 directly to the internet. Grafana is for ops only.

## Resource budget

Total ~500 MB RAM on the Jetson:
- Prometheus ~ 200 MB (1k active series, 30d retention)
- Grafana ~ 100 MB
- Loki ~ 150 MB (moderate ingest)
- Promtail ~ 50 MB

Well within Jetson Thor's 128 GB. If we ever scale this stack beyond
edge (Phase 4 fleet), Loki swaps to S3 backend and Prometheus federates
to a central plane.

## What's NOT here (yet)

| Future | When |
|---|---|
| Alertmanager (Slack/PagerDuty) | Phase 5 — needs alert rules + runbooks |
| OpenTelemetry traces | PR #14 — request_id cross-service tracing |
| Federation to multi-station | Phase 4 — when there's >1 Jetson |
| Hardware metrics | Other team's responsibility |

See `Doc: Observability Architecture` in Notion for the long-form
design rationale.
