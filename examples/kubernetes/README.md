# Kubernetes: Evaluating kagent with agentevals

Run agentevals alongside [kagent](https://github.com/kagent-dev/kagent) on Kubernetes to evaluate AI agent conversations in real time. This example deploys three components:

1. **agentevals** receives OTLP traces over HTTP and serves the evaluation UI
2. **OTel Collector** Optional, useful when you want centralized telemetry
controls.
3. **kagent** provides Kubernetes-native AI agents with built-in OTel instrumentation (gRPC export only)

```
kagent (gRPC :4317) --> OTel Collector( optional ) --> agentevals (gRPC :4317 / HTTP :4318)
                                                           |
                                                      UI on :8001
```

## Prerequisites

- A running Kubernetes cluster (kind, minikube, EKS, GKE, etc.)
- `helm` v3 installed
- `kubectl` configured for your cluster
- An OpenAI API key (`OPENAI_API_KEY`)

## Deploy

### 1. agentevals

```bash
helm install agentevals ./charts/agentevals \
  --set tag=0.6.3
```

This creates a single pod exposing:

| Port | Purpose |
|------|---------|
| 8001 | Web UI and API |
| 4317 | OTLP gRPC receiver (traces and logs) |
| 4318 | OTLP HTTP receiver (traces and logs) |
| 8080 | MCP (Streamable HTTP) |

### 2. OTel Collector (optional)

Native gRPC ingestion in agentevals is sufficient for most setups, but an
intermediate collector is still useful when you want centralized telemetry
controls:

- traffic shaping (batching, retries, backpressure)
- filtering or redaction before data reaches agentevals
- routing/fan-out to additional backends
- protocol translation for mixed clients

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

helm upgrade --install otel-collector open-telemetry/opentelemetry-collector \
  --namespace kagent --create-namespace \
  --set mode=deployment \
  --set replicaCount=1 \
  --set image.repository=otel/opentelemetry-collector \
  --set ports.otlp.enabled=true \
  --set ports.otlp-http.enabled=false \
  --set config.exporters.otlp.endpoint="agentevals.default.svc.cluster.local:4317" \
  --set config.exporters.otlp.compression="gzip" \
  --set config.service.pipelines.traces.receivers[0]=otlp \
  --set config.service.pipelines.traces.exporters[0]=otlp \
  --set config.service.pipelines.logs.receivers[0]=otlp \
  --set config.service.pipelines.logs.exporters[0]=otlp
```

> **Note:** If you deployed agentevals in a namespace other than `default`, update the `endpoint` value accordingly: `http://agentevals.<namespace>.svc.cluster.local:4317`.

### 3. kagent

Install the CRDs first, then the kagent operator with OTel tracing enabled:

```bash
helm install kagent-crds oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds \
  --namespace kagent \
  --create-namespace

helm upgrade --install kagent oci://ghcr.io/kagent-dev/kagent/helm/kagent \
  --namespace kagent \
  --set providers.default=openAI \
  --set providers.openAI.apiKey=$OPENAI_API_KEY \
  --set agents.kgateway-agent.enabled=false \
  --set agents.istio-agent.enabled=false \
  --set agents.promql-agent.enabled=false \
  --set agents.observability-agent.enabled=false \
  --set agents.argo-rollouts-agent.enabled=false \
  --set agents.cilium-policy-agent.enabled=false \
  --set agents.cilium-manager-agent.enabled=false \
  --set agents.cilium-debug-agent.enabled=false \
  --set otel.tracing.enabled=true \
  --set otel.tracing.exporter.otlp.endpoint="otel-collector-opentelemetry-collector.kagent.svc.cluster.local:4317" \
  --set otel.tracing.exporter.otlp.insecure=true
```

This installs kagent with only the default Helm agent (`helm-agent`) and the K8s troubleshooter enabled, and points its OTel exporter at the Collector.

> **Note:** If you are not running an OTel Collector, point `otel.tracing.exporter.otlp.endpoint` directly to the agentevals OTLP gRPC endpoint instead: `agentevals.default.svc.cluster.local:4317`.

### Verify the deployment

```bash
kubectl get pods -A -l 'app.kubernetes.io/name in (agentevals, kagent, opentelemetry-collector)'
```

All pods should be `Running` before continuing.

## Walkthrough: Comparing models with kagent and agentevals

This walkthrough shows how to evaluate two kagent agents side by side: the default Helm agent running `gpt-4.1-mini` and a new agent running `gpt-5`. You will chat with both agents, watch their traces stream into agentevals, select the better session as the evaluation baseline, and score both on tool trajectory and response match.

### Step 1. Access the UIs

Port-forward both services to your local machine:

```bash
# Terminal 1: agentevals UI
kubectl port-forward svc/agentevals 8001:8001

# Terminal 2: kagent UI
kubectl port-forward -n kagent svc/kagent 8083:8083
```

Open **http://localhost:8083** for the kagent UI and **http://localhost:8001** for the agentevals UI.

### Step 2. Create a GPT-5 agent

kagent ships with a default `helm-agent` configured to use `gpt-4.1-mini`. Create a second agent that uses `gpt-5` so you can compare the two.

**Option A: via the kagent UI**

1. Open http://localhost:8083
2. Navigate to the Agents page
3. Click **Create Agent**
4. Copy the configuration from the existing `helm-agent` (same system prompt, same tools)
5. Change the model to `gpt-5`
6. Name it `helm-agent-gpt5`
7. Save

**Option B: via a CRD**

Apply the following manifest (adjust the system prompt if needed):

```yaml
apiVersion: kagent.dev/v1alpha1
kind: Agent
metadata:
  name: helm-agent-gpt5
  namespace: kagent
spec:
  description: "Helm agent (GPT-5) for model comparison"
  modelConfig:
    model: gpt-5
    apiKeySecretRef:
      name: kagent-openai
      key: OPENAI_API_KEY
  systemPrompt: |
    You are a Kubernetes Helm expert. You help users manage Helm charts,
    releases, and repositories. Use your tools to inspect and manage
    Helm resources in the cluster.
  tools:
    - name: helm-list
    - name: helm-status
    - name: helm-get-values
    - name: helm-history
```

```bash
kubectl apply -f helm-agent-gpt5.yaml
```

### Step 3. Open agentevals Live view

1. Go to http://localhost:8001
2. Click **Live** in the sidebar to open the live streaming view
3. Leave this tab open. Sessions will appear as traces arrive.

### Step 4. Chat with both agents

Switch to the kagent UI (http://localhost:8083) and have the same conversation with each agent. For example:

**With `helm-agent` (gpt-4.1-mini):**

1. Select `helm-agent` from the agent list
2. Start a new conversation
3. Ask: *"List all Helm releases across all namespaces and tell me which ones have pending upgrades"*
4. Follow up: *"Show me the values for the agentevals release"*

**With `helm-agent-gpt5` (gpt-5):**

1. Select `helm-agent-gpt5` from the agent list
2. Start a new conversation
3. Ask the same questions in the same order

### Step 5. Watch traces in agentevals

Switch back to the agentevals Live view at http://localhost:8001. You will see two sessions appear, one for each conversation. Each session shows:

- **Status** transitioning from ACTIVE to COMPLETED as the conversation ends
- **Span count** incrementing in real time as the agent makes LLM calls and tool invocations
- **Model name** visible in the session metadata

### Step 6. Select the GPT-5 session as the eval set

Once both sessions are complete:

1. Click on the `helm-agent-gpt5` session card to open its trace details
2. Review the conversation: check that it called the right tools and produced correct responses
3. Click **Use as Eval Set** to mark this session as the evaluation baseline
4. Give it a name like `helm-agent-comparison`

This captures the GPT-5 session's tool trajectory and final responses as the golden reference.

<img width="1910" height="1498" alt="image" src="https://github.com/user-attachments/assets/2cabb4a8-c680-4742-a5be-403bac0487a3" />

### Step 7. Evaluate both sessions

1. Go back to the sessions list
2. Select both sessions (the `gpt-4.1-mini` session and the `gpt-5` session)
3. Click **Evaluate**
4. Select the `helm-agent-comparison` eval set
5. Choose the metrics:
   - **tool_trajectory_avg_score**: Did the agent call the correct tools in the correct order?
   - **response_match_score**: Did the agent produce responses consistent with the golden reference?
6. Run the evaluation

### What to look for

| Metric | What it tells you |
|--------|------------------|
| `tool_trajectory_avg_score` | Whether the agent followed the expected sequence of Helm tool calls (`helm-list`, then `helm-get-values`). A score of 1.0 means it matched exactly. |
| `response_match_score` | How closely the agent's final answers matched the GPT-5 baseline. Useful for catching regressions when switching to a cheaper model. |

Compare the two sessions in the results table:

- **Token usage**: The session metadata includes total token counts. If `gpt-5` consumed fewer tokens while achieving the same trajectory score, it may be the better choice for this use case.
- **Tool trajectory**: If one agent called extra tools or skipped expected ones, the trajectory score reflects that.
- **Response quality**: A lower response match score on the `gpt-4.1-mini` session highlights where the cheaper model diverged from the GPT-5 baseline.

<img width="1914" height="1154" alt="image" src="https://github.com/user-attachments/assets/5939a8d4-3775-4cf1-9cf2-d3b6b4afd582" />

You can also click an individual conversation and see a breakdown of each evaluators.

<img width="1916" height="1348" alt="image" src="https://github.com/user-attachments/assets/984b3d29-8018-4fcb-9036-bb7c6e97d9ff" />

## Cleanup

```bash
helm uninstall kagent -n kagent
helm uninstall kagent-crds -n kagent
helm uninstall otel-collector -n kagent
helm uninstall agentevals
kubectl delete namespace kagent
```
