FROM caddy:2.11.7-builder AS builder
RUN xcaddy build v2.11.7 --with github.com/sablierapp/sablier-caddy-plugin@v1.0.2
FROM caddy:2.11.7-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
