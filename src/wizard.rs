use anyhow::Result;
use std::io::{self, Write};

use crate::config::{
    default_system_prompt, Config, LimitsConfig, ModulesConfig, ProviderConfig, TelegramConfig,
    ToolsConfig,
};

pub fn run(config_path: &std::path::Path) -> Result<Config> {
    println!("\nNo config file found at '{}'.", config_path.display());
    println!("Answer a few questions to get started.\n");

    let provider = pick_provider()?;
    let telegram = pick_telegram()?;

    let config = Config {
        provider,
        pricing: None,
        telegram,
        tools: ToolsConfig::default(),
        modules: ModulesConfig::default(),
        limits: LimitsConfig::default(),
        workspace: "workspace".into(),
        audit_log: Some("eullm-agent-audit.jsonl".into()),
        max_iterations: 20,
        system_prompt: default_system_prompt(),
        models: Default::default(),
        profiles: Default::default(),
        policy_file: None,
        database: None,
        api: None,
    };

    let yaml = serde_saphyr::to_string(&config)?;
    write_private(config_path, &yaml)?;
    println!(
        "\nConfig saved to '{}'. Starting...\n",
        config_path.display()
    );

    Ok(config)
}

fn pick_provider() -> Result<ProviderConfig> {
    println!("Choose a provider:");
    println!("  1. Anthropic (Claude)  [default]");
    println!("  2. EULLM Engine (local)");
    println!("  3. OpenAI / compatible");
    println!("  4. Ollama");
    println!();

    loop {
        let choice = prompt("Provider", "1")?;
        match choice.trim() {
            "1" | "" => return setup_anthropic(),
            "2" => return setup_eullm(),
            "3" => return setup_openai(),
            "4" => return setup_ollama(),
            _ => println!("Enter 1, 2, 3 or 4."),
        }
    }
}

fn setup_anthropic() -> Result<ProviderConfig> {
    println!();
    let (api_key, api_key_env) = secret("Anthropic API key", "ANTHROPIC_API_KEY")?;
    let model = prompt("Model", "claude-sonnet-4-6")?;
    Ok(ProviderConfig::Anthropic {
        api_key,
        api_key_env,
        model,
        max_tokens: 4096,
    })
}

fn setup_eullm() -> Result<ProviderConfig> {
    println!();
    let base_url = prompt("EuLLM Engine URL", "http://localhost:11434")?;
    let model = prompt_required("Model name")?;
    let key = prompt("API key (blank if the Engine has no EULLM_API_KEYS)", "")?;
    Ok(ProviderConfig::Eullm {
        base_url,
        model,
        api_key: (!key.is_empty()).then_some(key),
        api_key_env: None,
    })
}

fn setup_ollama() -> Result<ProviderConfig> {
    println!();
    let base_url = prompt("Ollama URL", "http://localhost:11434")?;
    let model = prompt_required("Model name")?;
    Ok(ProviderConfig::Ollama { base_url, model })
}

fn setup_openai() -> Result<ProviderConfig> {
    println!();
    let (api_key, api_key_env) = secret("OpenAI API key", "OPENAI_API_KEY")?;
    let model = prompt("Model", "gpt-4o")?;
    let base_url_raw = prompt("Base URL (blank = api.openai.com)", "")?;
    Ok(ProviderConfig::OpenAI {
        api_key,
        api_key_env,
        model,
        base_url: if base_url_raw.is_empty() {
            None
        } else {
            Some(base_url_raw)
        },
    })
}

fn pick_telegram() -> Result<Option<TelegramConfig>> {
    println!();
    let enable = prompt("Enable Telegram bot?", "N")?;
    let enable_lower = enable.to_lowercase();
    if !matches!(enable_lower.trim(), "y" | "yes") {
        return Ok(None);
    }
    let (token, token_env) = secret("Bot token", "TELEGRAM_BOT_TOKEN")?;
    let allowed_users = loop {
        let ids_raw = prompt_required("Allowed Telegram user IDs, comma-separated")?;
        let ids: Vec<i64> = ids_raw
            .split(',')
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .filter_map(|s| s.parse::<i64>().ok())
            .collect();
        if !ids.is_empty() {
            break ids;
        }
        println!("  (at least one numeric user ID is required)");
    };
    Ok(Some(TelegramConfig {
        token,
        token_env,
        allowed_users,
        profile: "default".into(),
    }))
}

/// Ask for a secret: an environment variable name (preferred) or the value
/// itself, which is then stored in the config file.
fn secret(label: &str, default_env: &str) -> Result<(Option<String>, Option<String>)> {
    let var = prompt(
        &format!("Environment variable holding the {label} (blank to paste it instead)"),
        default_env,
    )?;
    if !var.is_empty() && std::env::var(&var).is_ok() {
        return Ok((None, Some(var)));
    }
    if !var.is_empty() {
        let keep = prompt(&format!("{var} is not set now. Use it anyway? [Y/n]"), "Y")?;
        if !matches!(keep.trim().to_lowercase().as_str(), "n" | "no") {
            return Ok((None, Some(var)));
        }
    }
    Ok((Some(prompt_required(label)?), None))
}

/// Write the config readable only by its owner: it may hold secrets.
fn write_private(path: &std::path::Path, text: &str) -> Result<()> {
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(path)?;
    f.write_all(text.as_bytes())?;
    Ok(())
}

fn prompt(label: &str, default: &str) -> Result<String> {
    if default.is_empty() {
        print!("{}: ", label);
    } else {
        print!("{} [{}]: ", label, default);
    }
    io::stdout().flush()?;
    let mut buf = String::new();
    io::stdin().read_line(&mut buf)?;
    let val = buf.trim().to_string();
    Ok(if val.is_empty() {
        default.to_string()
    } else {
        val
    })
}

fn prompt_required(label: &str) -> Result<String> {
    loop {
        print!("{}: ", label);
        io::stdout().flush()?;
        let mut buf = String::new();
        io::stdin().read_line(&mut buf)?;
        let val = buf.trim().to_string();
        if !val.is_empty() {
            return Ok(val);
        }
        println!("  (required \u{2014} please enter a value)");
    }
}
