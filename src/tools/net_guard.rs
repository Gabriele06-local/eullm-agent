//! Which addresses the HTTP tool may reach.
//!
//! A URL chosen by the model (or by a web page the model read) must not reach
//! the machine itself, the local network or cloud metadata endpoints. Every
//! address a host resolves to must be public; the caller then pins the
//! connection to the checked address so DNS cannot change between the check
//! and the request.

use anyhow::{bail, Context, Result};
use reqwest::Url;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};

/// True for addresses on the public internet.
pub fn is_public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => is_public_v4(v4),
        IpAddr::V6(v6) => is_public_v6(v6),
    }
}

fn is_public_v4(ip: Ipv4Addr) -> bool {
    let [a, b, c, _] = ip.octets();
    !(ip.is_unspecified()
        || ip.is_loopback()
        || ip.is_private()
        || ip.is_link_local()
        || ip.is_broadcast()
        || ip.is_documentation()
        || ip.is_multicast()
        || a == 0
        || (a == 100 && (64..=127).contains(&b)) // shared address space (CGNAT)
        || (a == 192 && b == 0 && c == 0) // IETF protocol assignments
        || (a == 198 && (b == 18 || b == 19)) // benchmarking
        || a >= 240) // reserved
}

fn is_public_v6(ip: Ipv6Addr) -> bool {
    if let Some(v4) = ip.to_ipv4_mapped() {
        return is_public_v4(v4);
    }
    let seg = ip.segments();
    // NAT64 (64:ff9b::/96) and 6to4 (2002::/16) embed an IPv4 address.
    if seg[0] == 0x64 && seg[1] == 0xff9b && seg[2..6] == [0, 0, 0, 0] {
        let v4 = Ipv4Addr::new(
            (seg[6] >> 8) as u8,
            seg[6] as u8,
            (seg[7] >> 8) as u8,
            seg[7] as u8,
        );
        return is_public_v4(v4);
    }
    if seg[0] == 0x2002 {
        let v4 = Ipv4Addr::new(
            (seg[1] >> 8) as u8,
            seg[1] as u8,
            (seg[2] >> 8) as u8,
            seg[2] as u8,
        );
        return is_public_v4(v4);
    }
    !(ip.is_unspecified()
        || ip.is_loopback()
        || ip.is_multicast()
        || (seg[0] & 0xfe00) == 0xfc00 // unique local
        || (seg[0] & 0xffc0) == 0xfe80 // link local
        || (seg[0] & 0xffc0) == 0xfec0 // site local (deprecated)
        || (seg[0] == 0x2001 && seg[1] == 0x0db8) // documentation
        || (seg[0] == 0x0100 && seg[1..4] == [0, 0, 0]) // discard prefix
        || seg[..6] == [0, 0, 0, 0, 0, 0]) // IPv4-compatible, deprecated
}

/// What the tool is allowed to request.
#[derive(Debug, Clone)]
pub struct NetPolicy {
    pub allow_http: bool,
    pub allow_private_networks: bool,
    pub allowed_domains: Vec<String>,
}

impl NetPolicy {
    /// Check scheme, host and every resolved address. Returns the address the
    /// connection must be pinned to.
    pub async fn check(&self, url: &Url) -> Result<SocketAddr> {
        match url.scheme() {
            "https" => {}
            "http" if self.allow_http => {}
            other => bail!("Blocked: scheme '{other}' is not allowed"),
        }
        if !url.username().is_empty() || url.password().is_some() {
            bail!("Blocked: credentials in URLs are not allowed");
        }
        let host = url.host_str().context("Blocked: URL has no host")?;
        let host_plain = host.trim_start_matches('[').trim_end_matches(']');
        if !self.allowed_domains.is_empty() && !self.domain_allowed(host_plain) {
            bail!("Blocked: host '{host_plain}' is not in tools.http.allowed_domains");
        }
        let port = url.port_or_known_default().context("Blocked: no port")?;

        let addrs: Vec<SocketAddr> = match host_plain.parse::<IpAddr>() {
            Ok(ip) => vec![SocketAddr::new(ip, port)],
            Err(_) => tokio::net::lookup_host((host_plain, port))
                .await
                .with_context(|| format!("cannot resolve {host_plain}"))?
                .collect(),
        };
        if addrs.is_empty() {
            bail!("cannot resolve {host_plain}");
        }
        if !self.allow_private_networks {
            if let Some(bad) = addrs.iter().find(|a| !is_public_ip(a.ip())) {
                bail!(
                    "Blocked: {host_plain} resolves to a non-public address ({})",
                    bad.ip()
                );
            }
        }
        Ok(addrs[0])
    }

    fn domain_allowed(&self, host: &str) -> bool {
        let host = host.trim_end_matches('.').to_ascii_lowercase();
        self.allowed_domains.iter().any(|d| {
            let d = d.trim_end_matches('.').to_ascii_lowercase();
            if let Some(base) = d.strip_prefix("*.") {
                host.ends_with(&format!(".{base}"))
            } else {
                host == d || host.ends_with(&format!(".{d}"))
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(s: &str) -> IpAddr {
        s.parse().unwrap()
    }

    #[test]
    fn non_public_addresses_are_rejected() {
        for s in [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "255.255.255.255",
            "::1",
            "::",
            "fc00::1",
            "fd12::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "64:ff9b::a00:1",
            "2002:7f00:1::",
            "2001:db8::1",
        ] {
            assert!(!is_public_ip(ip(s)), "{s} should be blocked");
        }
    }

    #[test]
    fn public_addresses_are_accepted() {
        for s in [
            "1.1.1.1",
            "93.184.216.34",
            "2606:4700:4700::1111",
            "::ffff:8.8.8.8",
        ] {
            assert!(is_public_ip(ip(s)), "{s} should be allowed");
        }
    }

    #[test]
    fn domain_allowlist_matches_subdomains_only() {
        let p = NetPolicy {
            allow_http: false,
            allow_private_networks: false,
            allowed_domains: vec!["example.com".into()],
        };
        assert!(p.domain_allowed("example.com"));
        assert!(p.domain_allowed("api.example.com"));
        assert!(!p.domain_allowed("evilexample.com"));
        assert!(!p.domain_allowed("example.com.evil.net"));
    }

    #[test]
    fn wildcard_domains_do_not_match_the_apex() {
        let p = NetPolicy {
            allow_http: false,
            allow_private_networks: false,
            allowed_domains: vec!["*.example.com".into()],
        };
        assert!(!p.domain_allowed("example.com"));
        assert!(p.domain_allowed("api.example.com"));
        assert!(p.domain_allowed("a.b.example.com"));
        assert!(!p.domain_allowed("evilexample.com"));
    }
}
