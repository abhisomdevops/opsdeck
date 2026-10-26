# Architecture

## Why build this instead of adopting an existing project

Surveyed October 2026. Several open-source projects cover parts of the brief; none covers the whole of it.

| Project | What it does well | What it lacks for this brief |
|---|---|---|
| [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) (CNCF Sandbox, Apache-2.0) | Strongest OSS investigation agent: 30+ toolsets (K8s, Prometheus, Grafana, Loki, Tempo, Datadog…), custom YAML toolsets, runbooks, multiple LLMs, gated remediation and PRs | No console UI or widgets; investigation-only. OpsDeck can call it through the optional `holmes` integration |
| [Aurora](https://github.com/Arvo-AI/aurora) (Arvo-AI) | Self-hosted incident platform: LangGraph agents, 30+ tools, sandboxed kubectl/cloud CLIs, dependency graph, RCAs exported to Confluence/Notion, OpenAI/Anthropic/Gemini/Ollama | Incident-centric; no user-built widgets or day-to-day console for tickets, CVEs, delivery |
| [RunLore](https://pkg.go.dev/github.com/Smana/runlore) | "What changed" RCA from Flux/Argo diffs, read-only with writes only as reviewed PRs, learns a knowledge base of past incidents | Alert-triggered chat bot, not an interactive console |
| [kagent](https://kagent.dev) | Generic in-cluster agent framework | A framework, not a product |
| [platformgen-py](https://github.com/techno-vet/platformgen-py) | AI-generated widgets with 1-second hot reload, 26+ widgets | Tkinter desktop app, single user, GitHub Copilot only, no gated actions or audit |
| [Glance](https://www.producthunt.com/products/glance-12) | AI writes real React widget components, stored and transpiled at runtime | Personal dashboard; no SRE integrations, RBAC or sandboxing |

OpsDeck combines the pieces: a multi-user web console with RBAC and audit (from the original OpsDeck), an agent loop in the style of HolmesGPT over the same integrations, and platformgen/Glance-style AI-built widgets with hot reload, sandboxed so they are safe to share across a team.

## Shape of the system

```
Browser (no build step)                         OpsDeck server (FastAPI, one process)
┌──────────────────────────────┐   REST/SSE    ┌───────────────────────────────────────────────┐
│ Overview · Dashboard ·       │◄─────────────►│ auth (local | oauth2-proxy headers) → roles   │
│ Widgets · Editor · per-tool  │               │                                               │
│ pages · Audit                │               │ Registry ── 21 Integration classes            │
│                              │               │   @read_op  → UI tables, AI tools, widget ops │
│ Widget host ──postMessage──┐ │               │   @action   → prepare/confirm/execute + audit │
│  <iframe sandbox>          │ │               │   demo_<op> → sample data with no creds       │
│   sdk.js + view.js         │ │               │                                               │
│                            ▼ │               │ Assistant: agent loop, playbooks, streaming   │
│ Assistant panel (SSE)        │               │   └ Providers: Anthropic · Bedrock · OpenAI(+ │
└──────────────────────────────┘               │     compatible, Azure) · Gemini · Ollama      │
                                               │ WidgetService: store · watcher · EventBus ·   │
                                               │   data runner · approvals · history · layouts │
                                               │ WidgetBuilder: AI author with self-correction │
                                               └───────────────┬───────────────────────────────┘
                                                               │ HTTPS (httpx, corp CA bundle), kubernetes client, boto3
               PagerDuty Jira Confluence Prometheus Alertmanager Grafana Loki Tempo Datadog New Relic
               Kubernetes Rancher AWS GitHub/GHES Argo CD Flux Jenkins Artifactory Prisma (CSPM+Compute)
               NVD EPSS CISA-KEV OSV HolmesGPT
```

### One declaration, three consumers

Every integration operation is declared once with `@read_op` / `@action` and typed `Param`s. From that:

- the integration page renders tables, filters and row actions;
- the agent gets a tool (`<integration>__<op>`) with a JSON schema;
- widgets can bind it as a data source or call it from `view.js`;
- the AI widget builder sees it in the operations catalog.

Adding an integration therefore lights up the UI, the agent and the widget system together.

### Agent loop

`assistant.py` runs a provider-neutral tool-use loop: the conversation is kept in a neutral format (`user` / `assistant` with tool calls / `tool` results) and each provider translates it (`llm.py`). Provider-specific state that must round-trip (Gemini thought signatures) is kept in a `raw` field and replayed verbatim. Each step is streamed to the browser as server-sent events (`tool`, `tool_result`, `text`, `proposal`, `widget`, `done` with token usage).

Read tools execute immediately. Action tools only **prepare**: they return an HMAC-signed, single-use, user-bound token and a human-readable summary (for PRs, the full unified diff against the base branch). Only the human's click executes it, after role and write-switch checks. Playbooks append task-specific instructions (RCA structure, ticket-to-PR steps, CVE prioritisation) to the system prompt.

### Widgets

See [WIDGETS.md](WIDGETS.md). The key decisions:

- **Hybrid model.** Declarative sources in `widget.json` cover most needs and are safe by construction (read ops only, declared up front). `view.js` gives full freedom of presentation inside a sandbox. `data.py` covers fan-out and joins on the server, gated by admin approval of its exact hash.
- **Files on disk are the source of truth**, so widgets can live in git, be reviewed in PRs and hot-reload from any writer. A content hash (`rev`) versions every change for cache busting and history.
- **Hot reload without restarts**: watcher → per-widget reload → EventBus → SSE → only affected iframes reload.

## How the seven use cases map

| Use case | Pieces |
|---|---|
| 1. Jira ticket → code change → PR | `jira.issue` (accepts URLs), `confluence.search/page`, `github.search_code/file/tree/pulls`, `github.create_pull_request` (action with diff), *Ticket to PR* playbook |
| 2. K8s namespaces/pods/deployments, restart with RBAC | `kubernetes.*`, `rollout_restart`/`scale` actions, Helm chart Role/ClusterRole with `allowWrites`, OpsDeck roles |
| 3. RCA from metrics, logs, traces, APM, paging | `prometheus`, `grafana`, `loki`, `tempo`, `datadog`, `newrelic`, `pagerduty`, plus change sources (`argocd`, `flux`, `github.compare/commits`, Grafana annotations), *Incident RCA* playbook, *Incident board* and *Golden signals* widgets |
| 4. CVEs from Prisma, enriched, mapped to code/IaC | `prisma_compute.vulnerabilities`, `cve.lookup/package/kev_recent`, `github.search_code/tree/file`, *CVE remediation* playbook, *CVE hotlist* widget |
| 5. Rancher workloads | `rancher.clusters/workloads/pods`, `rancher.restart` via Rancher's k8s proxy (Rancher RBAC applies), *Cluster health* widget |
| 6. Widgets on demand | `WidgetBuilder` (gallery, editor, or `opsdeck__build_widget` from chat), sandboxed runtime, hot reload |
| 7. Any question, any LLM | `Providers` (Anthropic, Bedrock, OpenAI/compatible/Azure, Gemini, Ollama), per-chat provider and model picker with live model lists |

## Extension points

| To add | Do |
|---|---|
| A system | One `Integration` subclass (see README §5) |
| A guided workflow | A Markdown playbook in `$OPSDECK_DATA_DIR/playbooks/` |
| A view | A widget (AI, editor, or files in `$OPSDECK_DATA_DIR/widgets/`) |
| A model | Any OpenAI-compatible endpoint via `OPENAI_BASE_URL`, or a new `Provider` subclass in `llm.py` |
| A second agent | Point `HOLMES_URL` at a HolmesGPT server; the agent can delegate with `holmes__ask` |
