# Infra-owned supported runtime; source xynes-cms-console-web@aea94f6cf194480110746810c662bb2ab81f65e0 stays unchanged.
# Build from the frontend parent so every linked package is inside the context:
# docker buildx build -f xynes-cms-console-web/Dockerfile --target prod \
#   -t xynesplatform/xynes-cms-console-web:local-test --load .

FROM node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e AS base
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10.23.0 && corepack prepare pnpm@10.33.0 --activate
WORKDIR /app

FROM base AS dev
COPY . .
RUN pnpm --dir xynes-auth-sdk install --frozen-lockfile && \
    pnpm --dir xynes-i18n install --frozen-lockfile && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm install --frozen-lockfile) && \
    pnpm --dir xynes-cms-console-web install --frozen-lockfile

RUN cp -a /app/lumia-ds/packages/icons/src/generated /tmp/icons-generated && \
    rm -rf /app/lumia-ds/packages/icons/src/generated && \
    cp -a /tmp/icons-generated /app/lumia-ds/packages/icons/src/generated && \
    rm -rf /tmp/icons-generated && \
    pnpm --dir xynes-i18n build && \
    pnpm --dir xynes-auth-sdk build && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/icons build) && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/components build) && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/editor build) && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/layout build) && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/marketing build)
WORKDIR /app/xynes-cms-console-web
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
EXPOSE 3000
CMD ["pnpm", "exec", "next", "dev", "--hostname", "0.0.0.0", "--port", "3000"]

FROM base AS build
ARG XYNES_BUILD_VERSION
ARG NEXT_PUBLIC_SUPABASE_URL
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_APP_URL
ARG NEXT_PUBLIC_AUTH_APP_URL
ARG NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS
ARG NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE

RUN test -n "$XYNES_BUILD_VERSION" && \
    test -n "$NEXT_PUBLIC_SUPABASE_URL" && \
    test -n "$NEXT_PUBLIC_SUPABASE_ANON_KEY" && \
    test -n "$NEXT_PUBLIC_API_URL" && \
    test -n "$NEXT_PUBLIC_APP_URL" && \
    test -n "$NEXT_PUBLIC_AUTH_APP_URL" && \
    test -n "$NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS"

ENV XYNES_BUILD_VERSION=$XYNES_BUILD_VERSION
ENV NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL
ENV NEXT_PUBLIC_SUPABASE_ANON_KEY=$NEXT_PUBLIC_SUPABASE_ANON_KEY
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL
ENV NEXT_PUBLIC_AUTH_APP_URL=$NEXT_PUBLIC_AUTH_APP_URL
ENV NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS=$NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS
ENV NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE=$NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE

COPY . .

RUN pnpm --dir xynes-auth-sdk install --frozen-lockfile && \
    pnpm --dir xynes-i18n install --frozen-lockfile && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm install --frozen-lockfile) && \
    pnpm --dir xynes-cms-console-web install --frozen-lockfile

RUN cp -a /app/lumia-ds/packages/icons/src/generated /tmp/icons-generated && \
    rm -rf /app/lumia-ds/packages/icons/src/generated && \
    cp -a /tmp/icons-generated /app/lumia-ds/packages/icons/src/generated && \
    rm -rf /tmp/icons-generated && \
    pnpm --dir xynes-i18n build && \
    pnpm --dir xynes-auth-sdk build && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/icons build) && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/components build) && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/editor build) && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/layout build) && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --dir packages/marketing build)

RUN pnpm --dir xynes-cms-console-web exec next build && \
    find -L /app/xynes-cms-console-web/.next/standalone -type l -delete && \
    ! find -L /app/xynes-cms-console-web/.next/standalone -type l -print -quit | grep -q .

FROM node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e AS prod
WORKDIR /app

RUN apk add --no-cache --upgrade libcrypto3=3.5.9-r0 libssl3=3.5.9-r0 && \
    addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 --ingroup nodejs --home /nonexistent nextjs

ARG XYNES_BUILD_VERSION
RUN test -n "$XYNES_BUILD_VERSION"

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
ENV XYNES_BUILD_VERSION=$XYNES_BUILD_VERSION

COPY --from=build --chown=nextjs:nodejs /app/xynes-cms-console-web/.next/standalone/ ./
COPY --from=build --chown=nextjs:nodejs /app/xynes-cms-console-web/.next/static/ ./xynes-cms-console-web/.next/static/
COPY --from=build --chown=nextjs:nodejs /app/xynes-cms-console-web/public/ ./xynes-cms-console-web/public/

WORKDIR /app/xynes-cms-console-web
RUN rm -rf \
      /usr/local/lib/node_modules/npm \
      /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm \
      /usr/local/bin/npx \
      /usr/local/bin/corepack \
      /usr/local/bin/pnpm \
      /usr/local/bin/pnpx \
      /usr/local/bin/yarn \
      /usr/local/bin/yarnpkg \
      /opt/yarn-v1.22.22 && \
    ! find -L node_modules -type l -print -quit | grep -q . && \
    rm -rf .next/cache && \
    mkdir -p /tmp/next-cache && \
    chown nextjs:nodejs /tmp/next-cache && \
    ln -s /tmp/next-cache .next/cache

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3000/api/health || exit 1

CMD ["sh", "-c", "mkdir -p /tmp/next-cache && exec node server.js"]
