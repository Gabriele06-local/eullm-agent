use anyhow::{Context, Result};
use async_trait::async_trait;
use serde_json::{json, Value};
use sqlx::sqlite::{
    SqliteConnectOptions, SqliteJournalMode, SqlitePool, SqlitePoolOptions, SqliteRow,
    SqliteSynchronous,
};
use sqlx::Row;
use std::str::FromStr;
use std::time::Duration;
use uuid::Uuid;

use super::*;

/// Run state and audit in one SQLite file: the local installation, for one
/// user on one machine. Same tables and rules as [`super::PgStore`].
#[derive(Clone)]
pub struct SqliteStore {
    pool: SqlitePool,
    /// This process, as written on its runs and refreshed in `instances`.
    instance: String,
    /// How long a process may go without refreshing before its runs count
    /// as interrupted.
    stale_after: Duration,
    _heartbeat: std::sync::Arc<Heartbeat>,
}

/// Stops refreshing `instances` when the store is dropped.
struct Heartbeat(tokio::task::JoinHandle<()>);

impl Drop for Heartbeat {
    fn drop(&mut self) {
        self.0.abort();
    }
}

static MIGRATOR: sqlx::migrate::Migrator = sqlx::migrate!("./migrations_sqlite");

/// A process refreshes its row this often; it counts as gone after three
/// missed refreshes.
const HEARTBEAT: Duration = Duration::from_secs(10);

impl SqliteStore {
    /// Open (creating it if needed) the database at `url`
    /// (`sqlite://path/to/file.db`) and apply pending migrations.
    pub async fn connect(url: &str, max_connections: u32) -> Result<Self> {
        Self::connect_with(url, max_connections, HEARTBEAT).await
    }

    /// As [`Self::connect`], refreshing this process every `heartbeat`.
    pub async fn connect_with(
        url: &str,
        max_connections: u32,
        heartbeat: Duration,
    ) -> Result<Self> {
        let options = SqliteConnectOptions::from_str(url)
            .with_context(|| "invalid SQLite database url".to_string())?
            .create_if_missing(true)
            .foreign_keys(true)
            // Readers do not block the writer and the other way round.
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            // One writer at a time: others wait instead of failing.
            .busy_timeout(Duration::from_secs(10));
        let pool = SqlitePoolOptions::new()
            .max_connections(max_connections.max(1))
            .acquire_timeout(Duration::from_secs(10))
            .connect_with(options)
            .await
            .context("cannot open the SQLite database")?;
        MIGRATOR
            .run(&pool)
            .await
            .context("database migration failed")?;
        let instance = Uuid::new_v4().to_string();
        let beat = {
            let pool = pool.clone();
            let instance = instance.clone();
            // The first refresh is in place before connect returns.
            sqlx::query("INSERT INTO instances (id, seen_at) VALUES ($1, $2)")
                .bind(&instance)
                .bind(now_ms())
                .execute(&pool)
                .await?;
            tokio::spawn(async move {
                let mut tick = tokio::time::interval(heartbeat);
                tick.tick().await;
                loop {
                    tick.tick().await;
                    let _ = sqlx::query("UPDATE instances SET seen_at = $2 WHERE id = $1")
                        .bind(&instance)
                        .bind(now_ms())
                        .execute(&pool)
                        .await;
                }
            })
        };
        Ok(Self {
            pool,
            instance,
            stale_after: heartbeat * 3,
            _heartbeat: std::sync::Arc::new(Heartbeat(beat)),
        })
    }

    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }

    async fn audit(
        &self,
        tx: &mut sqlx::SqliteConnection,
        tenant: &str,
        run_id: Option<&str>,
        event: &str,
        payload: Value,
    ) -> Result<()> {
        sqlx::query(
            "INSERT INTO audit_events (tenant, run_id, event, payload, created_at) \
             VALUES ($1, $2, $3, $4, $5)",
        )
        .bind(tenant)
        .bind(run_id)
        .bind(event)
        .bind(payload.to_string())
        .bind(now_ms())
        .execute(tx)
        .await?;
        Ok(())
    }

    fn approval_from_row(r: &SqliteRow) -> Result<Approval> {
        Ok(Approval {
            id: r.try_get("id")?,
            tenant: r.try_get("tenant")?,
            run_id: r.try_get("run_id")?,
            tool: r.try_get("tool")?,
            arguments: serde_json::from_str(r.try_get::<&str, _>("arguments")?)?,
            reason: r.try_get("reason")?,
            status: ApprovalStatus::parse(r.try_get::<&str, _>("status")?)
                .unwrap_or(ApprovalStatus::Expired),
            decided_by: r.try_get("decided_by")?,
            decision_note: r.try_get("decision_note")?,
            created_at_ms: r.try_get("created_at")?,
            decided_at_ms: r.try_get("decided_at")?,
        })
    }

    fn summary_from_row(r: &SqliteRow) -> Result<RunSummary> {
        Ok(RunSummary {
            id: r.try_get("id")?,
            tenant: r.try_get("tenant")?,
            profile: r.try_get("profile")?,
            source: r.try_get("source")?,
            status: RunStatus::parse(r.try_get::<&str, _>("status")?),
            created_at_ms: r.try_get("created_at")?,
            finished_at_ms: r.try_get("finished_at")?,
        })
    }
}

const APPROVAL_COLUMNS: &str = "id, tenant, run_id, tool, arguments, reason, status, decided_by, \
     decision_note, created_at, decided_at";

#[async_trait]
impl Store for SqliteStore {
    async fn create_run(&self, run: &NewRun) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO runs (id, tenant, profile, source, status, input, instance, created_at) \
             VALUES ($1, $2, $3, $4, 'queued', $5, $6, $7)",
        )
        .bind(&run.id)
        .bind(&run.tenant)
        .bind(&run.profile)
        .bind(&run.source)
        .bind(&run.input)
        .bind(&self.instance)
        .bind(now_ms())
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            &run.tenant,
            Some(&run.id),
            "run_created",
            json!({ "profile": run.profile, "source": run.source }),
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn set_run_status(&self, id: &str, status: RunStatus) -> Result<()> {
        sqlx::query("UPDATE runs SET status = $2 WHERE id = $1")
            .bind(id)
            .bind(status.as_str())
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    async fn finish_run(&self, id: &str, end: &RunEnd) -> Result<()> {
        let status = if end.error.is_some() {
            RunStatus::Failed
        } else {
            RunStatus::Succeeded
        };
        let mut tx = self.pool.begin().await?;
        let tenant: Option<String> = sqlx::query_scalar(
            "UPDATE runs SET status = $2, output = $3, error = $4, iterations = $5, \
             input_tokens = $6, output_tokens = $7, cost = $8, tainted = $9, finished_at = $10 \
             WHERE id = $1 RETURNING tenant",
        )
        .bind(id)
        .bind(status.as_str())
        .bind(&end.output)
        .bind(&end.error)
        .bind(end.iterations as i64)
        .bind(end.input_tokens as i64)
        .bind(end.output_tokens as i64)
        .bind(end.cost)
        .bind(end.tainted)
        .bind(now_ms())
        .fetch_optional(&mut *tx)
        .await?;
        if let Some(tenant) = tenant {
            self.audit(
                &mut tx,
                &tenant,
                Some(id),
                "run_finished",
                json!({
                    "status": status.as_str(),
                    "iterations": end.iterations,
                    "input_tokens": end.input_tokens,
                    "output_tokens": end.output_tokens,
                    "cost": end.cost,
                    "tainted": end.tainted,
                    "error": end.error,
                }),
            )
            .await?;
        }
        tx.commit().await?;
        Ok(())
    }

    async fn get_run(&self, tenant: &str, id: &str) -> Result<Option<RunView>> {
        let Some(r) = sqlx::query(
            "SELECT id, tenant, profile, source, status, input, output, error, iterations, \
             input_tokens, output_tokens, cost, tainted, created_at, finished_at \
             FROM runs WHERE id = $1 AND tenant = $2",
        )
        .bind(id)
        .bind(tenant)
        .fetch_optional(&self.pool)
        .await?
        else {
            return Ok(None);
        };

        let mut steps: Vec<(i64, Step)> = Vec::new();
        for row in sqlx::query(
            "SELECT provider, model, duration_ms, input_tokens, output_tokens, cost, error, \
             created_at FROM llm_calls WHERE run_id = $1 ORDER BY id",
        )
        .bind(id)
        .fetch_all(&self.pool)
        .await?
        {
            let at_ms: i64 = row.try_get("created_at")?;
            steps.push((
                at_ms,
                Step::LlmCall {
                    at_ms,
                    call: LlmCallRecord {
                        provider: row.try_get("provider")?,
                        model: row.try_get("model")?,
                        duration_ms: row.try_get::<i64, _>("duration_ms")? as u64,
                        input_tokens: row
                            .try_get::<Option<i64>, _>("input_tokens")?
                            .map(|v| v as u64),
                        output_tokens: row
                            .try_get::<Option<i64>, _>("output_tokens")?
                            .map(|v| v as u64),
                        cost: row.try_get("cost")?,
                        error: row.try_get("error")?,
                    },
                },
            ));
        }
        for row in sqlx::query(
            "SELECT tool, argument_keys, decision, decision_reason, approval_id, duration_ms, \
             output_bytes, error, created_at FROM tool_calls WHERE run_id = $1 ORDER BY id",
        )
        .bind(id)
        .fetch_all(&self.pool)
        .await?
        {
            let at_ms: i64 = row.try_get("created_at")?;
            steps.push((
                at_ms,
                Step::ToolCall {
                    at_ms,
                    call: ToolCallRecord {
                        tool: row.try_get("tool")?,
                        argument_keys: serde_json::from_str(
                            row.try_get::<&str, _>("argument_keys")?,
                        )?,
                        decision: row.try_get("decision")?,
                        decision_reason: row.try_get("decision_reason")?,
                        approval_id: row.try_get("approval_id")?,
                        duration_ms: row.try_get::<i64, _>("duration_ms")? as u64,
                        output_bytes: row
                            .try_get::<Option<i64>, _>("output_bytes")?
                            .map(|v| v as u64),
                        error: row.try_get("error")?,
                    },
                },
            ));
        }
        // Stable sort keeps insertion order within the same millisecond.
        steps.sort_by_key(|(at, _)| *at);

        Ok(Some(RunView {
            summary: Self::summary_from_row(&r)?,
            input: r.try_get("input")?,
            output: r.try_get("output")?,
            error: r.try_get("error")?,
            iterations: r.try_get::<i64, _>("iterations")? as u64,
            input_tokens: r.try_get::<i64, _>("input_tokens")? as u64,
            output_tokens: r.try_get::<i64, _>("output_tokens")? as u64,
            cost: r.try_get("cost")?,
            tainted: r.try_get("tainted")?,
            steps: steps.into_iter().map(|(_, s)| s).collect(),
        }))
    }

    async fn list_runs(&self, tenant: &str, limit: u32) -> Result<Vec<RunSummary>> {
        sqlx::query(
            "SELECT id, tenant, profile, source, status, created_at, finished_at FROM runs \
             WHERE tenant = $1 ORDER BY created_at DESC, rowid DESC LIMIT $2",
        )
        .bind(tenant)
        .bind(limit as i64)
        .fetch_all(&self.pool)
        .await?
        .iter()
        .map(Self::summary_from_row)
        .collect()
    }

    async fn record_llm_call(
        &self,
        tenant: &str,
        run_id: Option<&str>,
        call: &LlmCallRecord,
    ) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO llm_calls (tenant, run_id, provider, model, duration_ms, \
             input_tokens, output_tokens, cost, error, created_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        )
        .bind(tenant)
        .bind(run_id)
        .bind(&call.provider)
        .bind(&call.model)
        .bind(call.duration_ms as i64)
        .bind(call.input_tokens.map(|v| v as i64))
        .bind(call.output_tokens.map(|v| v as i64))
        .bind(call.cost)
        .bind(&call.error)
        .bind(now_ms())
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            tenant,
            run_id,
            "llm_call",
            serde_json::to_value(call)?,
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn usage(&self, tenant: &str, since_ms: i64) -> Result<Usage> {
        let r = sqlx::query(
            "SELECT \
               (SELECT count(*) FROM runs WHERE tenant = $1 AND created_at >= $2) AS runs, \
               (SELECT count(*) FROM llm_calls WHERE tenant = $1 AND created_at >= $2) AS llm_calls, \
               (SELECT coalesce(sum(input_tokens), 0) FROM llm_calls WHERE tenant = $1 AND created_at >= $2) AS input_tokens, \
               (SELECT coalesce(sum(output_tokens), 0) FROM llm_calls WHERE tenant = $1 AND created_at >= $2) AS output_tokens, \
               (SELECT coalesce(total(cost), 0.0) FROM llm_calls WHERE tenant = $1 AND created_at >= $2) AS cost, \
               (SELECT count(*) FROM fetches WHERE tenant = $1 AND created_at >= $2) AS fetches",
        )
        .bind(tenant)
        .bind(since_ms)
        .fetch_one(&self.pool)
        .await?;
        Ok(Usage {
            runs: r.try_get::<i64, _>("runs")? as u64,
            llm_calls: r.try_get::<i64, _>("llm_calls")? as u64,
            input_tokens: r.try_get::<i64, _>("input_tokens")? as u64,
            output_tokens: r.try_get::<i64, _>("output_tokens")? as u64,
            cost: r.try_get("cost")?,
            fetches: r.try_get::<i64, _>("fetches")? as u64,
        })
    }

    async fn record_fetch(&self, tenant: &str, fetch: &FetchRecord) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO fetches (tenant, url, status, bytes, duration_ms, error, created_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7)",
        )
        .bind(tenant)
        .bind(&fetch.url)
        .bind(fetch.status.map(i64::from))
        .bind(fetch.bytes as i64)
        .bind(fetch.duration_ms as i64)
        .bind(&fetch.error)
        .bind(now_ms())
        .execute(&mut *tx)
        .await?;
        self.audit(&mut tx, tenant, None, "fetch", serde_json::to_value(fetch)?)
            .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn record_tool_call(
        &self,
        tenant: &str,
        run_id: &str,
        call: &ToolCallRecord,
    ) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO tool_calls (tenant, run_id, tool, argument_keys, decision, \
             decision_reason, approval_id, duration_ms, output_bytes, error, created_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        )
        .bind(tenant)
        .bind(run_id)
        .bind(&call.tool)
        .bind(serde_json::to_string(&call.argument_keys)?)
        .bind(&call.decision)
        .bind(&call.decision_reason)
        .bind(&call.approval_id)
        .bind(call.duration_ms as i64)
        .bind(call.output_bytes.map(|v| v as i64))
        .bind(&call.error)
        .bind(now_ms())
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            tenant,
            Some(run_id),
            "tool_call",
            serde_json::to_value(call)?,
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn create_approval(&self, a: &Approval) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO approvals (id, tenant, run_id, tool, arguments, reason, status, created_at) \
             VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)",
        )
        .bind(&a.id)
        .bind(&a.tenant)
        .bind(&a.run_id)
        .bind(&a.tool)
        .bind(a.arguments.to_string())
        .bind(&a.reason)
        .bind(now_ms())
        .execute(&mut *tx)
        .await?;
        sqlx::query("UPDATE runs SET status = 'waiting_approval' WHERE id = $1")
            .bind(&a.run_id)
            .execute(&mut *tx)
            .await?;
        self.audit(
            &mut tx,
            &a.tenant,
            Some(&a.run_id),
            "approval_requested",
            json!({ "approval_id": a.id, "tool": a.tool, "reason": a.reason }),
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn decide_approval(
        &self,
        tenant: &str,
        id: &str,
        approve: bool,
        by: &str,
        note: Option<&str>,
    ) -> Result<Option<Approval>> {
        let status = if approve { "approved" } else { "denied" };
        let mut tx = self.pool.begin().await?;
        let row = sqlx::query(sqlx::AssertSqlSafe(format!(
            "UPDATE approvals SET status = $3, decided_by = $4, decision_note = $5, \
             decided_at = $6 WHERE id = $1 AND tenant = $2 AND status = 'pending' \
             RETURNING {APPROVAL_COLUMNS}"
        )))
        .bind(id)
        .bind(tenant)
        .bind(status)
        .bind(by)
        .bind(note)
        .bind(now_ms())
        .fetch_optional(&mut *tx)
        .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        let approval = Self::approval_from_row(&row)?;
        sqlx::query(
            "UPDATE runs SET status = 'running' WHERE id = $1 AND status = 'waiting_approval'",
        )
        .bind(&approval.run_id)
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            tenant,
            Some(&approval.run_id),
            "approval_decided",
            json!({ "approval_id": approval.id, "status": status, "by": by, "note": note }),
        )
        .await?;
        tx.commit().await?;
        Ok(Some(approval))
    }

    async fn expire_approval(&self, id: &str) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        let row: Option<(String, String)> = sqlx::query_as(
            "UPDATE approvals SET status = 'expired', decided_at = $2 \
             WHERE id = $1 AND status = 'pending' RETURNING tenant, run_id",
        )
        .bind(id)
        .bind(now_ms())
        .fetch_optional(&mut *tx)
        .await?;
        if let Some((tenant, run)) = row {
            self.audit(
                &mut tx,
                &tenant,
                Some(&run),
                "approval_expired",
                json!({ "approval_id": id }),
            )
            .await?;
        }
        tx.commit().await?;
        Ok(())
    }

    async fn get_approval(&self, tenant: &str, id: &str) -> Result<Option<Approval>> {
        sqlx::query(sqlx::AssertSqlSafe(format!(
            "SELECT {APPROVAL_COLUMNS} FROM approvals WHERE id = $1 AND tenant = $2"
        )))
        .bind(id)
        .bind(tenant)
        .fetch_optional(&self.pool)
        .await?
        .as_ref()
        .map(Self::approval_from_row)
        .transpose()
    }

    async fn recover_interrupted(&self, source_prefix: &str) -> Result<u64> {
        let now = now_ms();
        let stale = now - self.stale_after.as_millis() as i64;
        let mut tx = self.pool.begin().await?;
        let runs: Vec<(String, String)> = sqlx::query_as(
            "UPDATE runs SET status = 'failed', finished_at = $4, \
             error = 'interrupted: the service stopped before the run finished' \
             WHERE status IN ('queued', 'running', 'waiting_approval') \
             AND substr(source, 1, length($1)) = $1 \
             AND (instance IS NULL OR (instance <> $2 AND NOT EXISTS ( \
                 SELECT 1 FROM instances i WHERE i.id = runs.instance AND i.seen_at >= $3))) \
             RETURNING id, tenant",
        )
        .bind(source_prefix)
        .bind(&self.instance)
        .bind(stale)
        .bind(now)
        .fetch_all(&mut *tx)
        .await?;
        for (id, tenant) in &runs {
            sqlx::query(
                "UPDATE approvals SET status = 'expired', decided_at = $2 \
                 WHERE status = 'pending' AND run_id = $1",
            )
            .bind(id)
            .bind(now)
            .execute(&mut *tx)
            .await?;
            self.audit(
                &mut tx,
                tenant,
                Some(id),
                "run_interrupted",
                json!({ "reason": "service restart" }),
            )
            .await?;
        }
        // Processes gone long ago are forgotten.
        sqlx::query("DELETE FROM instances WHERE seen_at < $1 AND id <> $2")
            .bind(stale)
            .bind(&self.instance)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(runs.len() as u64)
    }

    async fn tenant_limits(&self, tenant: &str) -> Result<Option<TenantLimits>> {
        let row = sqlx::query("SELECT limits FROM tenant_limits WHERE tenant = $1")
            .bind(tenant)
            .fetch_optional(&self.pool)
            .await?;
        row.map(|r| Ok(serde_json::from_str(&r.try_get::<String, _>("limits")?)?))
            .transpose()
    }

    async fn set_tenant_limits(&self, tenant: &str, limits: &TenantLimits, by: &str) -> Result<()> {
        let value = serde_json::to_value(limits)?;
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO tenant_limits (tenant, limits, updated_at) VALUES ($1, $2, $3) \
             ON CONFLICT (tenant) DO UPDATE SET limits = excluded.limits, updated_at = excluded.updated_at",
        )
        .bind(tenant)
        .bind(value.to_string())
        .bind(now_ms())
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            tenant,
            None,
            "tenant_limits",
            json!({ "limits": value, "by": by }),
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn list_approvals(
        &self,
        tenant: &str,
        status: Option<ApprovalStatus>,
    ) -> Result<Vec<Approval>> {
        sqlx::query(sqlx::AssertSqlSafe(format!(
            "SELECT {APPROVAL_COLUMNS} FROM approvals WHERE tenant = $1 \
             AND ($2 IS NULL OR status = $2) ORDER BY created_at DESC, rowid DESC LIMIT 200"
        )))
        .bind(tenant)
        .bind(status.map(|s| s.as_str()))
        .fetch_all(&self.pool)
        .await?
        .iter()
        .map(Self::approval_from_row)
        .collect()
    }
}
