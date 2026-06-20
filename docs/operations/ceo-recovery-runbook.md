# CEO Agent Recovery Runbook

> **Canonical home moved.** This CEO-specific runbook is fully subsumed by the
> generalized **[Agent Liveness Recovery Runbook](agent-liveness-recovery-runbook.md)**
> ([FLO-395](/FLO/issues/FLO-395)), which covers every senior agent with one rule:
> *recovery owner = the stuck agent's manager*. The CEO remains a covered
> instance — its manager is the board, the configured peer (Software Architect)
> owns the release+restart attempts, and the board is the escalation backstop.
>
> The historical CEO-only content (mechanism: [FLO-267](/FLO/issues/FLO-267)) is
> preserved in git history at the commit that introduced this file. All
> procedure, constants, verification, and routine configuration now live in the
> [generalized runbook](agent-liveness-recovery-runbook.md).
>
> Related: [CEO heartbeat timeout enforcement](ceo-heartbeat-timeout.md) ([FLO-265](/FLO/issues/FLO-265)) ·
> [CEO heartbeat monitoring](ceo-heartbeat-monitoring.md) ([FLO-266](/FLO/issues/FLO-266)) ·
> [CEO silent-run analysis](../architecture/ceo-silent-run-analysis.md) ([FLO-264](/FLO/issues/FLO-264)).
