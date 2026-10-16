# Only curated immutable storage-service shim/tests enter this context.
FROM oven/bun:1.4.2@sha256:9114c058aeae42162ee16dd5084b95fe9473970bb6bcb5b232ab1630f0546895 AS verify
WORKDIR /src
COPY package.json bun.lock tsconfig.json .eslintrc.cjs ./
COPY src/ ./src/
COPY tests/ ./tests/
RUN bun install --frozen-lockfile \
  && bun run test:coverage \
  && bun run typecheck \
  && bun run lint

FROM debian:12-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS prod
# HTTPS bootstrap trust comes from the immutable runtime source. APT still
# verifies Debian signatures using the Debian base's archive keyring.
COPY --from=verify /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
  && printf '%s\n' \
    'Types: deb' \
    'URIs: https://snapshot.debian.org/archive/debian/20261009T000000Z/' \
    'Suites: bookworm' \
    'Components: main' \
    'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
    'Check-Valid-Until: no' \
    '' \
    'Types: deb' \
    'URIs: https://snapshot.debian.org/archive/debian-security/20261009T000000Z/' \
    'Suites: bookworm-security' \
    'Components: main' \
    'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
    'Check-Valid-Until: no' \
    > /etc/apt/sources.list.d/xynes-snapshot.sources \
  && apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libreoffice-core=4:7.4.7-1+deb12u14 \
    libreoffice-common=4:7.4.7-1+deb12u14 \
    libreoffice-writer=4:7.4.7-1+deb12u14 \
    libreoffice-calc=4:7.4.7-1+deb12u14 \
    libreoffice-impress=4:7.4.7-1+deb12u14 \
    libreoffice-draw=4:7.4.7-1+deb12u14 \
    fonts-liberation=1:1.07.4-11 \
    fonts-dejavu-core=2.37-6 \
    ca-certificates=20250419~deb12u1 \
    tini=0.19.0-1+b3 \
  && mkdir -p /usr/share/xynes /app \
  && dpkg-query -W -f='${Package}=${Version}\n' > /usr/share/xynes/native-packages.txt \
  && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
  && rm -f /var/log/apt/history.log /var/log/apt/term.log /var/log/apt/eipp.log.xz /var/log/dpkg.log /var/log/alternatives.log \
  && find /var/cache/fontconfig -maxdepth 1 -type f -name '*-le64.cache-8' -delete \
  && rm -f /var/cache/ldconfig/aux-cache \
  && groupadd --system --gid 10001 xynes \
  && useradd --system --uid 10001 --gid xynes --home-dir /app --shell /usr/sbin/nologin xynes \
  && chown 10001:10001 /app
COPY --from=verify /usr/local/bin/bun /usr/local/bin/bun
COPY --from=verify --chown=10001:10001 /src/src/ /app/src/
WORKDIR /app
ENV SAL_DISABLE_MACROS=1 HOME=/tmp XDG_CONFIG_HOME=/tmp/.config XDG_CACHE_HOME=/tmp/.cache SAL_USE_COMMON_ONE_ACCEL=0
USER 10001:10001
EXPOSE 8100
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bun", "run", "src/shim.ts"]
