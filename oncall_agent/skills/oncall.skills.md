# OnCall Agent Skills

## Triage Workflow

### Step 1: Signal Classification
- **Global First**: Signal appeared across all platforms before Windows-specific spike
- **Windows First**: Signal appeared on Windows before other platforms
- **Service-Specific**: Signal isolated to a specific service/API endpoint
- Determine scope by checking: platform distribution, geographic spread, affected services

### Step 2: Severity Assessment
| Level    | Criteria                                                      |
|----------|---------------------------------------------------------------|
| Critical | >50% success rate drop, user-facing, no workaround            |
| High     | 20-50% drop, user-facing, partial workaround exists           |
| Medium   | 5-20% drop, limited user impact, workaround available         |
| Low      | <5% drop, internal tooling, or cosmetic issue                 |

### Step 3: Common Signal Patterns

#### API Call Low Success Rate
- Check: endpoint-level breakdown, error codes (4xx vs 5xx), latency P50/P95/P99
- Common causes: upstream dependency failure, deployment rollout, config change, cert expiry
- Actions: check recent deployments, dependency health, traffic pattern changes

#### High CPU / Memory
- Check: process-level breakdown, garbage collection metrics, thread count
- Common causes: memory leak, infinite loop, traffic spike, noisy neighbor
- Actions: check autoscaling status, recent code changes, resource limits

#### Latency Spike
- Check: P50/P95/P99 breakdown, downstream dependency latency, queue depth
- Common causes: database slow queries, cold start, network issues, lock contention
- Actions: check DB query plans, connection pool stats, network path

#### Error Rate Increase
- Check: error type distribution, stack traces, affected endpoints
- Common causes: bad deployment, dependency outage, data corruption, rate limiting
- Actions: rollback assessment, dependency status check, error pattern analysis

#### Availability Drop
- Check: health check failures, pod restarts, node status
- Common causes: OOM kills, disk full, certificate issues, DNS resolution
- Actions: check pod events, node conditions, storage metrics

## Investigation Queries (Kusto/ADX Templates)

### Signal Overview
```kusto
SignalTable
| where SignalName == '{signal_name}'
| where Timestamp > ago(24h)
| summarize Count=count(), Platforms=make_set(Platform),
            FirstSeen=min(Timestamp), LastSeen=max(Timestamp)
| project SignalName, Count, Platforms, FirstSeen, LastSeen
```

### Trend Analysis (Week-over-Week)
```kusto
let ThisWeek = SignalTable
| where Timestamp > ago(7d)
| summarize CurrentCount=count();
let LastWeek = SignalTable
| where Timestamp between(ago(14d) .. ago(7d))
| summarize PreviousCount=count();
ThisWeek | join LastWeek on 1==1
| extend DeltaPercent = round((CurrentCount - PreviousCount) * 100.0 / PreviousCount, 1)
```

### Platform Breakdown
```kusto
SignalTable
| where SignalName == '{signal_name}'
| where Timestamp > ago(7d)
| summarize Count=count() by Platform
| order by Count desc
```

### Error Code Distribution
```kusto
RequestTable
| where Timestamp > ago(24h)
| where StatusCode >= 400
| summarize Count=count() by StatusCode, Endpoint
| order by Count desc
| take 20
```

### Recent Deployments
```kusto
DeploymentTable
| where Timestamp > ago(48h)
| project Timestamp, Service, Version, DeployedBy, Status
| order by Timestamp desc
```

## ICM (Incident Management) Procedures

### Creating an ICM Incident
- **Title**: `[{severity}] {signal_name} - {brief description}`
- **Owning Team**: Determined by service ownership mapping
- **Severity**: Maps from assessment (Critical→Sev1, High→Sev2, Medium→Sev3, Low→Sev4)
- **Impact**: User count estimate, geographic scope, duration

### Escalation Matrix
| Sev  | Response Time | Bridge Required | Manager Notify |
|------|--------------|-----------------|----------------|
| Sev1 | 15 min       | Yes             | Immediately    |
| Sev2 | 30 min       | Optional        | Within 1 hour  |
| Sev3 | 4 hours      | No              | Daily summary  |
| Sev4 | Next day     | No              | Weekly review  |

### Mitigation Strategies
1. **Rollback**: If caused by recent deployment, rollback to last known good
2. **Feature Flag**: Disable specific feature if isolated
3. **Traffic Shift**: Move traffic away from affected region/instance
4. **Scale Out**: Add capacity if resource-constrained
5. **Hotfix**: Emergency code fix for critical data issues

## Metric Connectors

### Key Metrics to Check
- **Success Rate**: `requests_total{status=~"2.."} / requests_total`
- **Error Rate**: `requests_total{status=~"5.."} / requests_total`
- **Latency P99**: `histogram_quantile(0.99, rate(request_duration_seconds_bucket[5m]))`
- **CPU Usage**: `rate(process_cpu_seconds_total[5m])`
- **Memory Usage**: `process_resident_memory_bytes / node_memory_MemTotal_bytes`
- **Pod Restarts**: `kube_pod_container_status_restarts_total`

### Dashboard Links (Templates)
- Grafana: `https://grafana.internal/d/{service}-overview`
- Azure Monitor: `https://portal.azure.com/#@/resource/{resource_id}/metrics`
- Kusto Web Explorer: `https://dataexplorer.azure.com/clusters/{cluster}/databases/{db}`
