# Migration Logic Preservation Report
**Project:** oracle_soa_test  
**Source:** oracle_soa  
**Generated:** 2026-05-25T18:07:26.470470+00:00  
**Preservation Score:** 80.0% (Grade: C)

## Summary
| Metric | Count |
|---|---|
| Total source steps | 12 |
| Fully preserved | 10 |
| Partially preserved | 2 |
| Needs human review | 0 |
| Blocked | 0 |
| Connections needing setup | 0 |

## Per-Flow Scores
| Flow | Steps | Score |
|---|---|---|
| AccountSyncBPEL | 2 | 100.0% |
| OrderProcessingBPEL | 6 | 83.3% |
| NotificationFanoutBPEL | 4 | 75.0% |

## Human Review Checklist

### MEDIUM Priority
- [ ] **[OrderProcessingBPEL]** `ValidateCredit` (OrderProcessingBPEL:step-6)
  - **Action:** Verify Boomi equivalent implements identical behavior; test with real integration data
  - **Boomi shape:** `REST_Connector_or_Oracle_EBS_Connector`

- [ ] **[NotificationFanoutBPEL]** `ParallelNotificationFanout` (NotificationFanoutBPEL:step-3)
  - **Action:** Replace parallel scatter-gather with sequential Boomi Branch; verify order-independence of routes
  - **Boomi shape:** `Branch`

## Score Deductions
| Flow | Step | Severity | Points |
|---|---|---|---|
| OrderProcessingBPEL | ValidateCredit | high | -10.0 |
| NotificationFanoutBPEL | ParallelNotificationFanout | high | -10.0 |
