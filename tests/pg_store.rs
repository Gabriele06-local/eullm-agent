//! PostgreSQL store. Needs `DATABASE_URL` pointing at a disposable
//! database (CI starts one); without it these tests do nothing and say so.

mod common;

use common::store_suite::{self, new_run, tenant};
use common::{tool_call, CountingTool, Scripted};
use serde_json::json;
use std::sync::Arc;
use std::time::Duration;

use eullm_agent::config::Config;
use eullm_agent::policy::Policy;
use eullm_agent::router::ModelRouter;
use eullm_agent::service::{Core, StartRun};
use eullm_agent::store::*;
use eullm_agent::tools::ToolRegistry;

async fn store() -> Option<PgStore> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("DATABASE_URL not set: PostgreSQL tests not run");
        return None;
    };
    Some(PgStore::connect(&url, 5).await.expect("connect"))
}

#[tokio::test]
async fn run_lifecycle_with_steps() {
    let Some(s) = store().await else { return };
    store_suite::run_lifecycle_with_steps(Arc::new(s)).await;
}

#[tokio::test]
async fn tenants_cannot_read_each_other() {
    let Some(s) = store().await else { return };
    store_suite::tenants_cannot_read_each_other(Arc::new(s)).await;
}

#[tokio::test]
async fn approvals_are_decided_once_and_scoped() {
    let Some(s) = store().await else { return };
    store_suite::approvals_are_decided_once_and_scoped(Arc::new(s)).await;
}

#[tokio::test]
async fn unanswered_approval_expires() {
    let Some(s) = store().await else { return };
    store_suite::unanswered_approval_expires(Arc::new(s)).await;
}

#[tokio::test]
async fn a_decision_taken_elsewhere_reaches_the_waiting_run() {
    let Some(s) = store().await else { return };
    store_suite::a_decision_taken_elsewhere_reaches_the_waiting_run(Arc::new(s)).await;
}

#[tokio::test]
async fn runs_left_unfinished_are_closed_at_startup() {
    let Some(s) = store().await else { return };
    let t = tenant("recover");
    let mut api_run = new_run(&t);
    api_run.source = "api:test".into();
    let mut tg_run = new_run(&t);
    tg_run.source = "telegram:42".into();
    s.create_run(&api_run).await.unwrap();
    s.create_run(&tg_run).await.unwrap();
    s.set_run_status(&api_run.id, RunStatus::WaitingApproval)
        .await
        .unwrap();
    let approval = Approval {
        id: uuid::Uuid::new_v4().to_string(),
        tenant: t.clone(),
        run_id: api_run.id.clone(),
        tool: "write_file".into(),
        arguments: json!({}),
        reason: "test".into(),
        status: ApprovalStatus::Pending,
        decided_by: None,
        decision_note: None,
        created_at_ms: now_ms(),
        decided_at_ms: None,
    };
    s.create_approval(&approval).await.unwrap();
    // A second process starting now leaves the runs of the live one alone.
    let other = store().await.unwrap();
    other.recover_interrupted("api:").await.unwrap();
    let api = s.get_run(&t, &api_run.id).await.unwrap().unwrap();
    assert_eq!(api.summary.status, RunStatus::WaitingApproval);
    // Once that process is gone, its runs are closed.
    drop(s);
    let s = other;
    let mut closed = 0;
    for _ in 0..50 {
        // the server notices the closed connection a moment later
        closed = s.recover_interrupted("api:").await.unwrap();
        if closed >= 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(closed >= 1);
    let api = s.get_run(&t, &api_run.id).await.unwrap().unwrap();
    assert_eq!(api.summary.status, RunStatus::Failed);
    assert!(api.error.unwrap().contains("interrupted"));
    let a = s.get_approval(&t, &approval.id).await.unwrap().unwrap();
    assert_eq!(a.status, ApprovalStatus::Expired);
    // Telegram runs belong to the other process.
    let tg = s.get_run(&t, &tg_run.id).await.unwrap().unwrap();
    assert_eq!(tg.summary.status, RunStatus::Queued);
}

#[tokio::test]
async fn expire_approval_tolerates_malformed_id() {
    let Some(s) = store().await else { return };
    store_suite::expire_approval_tolerates_malformed_id(Arc::new(s)).await;
}

#[tokio::test]
async fn audit_events_are_append_only() {
    let Some(s) = store().await else { return };
    let t = tenant("audit");
    let run = new_run(&t);
    s.create_run(&run).await.unwrap();
    let pool = s.pool();
    let n: i64 = sqlx::query_scalar("SELECT count(*) FROM core.audit_events WHERE tenant = $1")
        .bind(&t)
        .fetch_one(pool)
        .await
        .unwrap();
    assert_eq!(n, 1);
    let upd = sqlx::query("UPDATE core.audit_events SET event = 'x' WHERE tenant = $1")
        .bind(&t)
        .execute(pool)
        .await;
    assert!(upd.is_err(), "update must be refused");
    let del = sqlx::query("DELETE FROM core.audit_events WHERE tenant = $1")
        .bind(&t)
        .execute(pool)
        .await;
    assert!(del.is_err(), "delete must be refused");
}

#[tokio::test]
async fn every_model_and_tool_call_of_a_run_has_a_row() {
    let Some(pg) = store().await else { return };
    let t = tenant("rows");
    let config = Arc::new(Config::from_yaml("provider:\n  type: eullm\n  model: test\n").unwrap());
    let llm = Arc::new(Scripted::new(vec![
        tool_call("one", json!({"a": 1})),
        tool_call("two", json!({"b": 2})),
    ]));
    let tools = ToolRegistry::new();
    tools.register(CountingTool::new("one", "1").0);
    tools.register(CountingTool::new("two", "2").0);
    let core = Core::new(
        config,
        ModelRouter::single("default", llm),
        tools,
        Arc::new(Policy::default()),
        Arc::new(pg.clone()),
        Duration::from_secs(5),
        1,
        None,
    );
    let (id, result) = core
        .run_now(StartRun {
            tenant: t.clone(),
            profile: "default".into(),
            input: "go".into(),
            source: "test".into(),
            notify: None,
        })
        .await
        .unwrap();
    assert_eq!(result.unwrap(), "done");
    let llm_rows: i64 =
        sqlx::query_scalar("SELECT count(*) FROM core.llm_calls WHERE run_id = $1::uuid")
            .bind(&id)
            .fetch_one(pg.pool())
            .await
            .unwrap();
    let tool_rows: i64 =
        sqlx::query_scalar("SELECT count(*) FROM core.tool_calls WHERE run_id = $1::uuid")
            .bind(&id)
            .fetch_one(pg.pool())
            .await
            .unwrap();
    assert_eq!((llm_rows, tool_rows), (3, 2));
    let events: Vec<String> = sqlx::query_scalar(
        "SELECT event FROM core.audit_events WHERE run_id = $1::uuid ORDER BY id",
    )
    .bind(&id)
    .fetch_all(pg.pool())
    .await
    .unwrap();
    assert_eq!(
        events,
        [
            "run_created",
            "llm_call",
            "tool_call",
            "llm_call",
            "tool_call",
            "llm_call",
            "run_finished"
        ]
    );
}

#[tokio::test]
async fn fetches_are_recorded_with_an_audit_event() {
    let Some(s) = store().await else { return };
    let t = tenant("fetch");
    s.record_fetch(
        &t,
        &FetchRecord {
            url: "https://example.com/feed".into(),
            status: Some(200),
            bytes: 1234,
            duration_ms: 50,
            error: None,
        },
    )
    .await
    .unwrap();
    let pool = s.pool();
    let (url, status): (String, Option<i32>) =
        sqlx::query_as("SELECT url, status FROM core.fetches WHERE tenant = $1")
            .bind(&t)
            .fetch_one(pool)
            .await
            .unwrap();
    assert_eq!(
        (url.as_str(), status),
        ("https://example.com/feed", Some(200))
    );
    let events: Vec<String> =
        sqlx::query_scalar("SELECT event FROM core.audit_events WHERE tenant = $1")
            .bind(&t)
            .fetch_all(pool)
            .await
            .unwrap();
    assert_eq!(events, ["fetch"]);
}

#[tokio::test]
async fn usage_counts_one_tenant_since_a_time() {
    let Some(s) = store().await else { return };
    store_suite::usage_counts_one_tenant_since_a_time(Arc::new(s)).await;
}

#[tokio::test]
async fn tenant_limits_are_stored_and_replaced() {
    let Some(s) = store().await else { return };
    store_suite::tenant_limits_are_stored_and_replaced(Arc::new(s)).await;
}
