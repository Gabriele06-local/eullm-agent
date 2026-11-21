//! Running external programs without a shell, with a timeout that really
//! stops them and output that cannot grow without bound.
//!
//! With the bubblewrap sandbox (the default) a program runs in its own
//! namespaces: no network, its own process tree (everything it starts dies
//! with it), a read-only view of the system directories, the workspace and
//! nothing else of the host (no /home, /root, /etc/shadow, /var...).

use anyhow::{bail, Context, Result};
use std::ffi::OsString;
use std::path::Path;
use std::process::{ExitStatus, Stdio};
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;

use crate::config::{SandboxConfig, SandboxMode};
use crate::util::truncate_utf8;

pub struct ProgramOutput {
    pub status: ExitStatus,
    pub stdout: String,
    pub stderr: String,
    pub truncated: bool,
}

/// setrlimit with the same soft and hard value (a macro: the resource type
/// differs between libc flavours).
#[cfg(unix)]
macro_rules! set_limit {
    ($resource:expr, $value:expr) => {{
        let lim = libc::rlimit {
            rlim_cur: $value as libc::rlim_t,
            rlim_max: $value as libc::rlim_t,
        };
        libc::setrlimit($resource, &lim);
    }};
}

const KEPT_VARS: [&str; 6] = ["PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TEMP", "TMP"];

/// System directories a sandboxed program sees, read-only, when present.
const SYSTEM_DIRS: [&str; 7] = [
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
];
/// The few files under /etc that common programs need.
const SYSTEM_ETC: [&str; 6] = [
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/alternatives",
    "/etc/localtime",
    "/etc/fonts",
];

/// The bubblewrap arguments that set up the sandbox, up to and including `--`.
pub fn bwrap_args(sandbox: &SandboxConfig, cwd: &Path) -> Vec<OsString> {
    let mut a: Vec<OsString> = Vec::new();
    let mut push = |items: &[&std::ffi::OsStr]| a.extend(items.iter().map(|s| s.to_os_string()));
    push(&[
        "--unshare-all".as_ref(),
        "--die-with-parent".as_ref(),
        "--new-session".as_ref(),
    ]);
    if sandbox.network {
        push(&["--share-net".as_ref()]);
    }
    for dir in SYSTEM_DIRS {
        let p = Path::new(dir);
        match std::fs::read_link(p) {
            // Merged /usr: /bin -> usr/bin and so on.
            Ok(target) => push(&["--symlink".as_ref(), target.as_os_str(), p.as_os_str()]),
            Err(_) if p.is_dir() => push(&["--ro-bind".as_ref(), p.as_os_str(), p.as_os_str()]),
            Err(_) => {}
        }
    }
    for path in SYSTEM_ETC
        .iter()
        .map(Path::new)
        .chain(sandbox.read_only_paths.iter().map(|p| p.as_path()))
    {
        push(&["--ro-bind-try".as_ref(), path.as_os_str(), path.as_os_str()]);
    }
    push(&[
        "--proc".as_ref(),
        "/proc".as_ref(),
        "--dev".as_ref(),
        "/dev".as_ref(),
        "--tmpfs".as_ref(),
        "/tmp".as_ref(),
    ]);
    let bind = if sandbox.writable_workspace {
        "--bind"
    } else {
        "--ro-bind"
    };
    push(&[bind.as_ref(), cwd.as_os_str(), cwd.as_os_str()]);
    push(&["--chdir".as_ref(), cwd.as_os_str(), "--clearenv".as_ref()]);
    for var in KEPT_VARS {
        if let Some(v) = std::env::var_os(var) {
            push(&["--setenv".as_ref(), var.as_ref(), v.as_os_str()]);
        }
    }
    push(&[
        "--setenv".as_ref(),
        "HOME".as_ref(),
        cwd.as_os_str(),
        "--setenv".as_ref(),
        "TMPDIR".as_ref(),
        "/tmp".as_ref(),
        "--".as_ref(),
    ]);
    a
}

/// Check at startup that the sandbox works on this host, so that a missing
/// or blocked bubblewrap stops the service instead of running programs
/// unconfined.
pub fn check_sandbox(sandbox: &SandboxConfig, cwd: &Path) -> Result<()> {
    if sandbox.mode == SandboxMode::None {
        tracing::warn!(
            "tools.sandbox.mode is none: programs run without isolation (network, files and \
             processes of the service user are reachable)"
        );
        return Ok(());
    }
    if !cfg!(target_os = "linux") {
        bail!("tools.sandbox.mode bubblewrap needs Linux; set tools.sandbox.mode: none to run programs unconfined");
    }
    let out = std::process::Command::new(&sandbox.bwrap)
        .args(bwrap_args(sandbox, cwd))
        .arg("true")
        .env_clear()
        .stdin(Stdio::null())
        .output();
    match out {
        Ok(o) if o.status.success() => Ok(()),
        Ok(o) => bail!(
            "the bubblewrap sandbox does not work on this host: {}. Allow unprivileged user \
             namespaces for bwrap, or set tools.sandbox.mode: none to run programs unconfined",
            String::from_utf8_lossy(&o.stderr).trim()
        ),
        Err(e) => bail!(
            "cannot start {} ({e}): install bubblewrap, or set tools.sandbox.mode: none to run \
             programs unconfined",
            sandbox.bwrap.display()
        ),
    }
}

/// Run `program` with `args` (never through a shell) in `cwd`, with a clean
/// environment, inside the sandbox. On timeout the whole process group is
/// killed; in the sandbox, everything the program started dies with it.
pub async fn run_program(
    program: &str,
    args: &[String],
    cwd: &Path,
    timeout: Duration,
    max_output_bytes: usize,
    sandbox: &SandboxConfig,
) -> Result<ProgramOutput> {
    let mut cmd = match sandbox.mode {
        SandboxMode::Bubblewrap => {
            let mut c = Command::new(&sandbox.bwrap);
            c.args(bwrap_args(sandbox, cwd)).arg(program);
            c
        }
        SandboxMode::None => Command::new(program),
    };
    cmd.args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    cmd.env_clear();
    if sandbox.mode == SandboxMode::None {
        // In the sandbox bwrap sets these for the program itself.
        for var in KEPT_VARS {
            if let Ok(v) = std::env::var(var) {
                cmd.env(var, v);
            }
        }
        cmd.env("HOME", cwd);
    }
    #[cfg(unix)]
    {
        cmd.process_group(0);
        let (mem, file) = (sandbox.max_memory_mb, sandbox.max_file_mb);
        // SAFETY: only async-signal-safe setrlimit calls between fork and exec.
        unsafe {
            cmd.pre_exec(move || {
                set_limit!(libc::RLIMIT_CORE, 0);
                if mem > 0 {
                    set_limit!(libc::RLIMIT_AS, mem * 1024 * 1024);
                }
                if file > 0 {
                    set_limit!(libc::RLIMIT_FSIZE, file * 1024 * 1024);
                }
                Ok(())
            });
        }
    }

    let mut child = cmd
        .spawn()
        .with_context(|| format!("cannot start {program}"))?;
    let pid = child.id();
    let stdout = child.stdout.take().context("no stdout")?;
    let stderr = child.stderr.take().context("no stderr")?;
    let out_task = tokio::spawn(read_capped(stdout, max_output_bytes));
    let err_task = tokio::spawn(read_capped(stderr, max_output_bytes));

    let deadline = tokio::time::Instant::now() + timeout;
    let timed_out = |pid| {
        kill_group(pid);
        anyhow::anyhow!(
            "Command timed out after {}s and was stopped",
            timeout.as_secs()
        )
    };
    let status = match tokio::time::timeout_at(deadline, child.wait()).await {
        Ok(status) => status?,
        Err(_) => {
            let err = timed_out(pid);
            let _ = child.kill().await;
            out_task.abort();
            err_task.abort();
            return Err(err);
        }
    };
    // A background process started by the program can keep the pipes open
    // after it exits; the same deadline applies to reading them.
    let outputs = tokio::time::timeout_at(deadline, async {
        let (stdout, t1) = out_task.await??;
        let (stderr, t2) = err_task.await??;
        anyhow::Ok((stdout, t1, stderr, t2))
    })
    .await;
    let (stdout, t1, stderr, t2) = match outputs {
        Ok(r) => r?,
        Err(_) => return Err(timed_out(pid)),
    };
    Ok(ProgramOutput {
        status,
        stdout,
        stderr,
        truncated: t1 || t2,
    })
}

/// Read everything, keep at most `max` bytes (lossy UTF-8).
async fn read_capped<R: AsyncRead + Unpin>(mut r: R, max: usize) -> Result<(String, bool)> {
    let mut kept = Vec::new();
    let mut buf = [0u8; 8192];
    let mut truncated = false;
    loop {
        let n = r.read(&mut buf).await?;
        if n == 0 {
            break;
        }
        let room = max.saturating_sub(kept.len());
        if room < n {
            truncated = true;
        }
        kept.extend_from_slice(&buf[..n.min(room)]);
    }
    let text = String::from_utf8_lossy(&kept).into_owned();
    Ok((truncate_utf8(&text, max).to_string(), truncated))
}

#[cfg(unix)]
fn kill_group(pid: Option<u32>) {
    if let Some(pid) = pid {
        // The child leads its own process group (process_group(0)).
        unsafe {
            libc::killpg(pid as libc::pid_t, libc::SIGKILL);
        }
    }
}

#[cfg(not(unix))]
fn kill_group(_pid: Option<u32>) {}

/// Format the result the way tools return it to the model.
pub fn format_output(out: &ProgramOutput) -> String {
    let mut result = String::new();
    if !out.stdout.is_empty() {
        result.push_str(&out.stdout);
    }
    if !out.stderr.is_empty() {
        if !result.is_empty() {
            result.push('\n');
        }
        result.push_str("[stderr] ");
        result.push_str(&out.stderr);
    }
    if out.truncated {
        result.push_str("\n[output truncated]");
    }
    if !out.status.success() {
        let code = out.status.code().unwrap_or(-1);
        result.push_str(&format!("\n[exit {code}]"));
    }
    if result.is_empty() {
        result = "(no output)".into();
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn read_capped_stays_within_the_byte_limit() {
        let invalid = vec![0xffu8; 100];
        let (out, truncated) = read_capped(&invalid[..], 10).await.unwrap();
        assert!(truncated);
        assert!(out.len() <= 10, "{} bytes", out.len());

        let unicode = "€".repeat(10);
        let (out, truncated) = read_capped(unicode.as_bytes(), 10).await.unwrap();
        assert!(truncated);
        assert!(out.len() <= 10, "{} bytes", out.len());
        assert!(!out.contains('\u{fffd}'));
    }
}
