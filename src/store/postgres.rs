use anyhow::{Context, Result};
use async_trait::async_trait;
use serde_json::{json, Value};
use sqlx::postgres::{PgPool, PgPoolOptions, PgRow};
use sqlx::Row;
use std::sync::Arc;
use uuid::Uuid;

use super::*;

/// Run state and audit in the `core` schema of PostgreSQL.
#[derive(Clone)]
pub struct PgStore {
    pool: PgPool,
    /// This process, as written on its runs; alive while `lock` holds the
    /// advisory lock (`INSTANCE_LOCKS`, instance).
    instance: i32,
    _lock: Arc<std::sync::Mutex<sqlx::PgConnection>>,
}

/// First key of the advisory locks that mark live processes.
const INSTANCE_LOCKS: i32 = 0x4575_4c4c;

static MIGRATOR: sqlx::migrate::Migrator = sqlx::migrate!("./migrations");

fn uuid(id: &str) -> Result<Uuid> {
    Uuid::parse_str(id).with_context(|| format!("invalid id '{id}'"))
}

/// Built only from constants, so the formatted queries carry no input.
const MS: &str = "(extract(epoch from {col}) * 1000)::bigint";

fn ms(col: &str) -> String {
    MS.replace("{col}", col)
}

impl PgStore {
    /// Connect and apply pending migrations.
    pub async fn connect(url: &str, max_connections: u32) -> Result<Self> {
        let pool = PgPoolOptions::new()
            .max_connections(max_connections.max(1))
            .acquire_timeout(std::time::Duration::from_secs(10))
            .connect(url)
            .await
            .context("cannot connect to PostgreSQL")?;
        // The migration history lives in schema public. Unqualified, it would
        // follow the search path: for a role named `core`, "$user" is schema
        // `core` once the first migration has created it, and the next start
        // would find no history there and apply everything again.
        let mut conn = pool.acquire().await?;
        sqlx::query("SET search_path TO public")
            .execute(&mut *conn)
            .await?;
        MIGRATOR
            .run(&mut *conn)
            .await
            .context("database migration failed")?;
        drop(conn.detach());
        // A connection of its own holds the lock for the life of the
        // process; when the process dies the lock goes with it.
        let mut conn = pool.acquire().await?.detach();
        let instance = loop {
            let bytes = Uuid::new_v4().into_bytes();
            let candidate =
                (u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]) >> 1) as i32;
            let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock($1, $2)")
                .bind(INSTANCE_LOCKS)
                .bind(candidate)
                .fetch_one(&mut conn)
                .await?;
            if locked {
                break candidate;
            }
        };
        Ok(Self {
            pool,
            instance,
            _lock: Arc::new(std::sync::Mutex::new(conn)),
        })
    }

    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    async fn audit(
        &self,
        tx: &mut sqlx::PgConnection,
        tenant: &str,
        run_id: Option<Uuid>,
        event: &str,
        payload: Value,
    ) -> Result<()> {
        sqlx::query(
            "INSERT INTO core.audit_events (tenant, run_id, event, payload) VALUES ($1, $2, $3, $4)",
        )
        .bind(tenant)
        .bind(run_id)
        .bind(event)
        .bind(payload)
        .execute(tx)
        .await?;
        Ok(())
    }

    fn approval_from_row(r: &PgRow) -> Result<Approval> {
        Ok(Approval {
            id: r.try_get::<Uuid, _>("id")?.to_string(),
            tenant: r.try_get("tenant")?,
            run_id: r.try_get::<Uuid, _>("run_id")?.to_string(),
            tool: r.try_get("tool")?,
            arguments: r.try_get("arguments")?,
            reason: r.try_get("reason")?,
            status: ApprovalStatus::parse(r.try_get::<&str, _>("status")?)
                .unwrap_or(ApprovalStatus::Expired),
            decided_by: r.try_get("decided_by")?,
            decision_note: r.try_get("decision_note")?,
            created_at_ms: r.try_get("created_at_ms")?,
            decided_at_ms: r.try_get("decided_at_ms")?,
        })
    }

    fn approval_columns() -> String {
        format!(
            "id, tenant, run_id, tool, arguments, reason, status, decided_by, decision_note, \
             {} AS created_at_ms, {} AS decided_at_ms",
            ms("created_at"),
            ms("decided_at")
        )
    }

    fn summary_from_row(r: &PgRow) -> Result<RunSummary> {
        Ok(RunSummary {
            id: r.try_get::<Uuid, _>("id")?.to_string(),
            tenant: r.try_get("tenant")?,
            profile: r.try_get("profile")?,
            source: r.try_get("source")?,
            status: RunStatus::parse(r.try_get::<&str, _>("status")?),
            created_at_ms: r.try_get("created_at_ms")?,
            finished_at_ms: r.try_get("finished_at_ms")?,
        })
    }
}

#[async_trait]
impl Store for PgStore {
    async fn create_run(&self, run: &NewRun) -> Result<()> {
        let id = uuid(&run.id)?;
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO core.runs (id, tenant, profile, source, status, input, instance) \
             VALUES ($1, $2, $3, $4, 'queued', $5, $6)",
        )
        .bind(id)
        .bind(&run.tenant)
        .bind(&run.profile)
        .bind(&run.source)
        .bind(&run.input)
        .bind(self.instance)
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            &run.tenant,
            Some(id),
            "run_created",
            json!({ "profile": run.profile, "source": run.source }),
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn set_run_status(&self, id: &str, status: RunStatus) -> Result<()> {
        sqlx::query("UPDATE core.runs SET status = $2 WHERE id = $1")
            .bind(uuid(id)?)
            .bind(status.as_str())
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    async fn finish_run(&self, id: &str, end: &RunEnd) -> Result<()> {
        let id = uuid(id)?;
        let status = if end.error.is_some() {
            RunStatus::Failed
        } else {
            RunStatus::Succeeded
        };
        let mut tx = self.pool.begin().await?;
        let tenant: Option<String> = sqlx::query_scalar(
            "UPDATE core.runs SET status = $2, output = $3, error = $4, iterations = $5, \
             input_tokens = $6, output_tokens = $7, cost = $8, tainted = $9, finished_at = now() \
             WHERE id = $1 RETURNING tenant",
        )
        .bind(id)
        .bind(status.as_str())
        .bind(&end.output)
        .bind(&end.error)
        .bind(end.iterations as i32)
        .bind(end.input_tokens as i64)
        .bind(end.output_tokens as i64)
        .bind(end.cost)
        .bind(end.tainted)
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
        let Ok(id) = Uuid::parse_str(id) else {
            return Ok(None);
        };
        let q = format!(
            "SELECT id, tenant, profile, source, status, input, output, error, iterations, \
             input_tokens, output_tokens, cost, tainted, {} AS created_at_ms, {} AS finished_at_ms \
             FROM core.runs WHERE id = $1 AND tenant = $2",
            ms("created_at"),
            ms("finished_at")
        );
        let Some(r) = sqlx::query(sqlx::AssertSqlSafe(q))
            .bind(id)
            .bind(tenant)
            .fetch_optional(&self.pool)
            .await?
        else {
            return Ok(None);
        };

        let mut steps: Vec<(i64, Step)> = Vec::new();
        let q = format!(
            "SELECT provider, model, duration_ms, input_tokens, output_tokens, cost, error, \
             {} AS at_ms FROM core.llm_calls WHERE run_id = $1 ORDER BY id",
            ms("created_at")
        );
        for row in sqlx::query(sqlx::AssertSqlSafe(q))
            .bind(id)
            .fetch_all(&self.pool)
            .await?
        {
            let at_ms: i64 = row.try_get("at_ms")?;
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
        let q = format!(
            "SELECT tool, argument_keys, decision, decision_reason, approval_id, duration_ms, \
             output_bytes, error, {} AS at_ms FROM core.tool_calls WHERE run_id = $1 ORDER BY id",
            ms("created_at")
        );
        for row in sqlx::query(sqlx::AssertSqlSafe(q))
            .bind(id)
            .fetch_all(&self.pool)
            .await?
        {
            let at_ms: i64 = row.try_get("at_ms")?;
            steps.push((
                at_ms,
                Step::ToolCall {
                    at_ms,
                    call: ToolCallRecord {
                        tool: row.try_get("tool")?,
                        argument_keys: row.try_get("argument_keys")?,
                        decision: row.try_get("decision")?,
                        decision_reason: row.try_get("decision_reason")?,
                        approval_id: row
                            .try_get::<Option<Uuid>, _>("approval_id")?
                            .map(|u| u.to_string()),
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
            iterations: r.try_get::<i32, _>("iterations")? as u64,
            input_tokens: r.try_get::<i64, _>("input_tokens")? as u64,
            output_tokens: r.try_get::<i64, _>("output_tokens")? as u64,
            cost: r.try_get("cost")?,
            tainted: r.try_get("tainted")?,
            steps: steps.into_iter().map(|(_, s)| s).collect(),
        }))
    }

    async fn list_runs(&self, tenant: &str, limit: u32) -> Result<Vec<RunSummary>> {
        let q = format!(
            "SELECT id, tenant, profile, source, status, {} AS created_at_ms, \
             {} AS finished_at_ms FROM core.runs WHERE tenant = $1 \
             ORDER BY created_at DESC LIMIT $2",
            ms("created_at"),
            ms("finished_at")
        );
        sqlx::query(sqlx::AssertSqlSafe(q))
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
        let run = run_id.map(uuid).transpose()?;
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO core.llm_calls (tenant, run_id, provider, model, duration_ms, \
             input_tokens, output_tokens, cost, error) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        )
        .bind(tenant)
        .bind(run)
        .bind(&call.provider)
        .bind(&call.model)
        .bind(call.duration_ms as i64)
        .bind(call.input_tokens.map(|v| v as i64))
        .bind(call.output_tokens.map(|v| v as i64))
        .bind(call.cost)
        .bind(&call.error)
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            tenant,
            run,
            "llm_call",
            serde_json::to_value(call)?,
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn usage(&self, tenant: &str, since_ms: i64) -> Result<Usage> {
        let since = "to_timestamp($2::double precision / 1000)";
        let q = format!(
            "SELECT \
               (SELECT count(*) FROM core.runs WHERE tenant = $1 AND created_at >= {since}) AS runs, \
               (SELECT count(*) FROM core.llm_calls WHERE tenant = $1 AND created_at >= {since}) AS llm_calls, \
               (SELECT coalesce(sum(input_tokens), 0)::bigint FROM core.llm_calls WHERE tenant = $1 AND created_at >= {since}) AS input_tokens, \
               (SELECT coalesce(sum(output_tokens), 0)::bigint FROM core.llm_calls WHERE tenant = $1 AND created_at >= {since}) AS output_tokens, \
               (SELECT coalesce(sum(cost), 0)::double precision FROM core.llm_calls WHERE tenant = $1 AND created_at >= {since}) AS cost, \
               (SELECT count(*) FROM core.fetches WHERE tenant = $1 AND created_at >= {since}) AS fetches"
        );
        let r = sqlx::query(sqlx::AssertSqlSafe(q))
            .bind(tenant)
            .bind(since_ms as f64)
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
            "INSERT INTO core.fetches (tenant, url, status, bytes, duration_ms, error) \
             VALUES ($1, $2, $3, $4, $5, $6)",
        )
        .bind(tenant)
        .bind(&fetch.url)
        .bind(fetch.status.map(i32::from))
        .bind(fetch.bytes as i64)
        .bind(fetch.duration_ms as i64)
        .bind(&fetch.error)
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
        let run = uuid(run_id)?;
        let approval = call.approval_id.as_deref().map(uuid).transpose()?;
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO core.tool_calls (tenant, run_id, tool, argument_keys, decision, \
             decision_reason, approval_id, duration_ms, output_bytes, error) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        )
        .bind(tenant)
        .bind(run)
        .bind(&call.tool)
        .bind(&call.argument_keys)
        .bind(&call.decision)
        .bind(&call.decision_reason)
        .bind(approval)
        .bind(call.duration_ms as i64)
        .bind(call.output_bytes.map(|v| v as i64))
        .bind(&call.error)
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            tenant,
            Some(run),
            "tool_call",
            serde_json::to_value(call)?,
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn create_approval(&self, a: &Approval) -> Result<()> {
        let id = uuid(&a.id)?;
        let run = uuid(&a.run_id)?;
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO core.approvals (id, tenant, run_id, tool, arguments, reason, status) \
             VALUES ($1, $2, $3, $4, $5, $6, 'pending')",
        )
        .bind(id)
        .bind(&a.tenant)
        .bind(run)
        .bind(&a.tool)
        .bind(&a.arguments)
        .bind(&a.reason)
        .execute(&mut *tx)
        .await?;
        sqlx::query("UPDATE core.runs SET status = 'waiting_approval' WHERE id = $1")
            .bind(run)
            .execute(&mut *tx)
            .await?;
        self.audit(
            &mut tx,
            &a.tenant,
            Some(run),
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
        let Ok(id) = Uuid::parse_str(id) else {
            return Ok(None);
        };
        let status = if approve { "approved" } else { "denied" };
        let mut tx = self.pool.begin().await?;
        let q = format!(
            "UPDATE core.approvals SET status = $3, decided_by = $4, decision_note = $5, \
             decided_at = now() WHERE id = $1 AND tenant = $2 AND status = 'pending' \
             RETURNING {}",
            Self::approval_columns()
        );
        let row = sqlx::query(sqlx::AssertSqlSafe(q))
            .bind(id)
            .bind(tenant)
            .bind(status)
            .bind(by)
            .bind(note)
            .fetch_optional(&mut *tx)
            .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        let approval = Self::approval_from_row(&row)?;
        sqlx::query(
            "UPDATE core.runs SET status = 'running' WHERE id = $1 AND status = 'waiting_approval'",
        )
        .bind(uuid(&approval.run_id)?)
        .execute(&mut *tx)
        .await?;
        self.audit(
            &mut tx,
            tenant,
            Some(uuid(&approval.run_id)?),
            "approval_decided",
            json!({ "approval_id": approval.id, "status": status, "by": by, "note": note }),
        )
        .await?;
        tx.commit().await?;
        Ok(Some(approval))
    }

    async fn expire_approval(&self, id: &str) -> Result<()> {
        let Ok(id) = Uuid::parse_str(id) else {
            return Ok(());
        };
        let mut tx = self.pool.begin().await?;
        let row: Option<(String, Uuid)> = sqlx::query_as(
            "UPDATE core.approvals SET status = 'expired', decided_at = now() \
             WHERE id = $1 AND status = 'pending' RETURNING tenant, run_id",
        )
        .bind(id)
        .fetch_optional(&mut *tx)
        .await?;
        if let Some((tenant, run)) = row {
            self.audit(
                &mut tx,
                &tenant,
                Some(run),
                "approval_expired",
                json!({ "approval_id": id.to_string() }),
            )
            .await?;
        }
        tx.commit().await?;
        Ok(())
    }

    async fn get_approval(&self, tenant: &str, id: &str) -> Result<Option<Approval>> {
        let Ok(id) = Uuid::parse_str(id) else {
            return Ok(None);
        };
        let q = format!(
            "SELECT {} FROM core.approvals WHERE id = $1 AND tenant = $2",
            Self::approval_columns()
        );
        sqlx::query(sqlx::AssertSqlSafe(q))
            .bind(id)
            .bind(tenant)
            .fetch_optional(&self.pool)
            .await?
            .as_ref()
            .map(Self::approval_from_row)
            .transpose()
    }

    async fn recover_interrupted(&self, source_prefix: &str) -> Result<u64> {
        let mut tx = self.pool.begin().await?;
        let runs: Vec<(Uuid, String)> = sqlx::query_as(
            "UPDATE core.runs SET status = 'failed', finished_at = now(), \
             error = 'interrupted: the service stopped before the run finished' \
             WHERE status IN ('queued', 'running', 'waiting_approval') \
             AND starts_with(source, $1) \
             AND (instance IS NULL OR (instance <> $2 AND NOT EXISTS ( \
                 SELECT 1 FROM pg_locks l WHERE l.locktype = 'advisory' AND l.granted \
                 AND l.classid = $3::oid AND l.objid = runs.instance::oid AND l.objsubid = 2))) \
             RETURNING id, tenant",
        )
        .bind(source_prefix)
        .bind(self.instance)
        .bind(INSTANCE_LOCKS)
        .fetch_all(&mut *tx)
        .await?;
        let ids: Vec<Uuid> = runs.iter().map(|(id, _)| *id).collect();
        sqlx::query(
            "UPDATE core.approvals SET status = 'expired', decided_at = now() \
             WHERE status = 'pending' AND run_id = ANY($1)",
        )
        .bind(&ids)
        .execute(&mut *tx)
        .await?;
        for (id, tenant) in &runs {
            self.audit(
                &mut tx,
                tenant,
                Some(*id),
                "run_interrupted",
                json!({ "reason": "service restart" }),
            )
            .await?;
        }
        tx.commit().await?;
        Ok(runs.len() as u64)
    }

    async fn tenant_limits(&self, tenant: &str) -> Result<Option<TenantLimits>> {
        let row = sqlx::query("SELECT limits FROM core.tenant_limits WHERE tenant = $1")
            .bind(tenant)
            .fetch_optional(&self.pool)
            .await?;
        row.map(|r| Ok(serde_json::from_value(r.try_get("limits")?)?))
            .transpose()
    }

    async fn set_tenant_limits(&self, tenant: &str, limits: &TenantLimits, by: &str) -> Result<()> {
        let value = serde_json::to_value(limits)?;
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO core.tenant_limits (tenant, limits) VALUES ($1, $2) \
             ON CONFLICT (tenant) DO UPDATE SET limits = EXCLUDED.limits, updated_at = now()",
        )
        .bind(tenant)
        .bind(&value)
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
        let q = format!(
            "SELECT {} FROM core.approvals WHERE tenant = $1 AND ($2::text IS NULL OR status = $2) \
             ORDER BY created_at DESC LIMIT 200",
            Self::approval_columns()
        );
        sqlx::query(sqlx::AssertSqlSafe(q))
            .bind(tenant)
            .bind(status.map(|s| s.as_str()))
            .fetch_all(&self.pool)
            .await?
            .iter()
            .map(Self::approval_from_row)
            .collect()
    }
}
