# EuLLM Agent Core for the server installation (deploy/server).
FROM rust:1.99.0-slim-trixie AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked && strip target/release/eullm-agent

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --home /var/lib/eullm-agent --create-home eullm
COPY --from=build /src/target/release/eullm-agent /usr/local/bin/eullm-agent
USER eullm
WORKDIR /var/lib/eullm-agent
EXPOSE 8088
ENTRYPOINT ["eullm-agent", "-c", "/etc/eullm-agent/config.yaml"]
CMD ["api", "--listen", "0.0.0.0:8088"]
