//! PostgreSQL store. Needs `DATABASE_URL` pointing at a disposable
//! database (CI starts one); without it these tests do nothing and say so.

mod common;

use common::{tool_call, CountingTool, Scripted};
use serde_json::json;
use std::sync::Arc;
use std::time::Duration;

use eullm_agent::approvals::{ApprovalQueue, ApprovalRequest, Outcome};
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

fn tenant(name: &str) -> String {
    format!("{name}-{}", uuid::Uuid::new_v4().simple())
}

fn new_run(tenant: &str) -> NewRun {
    NewRun {
        id: uuid::Uuid::new_v4().to_string(),
        tenant: tenant.into(),
        profile: "default".into(),
        source: "test".into(),
        input: "task".into(),
    }
}

#[tokio::test]
async fn run_lifecycle_with_steps() {
    let Some(s) = store().await else { return };
    let t = tenant("life");
    let run = new_run(&t);
    s.create_run(&run).await.unwrap();
    s.set_run_status(&run.id, RunStatus::Running).await.unwrap();
    s.record_llm_call(
        &t,
        Some(&run.id),
        &LlmCallRecord {
            provider: "eullm".into(),
            model: "m".into(),
            duration_ms: 12,
            input_tokens: Some(100),
            output_tokens: Some(7),
            cost: Some(0.01),
            error: None,
        },
    )
    .await
    .unwrap();
    s.record_tool_call(
        &t,
        &run.id,
        &ToolCallRecord {
            tool: "read_file".into(),
            argument_keys: vec!["path".into()],
            decision: "allow".into(),
            decision_reason: None,
            approval_id: None,
            duration_ms: 3,
            output_bytes: Some(42),
            error: None,
        },
    )
    .await
    .unwrap();
    s.finish_run(
        &run.id,
        &RunEnd {
            iterations: 2,
            input_tokens: 100,
            output_tokens: 7,
            cost: Some(0.01),
            tainted: true,
            output: Some("answer".into()),
            error: None,
        },
    )
    .await
    .unwrap();

    let v = s.get_run(&t, &run.id).await.unwrap().unwrap();
    assert_eq!(v.summary.status, RunStatus::Succeeded);
    assert_eq!(v.output.as_deref(), Some("answer"));
    assert!(v.tainted);
    assert_eq!(v.steps.len(), 2);
    assert!(matches!(v.steps[0], Step::LlmCall { .. }));
    assert!(v.summary.finished_at_ms.is_some());
    assert_eq!(s.list_runs(&t, 10).await.unwrap().len(), 1);
}

#[tokio::test]
async fn tenants_cannot_read_each_other() {
    let Some(s) = store().await else { return };
    let (a, b) = (tenant("a"), tenant("b"));
    let run = new_run(&a);
    s.create_run(&run).await.unwrap();
    assert!(s.get_run(&b, &run.id).await.unwrap().is_none());
    assert!(s.list_runs(&b, 10).await.unwrap().is_empty());
    assert!(s.get_run(&a, "not-a-uuid").await.unwrap().is_none());
}

#[tokio::test]
async fn approvals_are_decided_once_and_scoped() {
    let Some(s) = store().await else { return };
    let s: Arc<dyn Store> = Arc::new(s);
    let t = tenant("appr");
    let run = new_run(&t);
    s.create_run(&run).await.unwrap();
    let queue = ApprovalQueue::new(Arc::clone(&s), Duration::from_secs(5));

    let q = Arc::clone(&queue);
    let req = ApprovalRequest {
        tenant: t.clone(),
        run_id: run.id.clone(),
        tool: "write_file".into(),
        arguments: json!({"path": "x", "content": "y"}),
        reason: "test".into(),
        run_deadline: None,
    };
    let waiter = tokio::spawn(async move { q.request_with(req, None).await });

    let pending = loop {
        let p = s
            .list_approvals(&t, Some(ApprovalStatus::Pending))
            .await
            .unwrap();
        if let Some(a) = p.into_iter().next() {
            break a;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    };
    assert_eq!(
        s.get_run(&t, &run.id)
            .await
            .unwrap()
            .unwrap()
            .summary
            .status,
        RunStatus::WaitingApproval
    );
    assert!(queue
        .decide("other-tenant", &pending.id, true, "x", None)
        .await
        .unwrap()
        .is_none());
    let decided = queue
        .decide(&t, &pending.id, true, "francesco", Some("ok"))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(decided.status, ApprovalStatus::Approved);
    assert!(queue
        .decide(&t, &pending.id, false, "x", None)
        .await
        .unwrap()
        .is_none());
    let (_, outcome) = waiter.await.unwrap().unwrap();
    assert_eq!(
        outcome,
        Outcome::Approved {
            by: "francesco".into()
        }
    );
    assert_eq!(
        s.get_run(&t, &run.id)
            .await
            .unwrap()
            .unwrap()
            .summary
            .status,
        RunStatus::Running
    );
}

#[tokio::test]
async fn unanswered_approval_expires() {
    let Some(s) = store().await else { return };
    let s: Arc<dyn Store> = Arc::new(s);
    let t = tenant("exp");
    let run = new_run(&t);
    s.create_run(&run).await.unwrap();
    let queue = ApprovalQueue::new(Arc::clone(&s), Duration::from_millis(200));
    let (id, outcome) = queue
        .request_with(
            ApprovalRequest {
                tenant: t.clone(),
                run_id: run.id.clone(),
                tool: "run_program".into(),
                arguments: json!({}),
                reason: "test".into(),
                run_deadline: None,
            },
            None,
        )
        .await
        .unwrap();
    assert_eq!(outcome, Outcome::Expired);
    let list = s.list_approvals(&t, None).await.unwrap();
    assert_eq!(list[0].id, id.unwrap());
    assert_eq!(list[0].status, ApprovalStatus::Expired);
}

#[tokio::test]
async fn a_decision_taken_elsewhere_reaches_the_waiting_run() {
    let Some(s) = store().await else { return };
    let s: Arc<dyn Store> = Arc::new(s);
    let t = tenant("other");
    let run = new_run(&t);
    s.create_run(&run).await.unwrap();
    // Two Core processes on one database: the run waits in one, the
    // decision is taken through the other.
    let here = ApprovalQueue::new(Arc::clone(&s), Duration::from_secs(10));
    let there = ApprovalQueue::new(Arc::clone(&s), Duration::from_secs(10));
    let q = Arc::clone(&here);
    let req = ApprovalRequest {
        tenant: t.clone(),
        run_id: run.id.clone(),
        tool: "write_file".into(),
        arguments: json!({}),
        reason: "test".into(),
        run_deadline: None,
    };
    let waiter = tokio::spawn(async move { q.request_with(req, None).await });
    let pending = loop {
        if let Some(a) = s
            .list_approvals(&t, Some(ApprovalStatus::Pending))
            .await
            .unwrap()
            .into_iter()
            .next()
        {
            break a;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    };
    there
        .decide(&t, &pending.id, false, "francesco", Some("no"))
        .await
        .unwrap()
        .unwrap();
    let (_, outcome) = tokio::time::timeout(Duration::from_secs(5), waiter)
        .await
        .expect("the waiting run was not woken")
        .unwrap()
        .unwrap();
    assert_eq!(
        outcome,
        Outcome::Denied {
            by: "francesco".into(),
            note: Some("no".into())
        }
    );
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
    assert!(s.recover_interrupted("api:").await.unwrap() >= 1);
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
    let s: Arc<dyn Store> = Arc::new(s);
    s.expire_approval("not-a-uuid").await.unwrap();
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
    let (t, other) = (tenant("usage"), tenant("usage-other"));
    let since = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
        - 60_000;
    let run = new_run(&t);
    s.create_run(&run).await.unwrap();
    s.create_run(&new_run(&other)).await.unwrap();
    let call = LlmCallRecord {
        provider: "eullm".into(),
        model: "m".into(),
        duration_ms: 1,
        input_tokens: Some(100),
        output_tokens: Some(20),
        cost: Some(0.5),
        error: None,
    };
    s.record_llm_call(&t, Some(&run.id), &call).await.unwrap();
    s.record_llm_call(&t, None, &call).await.unwrap();
    s.record_llm_call(&other, None, &call).await.unwrap();
    s.record_fetch(
        &t,
        &FetchRecord {
            url: "https://example.com/".into(),
            status: Some(200),
            bytes: 1,
            duration_ms: 1,
            error: None,
        },
    )
    .await
    .unwrap();

    let u = s.usage(&t, since).await.unwrap();
    assert_eq!(
        u,
        Usage {
            runs: 1,
            llm_calls: 2,
            input_tokens: 200,
            output_tokens: 40,
            cost: 1.0,
            fetches: 1,
        }
    );
    let later = since + 3_600_000;
    assert_eq!(s.usage(&t, later).await.unwrap(), Usage::default());
}
