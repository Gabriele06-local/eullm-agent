//! Behaviour every store must share, run against each backend by its own
//! test file (`pg_store.rs`, `sqlite_store.rs`).

use serde_json::json;
use std::sync::Arc;
use std::time::Duration;

use eullm_agent::approvals::{ApprovalQueue, ApprovalRequest, Outcome};
use eullm_agent::store::*;

pub fn tenant(name: &str) -> String {
    format!("{name}-{}", uuid::Uuid::new_v4().simple())
}

pub fn new_run(tenant: &str) -> NewRun {
    NewRun {
        id: uuid::Uuid::new_v4().to_string(),
        tenant: tenant.into(),
        profile: "default".into(),
        source: "test".into(),
        input: "task".into(),
    }
}

pub async fn run_lifecycle_with_steps(s: Arc<dyn Store>) {
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

pub async fn tenants_cannot_read_each_other(s: Arc<dyn Store>) {
    let (a, b) = (tenant("a"), tenant("b"));
    let run = new_run(&a);
    s.create_run(&run).await.unwrap();
    assert!(s.get_run(&b, &run.id).await.unwrap().is_none());
    assert!(s.list_runs(&b, 10).await.unwrap().is_empty());
    assert!(s.get_run(&a, "not-a-uuid").await.unwrap().is_none());
}

pub async fn approvals_are_decided_once_and_scoped(s: Arc<dyn Store>) {
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

pub async fn unanswered_approval_expires(s: Arc<dyn Store>) {
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

pub async fn a_decision_taken_elsewhere_reaches_the_waiting_run(s: Arc<dyn Store>) {
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

pub async fn expire_approval_tolerates_malformed_id(s: Arc<dyn Store>) {
    s.expire_approval("not-a-uuid").await.unwrap();
}

pub async fn usage_counts_one_tenant_since_a_time(s: Arc<dyn Store>) {
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

pub async fn tenant_limits_are_stored_and_replaced(s: Arc<dyn Store>) {
    use eullm_agent::config::TenantLimits;

    let tenant = format!("editor/{}", uuid::Uuid::new_v4().simple());
    assert!(s.tenant_limits(&tenant).await.unwrap().is_none());
    let first = TenantLimits {
        max_cost_per_month: Some(5.0),
        max_runs_per_day: Some(10),
        ..Default::default()
    };
    s.set_tenant_limits(&tenant, &first, "test").await.unwrap();
    assert_eq!(s.tenant_limits(&tenant).await.unwrap(), Some(first));
    let second = TenantLimits {
        max_fetches_per_day: Some(100),
        ..Default::default()
    };
    s.set_tenant_limits(&tenant, &second, "test").await.unwrap();
    assert_eq!(s.tenant_limits(&tenant).await.unwrap(), Some(second));
    assert!(s.tenant_limits("editor/nobody").await.unwrap().is_none());
}
