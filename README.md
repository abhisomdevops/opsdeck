# OpsDeck

A web console for SRE and production operations. One screen for Kubernetes, Flux, Argo CD, Jenkins, Artifactory, AWS, Prisma Cloud, Jira and Confluence, plus an AI assistant that can investigate across all of them.

- **Overview page** ranks what needs attention across every system: crashlooping pods, out-of-sync apps, firing alarms, failing builds and critical security alerts.
- **A widget per system** has tables, filters, logs and drill-downs.
- **Gated actions:** rollout restart, scale, Argo CD sync, Flux reconcile, Jenkins build and Jira comment. Each one needs an explicit confirmation, is signed to your identity, is single-use, and is written to an audit log.
- **The AI assistant** (Claude, or Claude on AWS Bedrock) calls the same integrations as tools. It can read freely. It can only *propose* changes, and you click to run them.
- **Demo mode:** any system you haven't configured shows realistic sample data, so you can try everything with zero credentials.

---

## 1. Run it on your Mac

You need Python 3.11+ (`brew install python@3.12`) or Docker Desktop.

### Option A: Python (recommended for real use)

```bash
unzip opsdeck.zip && cd opsdeck
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
opsdeck                     # serves http://127.0.0.1:8080
```

Open http://127.0.0.1:8080. Everything runs on demo data until you add credentials.

### Option B: Docker

```bash
cp .env.example .env
docker compose up --build   # http://localhost:8080
```

The container mounts `~/.kube` and `~/.aws` read-only. Two cases need Option A instead:

- **EKS kubeconfigs** call the `aws` CLI to get tokens, and the image doesn't include it.
- **Local clusters** (kind/minikube) often have kubeconfigs pointing at `127.0.0.1`, which isn't reachable from inside the container.

### Connect real systems

Edit `.env` one system at a time and restart `opsdeck`. A system flips from **demo** to **live** in the left nav once its variables are set. The Overview's Connections grid shows whether it connected and the latency.

| System | Minimum settings | Credential to use |
|---|---|---|
| Kubernetes + Flux | `K8S_ENABLED=true`, optional `K8S_CONTEXT`, `K8S_NAMESPACES` | Your kubeconfig |
| Argo CD | `ARGOCD_URL`, `ARGOCD_TOKEN` | `argocd account generate-token --account opsdeck` (read-only RBAC) |
| Jenkins | `JENKINS_URL`, `JENKINS_USER`, `JENKINS_TOKEN` | User API token |
| Artifactory | `ARTIFACTORY_URL` (ends in `/artifactory`), `ARTIFACTORY_TOKEN` | Read-only access token |
| AWS | `AWS_REGIONS=us-east-1`, optional `AWS_PROFILE` | Standard AWS chain (SSO profile, keys, instance role) |
| Prisma Cloud | `PRISMA_URL`, `PRISMA_ACCESS_KEY`, `PRISMA_SECRET_KEY` | Access key on a read-only role |
| Jira | `JIRA_URL`, `JIRA_TOKEN`, plus `JIRA_USER` on Cloud | Cloud API token, or Data Center PAT with no user |
| Confluence | `CONFLUENCE_URL`, `CONFLUENCE_TOKEN`, plus `CONFLUENCE_USER` on Cloud | Same as Jira |
| Assistant | `ANTHROPIC_API_KEY`, or `OPSDECK_LLM_PROVIDER=bedrock` + `BEDROCK_MODEL_ID` | |

Behind a corporate TLS-inspecting proxy (Zscaler and similar), set `OPSDECK_CA_BUNDLE=/path/to/corp-ca.pem`.

Actions against **live** systems are blocked until you set `OPSDECK_ENABLE_WRITES=true`. Actions against demo integrations always work, so you can try the confirmation flow.

---

## 2. Run it on an EC2 instance running Kubernetes

These steps assume **k3s** on the instance, which is the most common single-node setup. Notes for EKS and kubeadm follow at the end.

### 2.1 Quickest: run it on the host, reach it over SSH

No image or chart needed. It reads the cluster through k3s's kubeconfig.

```bash
# on the EC2 instance
sudo dnf install -y python3.11 python3.11-pip unzip    # Amazon Linux 2023; use apt on Ubuntu
unzip opsdeck.zip && cd opsdeck
python3.11 -m venv .venv && . .venv/bin/activate && pip install -e .
cp .env.example .env
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown $USER ~/.kube/config
sed -i "s/^K8S_ENABLED=.*/K8S_ENABLED=true/" .env
opsdeck                                     # listens on 127.0.0.1:8080 only

# on your Mac
ssh -L 8080:127.0.0.1:8080 ec2-user@<ec2-host>
open http://localhost:8080
```

### 2.2 Proper: deploy into the cluster with Helm

**1. Install Helm on the instance:**

```bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml   # or ~/.kube/config as above
```

**2. Build the image and make it available to the cluster.** Pick one:

```bash
# (a) No registry: build with Docker, import into k3s's containerd
sudo dnf install -y docker && sudo systemctl start docker
sudo docker build -t opsdeck:0.1.0 .
sudo docker save opsdeck:0.1.0 | sudo k3s ctr images import -

# (b) ECR, which works for multi-node clusters and EKS too
aws ecr create-repository --repository-name opsdeck
aws ecr get-login-password | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
docker build -t <acct>.dkr.ecr.<region>.amazonaws.com/opsdeck:0.1.0 .
docker push <acct>.dkr.ecr.<region>.amazonaws.com/opsdeck:0.1.0
```

**3. Create the secrets.** Put tokens in a file that never goes to git. Same keys as `.env`, secrets only:

```bash
cat > prod.env <<'EOF'
ANTHROPIC_API_KEY=...
ARGOCD_TOKEN=...
JENKINS_USER=...
JENKINS_TOKEN=...
OPSDECK_SECRET=<output of: openssl rand -hex 32>
EOF
kubectl create namespace opsdeck
kubectl -n opsdeck create secret generic opsdeck-secrets --from-env-file=prod.env
```

**4. Install.** Non-secret settings go in a values file:

```yaml
# my-values.yaml
image: { repository: opsdeck, tag: "0.1.0", pullPolicy: Never }   # (b): your ECR repo, IfNotPresent
config:
  OPSDECK_ENV_NAME: prod
  AWS_REGIONS: us-east-1
  ARGOCD_URL: https://argocd-server.argocd.svc.cluster.local   # self-signed by default: see note below
  JENKINS_URL: https://jenkins.acme.internal
kubernetes:
  namespaces: [payments, checkout, flux-system]   # empty = whole cluster (ClusterRole)
  allowWrites: false                              # true lets restart/scale/reconcile work
```

```bash
helm install opsdeck deploy/helm/opsdeck -n opsdeck -f my-values.yaml
```

Argo CD's in-cluster endpoint uses a self-signed certificate by default. Either mount its CA with `extraVolumes`/`extraVolumeMounts` and set `config.OPSDECK_CA_BUNDLE`, or, if `argocd-server` runs with `--insecure`, use `http://argocd-server.argocd.svc.cluster.local`.

**5. Open it.** With auth off (the default), use a port-forward over SSH only:

```bash
kubectl -n opsdeck port-forward svc/opsdeck 8080:80     # on the instance
ssh -L 8080:127.0.0.1:8080 ec2-user@<ec2-host>          # on your Mac
open http://localhost:8080
```

**6. AWS access from the pod.** Attach an instance profile with `deploy/aws/opsdeck-readonly-policy.json`. Pods reach the instance metadata service only if the IMDSv2 hop limit is 2:

```bash
aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2 --http-tokens required
```

**7. Shared team access (SSO).** Enable the oauth2-proxy sidecar and an ingress. k3s ships Traefik:

```yaml
oauth2Proxy:
  enabled: true
  provider: oidc
  oidcIssuerUrl: https://<your-okta-or-entra-issuer>
  emailDomains: ["acme.com"]
  extraArgs: ["--redirect-url=https://opsdeck.acme.com/oauth2/callback"]
roles:
  admins: "you@acme.com"
  operators: "sre1@acme.com,sre2@acme.com"   # everyone else is a viewer
ingress: { enabled: true, className: traefik, host: opsdeck.acme.com, tlsSecret: opsdeck-tls }
persistence: { enabled: true }               # keep the audit log across restarts
```

```bash
kubectl -n opsdeck create secret generic opsdeck-oauth2 \
  --from-literal=client-id=... --from-literal=client-secret=... \
  --from-literal=cookie-secret=$(openssl rand -base64 32 | head -c 32)
helm upgrade opsdeck deploy/helm/opsdeck -n opsdeck -f my-values.yaml
```

With oauth2-proxy on, the app binds to `127.0.0.1` inside the pod. Identity headers can therefore only come from the proxy, and nobody can forge them from the network.

**EKS or kubeadm instead of k3s:** use option (b) for the image. On EKS, use IRSA or Pod Identity for AWS (`serviceAccount.annotations: {eks.amazonaws.com/role-arn: ...}`) instead of the instance profile, and an ALB or nginx ingress class.

---

## 3. Roles, safety and audit

| Role | Can |
|---|---|
| viewer | See everything and use the assistant (read-only tools) |
| operator | + low/medium-risk actions: restart, Argo sync, Flux reconcile, Jenkins build, Jira comment |
| admin | + high-risk actions: scale |

- **Local mode** (laptop, or a port-forward with auth off): you are admin. **Proxy mode:** roles come from `OPSDECK_ADMINS` / `OPSDECK_OPERATORS` by email.
- **Every action takes two steps.** *Prepare* returns an HMAC-signed token bound to the user, action and exact parameters. It expires in 5 minutes and can be used once. *Execute* re-checks permissions before running.
- **The assistant can only prepare.** It never sees a confirm button and cannot execute anything.
- **Live writes have three independent switches:**
  - `OPSDECK_ENABLE_WRITES` in the app
  - `kubernetes.allowWrites` in cluster RBAC
  - the permissions you grant each tool's token
- **Audit records go to two places:** SQLite at `$OPSDECK_DATA_DIR/audit.db` (shown under *Audit log*), and stdout as JSON (`{"audit": {...}}`) so Loki or CloudWatch can collect them.
- **Browser protections:** a strict CSP, no `innerHTML` with upstream data, and a required `X-OpsDeck` header on every POST to block CSRF.

---

## 4. Add your own widget

An integration is one Python file. It declares its operations; the UI and the assistant's tools are generated from those declarations, so you write no front-end code.

```python
# opsdeck/integrations/pagerduty.py
from ..config import env
from ..registry import Column, Integration, Param, RowAction, Section, action, read_op
from ._common import check, http_client


class PagerDuty(Integration):
    name, title, category = "pagerduty", "PagerDuty", "Work"
    description = "Open incidents and on-call."
    required_env = ("PAGERDUTY_TOKEN",)

    def _client(self):
        return http_client(self.settings, "https://api.pagerduty.com",
                           headers={"Authorization": f"Token token={env('PAGERDUTY_TOKEN')}"})

    @read_op("Open incidents.", (Param("urgency", enum=["high", "low"]),))
    def incidents(self, urgency=None):
        params = {"statuses[]": ["triggered", "acknowledged"]}
        if urgency:
            params["urgencies[]"] = urgency
        with self._client() as c:
            data = check(c.get("/incidents", params=params)).json()
        return [{"id": i["id"], "title": i["title"], "status": i["status"],
                 "service": i["service"]["summary"], "created": i["created_at"], "url": i["html_url"]}
                for i in data["incidents"]]

    def demo_incidents(self, urgency=None):  # sample data so it works without a token
        return [{"id": "P1", "title": "Checkout latency", "status": "triggered",
                 "service": "checkout", "created": "2026-10-22T18:00:00Z", "url": ""}]

    def widget(self):
        return [Section("Open incidents", "incidents", inputs=["urgency"], refresh_secs=60,
                        columns=[Column("title"), Column("status", fmt="status"), Column("service"),
                                 Column("created", fmt="time"), Column("url", fmt="link")])]

    def attention(self):
        n = len(self.invoke("incidents", {"urgency": "high"}))
        return [{"severity": "critical", "title": f"{n} high-urgency incidents", "detail": ""}] if n else []
```

Then add `PagerDuty` to `ALL_INTEGRATIONS` in `opsdeck/integrations/__init__.py`.

- **Actions** use `@action(..., risk="low"|"medium"|"high")` and automatically get the confirmation, role and audit flow.
- **Column formats:** `text`, `status`, `time`, `link`, `code`, `num`, `bytes`, `duration`.
- **Auto-reload:** run `make dev` and the server restarts whenever you save a file. Refresh the browser to pick up the change.

---

## 5. Project layout

```
opsdeck/
  main.py            FastAPI app: REST API, overview, chat, static UI
  registry.py        Integration base class, @read_op/@action, widget layout types
  actions.py         prepare -> confirm -> execute, signed single-use tokens
  auth.py            local vs proxy identity, roles
  audit.py           SQLite + JSON-log audit trail
  assistant.py       Claude tool-use loop over the registry
  integrations/      kubernetes, flux, argocd, jenkins, artifactory, aws, prisma, jira, confluence
  static/            index.html, app.js, app.css (no build step)
deploy/helm/opsdeck  Helm chart (RBAC, optional oauth2-proxy sidecar, ingress, PVC)
deploy/aws/          read-only IAM policy for the AWS widget
tests/               API, action-flow, auth, assistant loop, and live HTTP paths (mocked)
```

Run the tests with `make test`, or `pytest -q`.

## 6. Known gaps and next steps

- **Single cluster.** Supports one cluster and one Argo CD and Jenkins per instance. Multi-cluster means one `Kubernetes` instance per context.
- **Prisma.** Covers CSPM alerts only; Prisma Compute (image and runtime vulnerabilities) isn't wired up yet.
- **Assistant replies.** Answers arrive whole rather than streaming, and chat history lives only in the browser tab.
- **Audit storage.** Uses SQLite with one replica; for HA, swap `audit.py` for Postgres.
- **Natural next widgets:** PagerDuty or Opsgenie, Datadog or Grafana and Prometheus (alerts and SLO burn), GitHub (PRs and deploy status), ServiceNow (change records).
