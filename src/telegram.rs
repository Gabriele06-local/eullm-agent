use anyhow::Result;
use std::sync::Arc;
use teloxide::prelude::*;
use tracing::{info, warn};

use crate::approvals::Notify;
use crate::config::resolve_secret;
use crate::service::{Core, StartRun};
use crate::store::{Approval, ApprovalStatus};
use crate::util::truncate_utf8;

/// Longest answer sent in one Telegram message (the API limit is 4096).
const MAX_REPLY_BYTES: usize = 4000;
/// Tenant of runs started from Telegram.
const TENANT: &str = "default";

pub async fn serve(core: Arc<Core>) -> Result<()> {
    let tg = core.config.validate_telegram()?;
    let token = resolve_secret(&tg.token, &tg.token_env)?
        .ok_or_else(|| anyhow::anyhow!("telegram.token or telegram.token_env is required"))?;

    let bot = Bot::new(token);
    info!(
        "Telegram bot started, {} allowed user(s)",
        tg.allowed_users.len()
    );

    teloxide::repl(bot, move |bot: Bot, msg: Message| {
        let core = Arc::clone(&core);
        async move {
            if let Err(e) = dispatch(bot, msg, core).await {
                warn!("dispatch error: {e}");
            }
            respond(())
        }
    })
    .await;

    Ok(())
}

fn approval_message(a: &Approval) -> String {
    let args = serde_json::to_string_pretty(&a.arguments).unwrap_or_default();
    let short = a.id.get(..8).unwrap_or(&a.id);
    format!(
        "⚠ Approval needed\nTool: {}\nReason: {}\nArguments:\n{}\n\n/approve {short}\n/deny {short}",
        a.tool,
        a.reason,
        truncate_utf8(&args, 2500)
    )
}

/// Parse "/approve <id> [note]" or "/deny <id> [note]".
/// Returns None for other messages (including "/approveX").
///
/// A bare "/approve" or "/deny" parses with an empty id so the caller can
/// answer with usage instead of silently ignoring it — or worse, matching
/// every pending approval, since "" is a prefix of any id.
fn approval_command(text: &str) -> Option<(bool, &str, Option<&str>)> {
    for (cmd, approve) in [("/approve", true), ("/deny", false)] {
        let Some(rest) = text.strip_prefix(cmd) else {
            continue;
        };
        if !rest.is_empty() && !rest.starts_with(' ') {
            continue;
        }
        let mut parts = rest.trim().splitn(2, ' ');
        let id = parts.next().unwrap_or_default();
        let note = parts.next().map(str::trim).filter(|s| !s.is_empty());
        return Some((approve, id, note));
    }
    None
}

/// Find a pending approval by full id or unique prefix.
async fn find_pending(core: &Core, id: &str) -> Result<Option<String>> {
    let pending = core
        .store
        .list_approvals(TENANT, Some(ApprovalStatus::Pending))
        .await?;
    let matches: Vec<&Approval> = pending.iter().filter(|a| a.id.starts_with(id)).collect();
    Ok(match matches.as_slice() {
        [one] => Some(one.id.clone()),
        _ => None,
    })
}

async fn dispatch(bot: Bot, msg: Message, core: Arc<Core>) -> Result<()> {
    let Some(tg) = core.config.telegram.as_ref() else {
        return Ok(());
    };
    // Channel posts and anonymous admins have no sender: never authorised.
    let Some(uid) = msg.from.as_ref().map(|u| u.id.0 as i64) else {
        warn!("Rejected message without a sender");
        return Ok(());
    };
    if !is_allowed(&tg.allowed_users, uid) {
        warn!("Rejected unauthorized user {uid}");
        return Ok(());
    }

    let text = match msg.text() {
        Some(t) => t,
        None => return Ok(()),
    };

    if text == "/help" || text.starts_with("/help ") {
        bot.send_message(
            msg.chat.id,
            "/run <task> — execute a task\n/approve <id> — allow a pending action\n\
             /deny <id> [note] — refuse a pending action\n/status — show models & tools\n\
             /help — this message",
        )
        .await?;
        return Ok(());
    }

    if text == "/status" {
        let defs = core.tools.definitions();
        let tool_names: Vec<&str> = defs.iter().map(|t| t.name.as_str()).collect();
        bot.send_message(
            msg.chat.id,
            format!(
                "Models: {}\nTools: {}",
                core.router.names().join(", "),
                tool_names.join(", "),
            ),
        )
        .await?;
        return Ok(());
    }

    if let Some((approve, id, note)) = approval_command(text) {
        if id.is_empty() {
            bot.send_message(
                msg.chat.id,
                "Usage: /approve <id>\nUsage: /deny <id> [note]",
            )
            .await?;
            return Ok(());
        }
        let reply = match find_pending(&core, id).await? {
            None => "No pending approval with that id.".to_string(),
            Some(full) => {
                let by = format!("telegram:{uid}");
                match core
                    .approvals
                    .decide(TENANT, &full, approve, &by, note)
                    .await?
                {
                    Some(a) => format!(
                        "{} {}: {}",
                        if approve { "✅" } else { "⛔" },
                        a.status.as_str(),
                        a.tool
                    ),
                    None => "That approval was already decided.".to_string(),
                }
            }
        };
        bot.send_message(msg.chat.id, reply).await?;
        return Ok(());
    }

    if let Some(task) = text.strip_prefix("/run ") {
        let task = task.trim().to_string();
        if task.is_empty() {
            bot.send_message(msg.chat.id, "Usage: /run <task description>")
                .await?;
            return Ok(());
        }

        bot.send_message(msg.chat.id, format!("▶ {task}")).await?;
        let chat_id = msg.chat.id;
        let notify_bot = bot.clone();
        let notify: Notify = Arc::new(move |a: &Approval| {
            let bot = notify_bot.clone();
            let text = approval_message(a);
            tokio::spawn(async move {
                let _ = bot.send_message(chat_id, text).await;
            });
        });
        let profile = tg.profile.clone();
        // Run in the background: updates from one chat are handled in
        // order, and /approve must get through while the run waits.
        tokio::spawn(async move {
            let req = StartRun {
                tenant: TENANT.into(),
                profile,
                input: task,
                source: format!("telegram:{uid}"),
                notify: Some(notify),
            };
            let reply = match core.run_now(req).await {
                Ok((_, Ok(answer))) => {
                    if answer.len() > MAX_REPLY_BYTES {
                        format!("{}…\n[truncated]", truncate_utf8(&answer, MAX_REPLY_BYTES))
                    } else {
                        answer
                    }
                }
                Ok((_, Err(e))) | Err(e) => format!("❌ {e}"),
            };
            let _ = bot.send_message(chat_id, reply).await;
        });
        return Ok(());
    }

    Ok(())
}

/// Only listed users may send tasks; an empty list allows nobody.
pub fn is_allowed(allowed_users: &[i64], uid: i64) -> bool {
    allowed_users.contains(&uid)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn approval_command_parses_id_and_note() {
        assert_eq!(
            approval_command("/approve abc123"),
            Some((true, "abc123", None))
        );
        assert_eq!(
            approval_command("/deny abc123 not now"),
            Some((false, "abc123", Some("not now")))
        );
    }

    #[test]
    fn bare_command_parses_with_empty_id() {
        assert_eq!(approval_command("/approve"), Some((true, "", None)));
        assert_eq!(approval_command("/deny  "), Some((false, "", None)));
    }

    #[test]
    fn other_messages_are_not_approval_commands() {
        assert_eq!(approval_command("/approveX abc"), None);
        assert_eq!(approval_command("/run do things"), None);
        assert_eq!(approval_command("hello"), None);
    }
}
