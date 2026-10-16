# Build with frontend/ produced by scripts/release/prepare-context.sh as context.
# Keep the pinned application's next.config.mjs intact; its runtime is next start.
FROM node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e AS base
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && \
    corepack prepare pnpm@10.23.0 && \
    corepack prepare pnpm@10.33.0 --activate
WORKDIR /app

FROM base AS build
ARG XYNES_BUILD_VERSION
ARG NEXT_PUBLIC_SUPABASE_URL
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_APP_URL
ARG NEXT_PUBLIC_AUTH_APP_URL
ARG NEXT_PUBLIC_CONSOLE_URL
ARG NEXT_PUBLIC_CMS_CONSOLE_URL
ARG NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS
ARG NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE
RUN test -n "$XYNES_BUILD_VERSION" && \
    test -n "$NEXT_PUBLIC_SUPABASE_URL" && \
    test -n "$NEXT_PUBLIC_SUPABASE_ANON_KEY" && \
    test -n "$NEXT_PUBLIC_API_URL" && \
    test -n "$NEXT_PUBLIC_APP_URL" && \
    test -n "$NEXT_PUBLIC_AUTH_APP_URL" && \
    test -n "$NEXT_PUBLIC_CONSOLE_URL" && \
    test -n "$NEXT_PUBLIC_CMS_CONSOLE_URL" && \
    test -n "$NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS"
ENV XYNES_BUILD_VERSION=$XYNES_BUILD_VERSION
ENV NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL
ENV NEXT_PUBLIC_SUPABASE_ANON_KEY=$NEXT_PUBLIC_SUPABASE_ANON_KEY
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL
ENV NEXT_PUBLIC_AUTH_APP_URL=$NEXT_PUBLIC_AUTH_APP_URL
ENV NEXT_PUBLIC_CONSOLE_URL=$NEXT_PUBLIC_CONSOLE_URL
ENV NEXT_PUBLIC_CMS_CONSOLE_URL=$NEXT_PUBLIC_CMS_CONSOLE_URL
ENV NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS=$NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS
ENV NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE=$NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE
COPY . .
# Each repository owns its lock. Auth's broad workspace file must not rewrite
# sibling importers: canonical CI also installs Auth with --ignore-workspace.
RUN (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm install --frozen-lockfile) && \
    pnpm --dir xynes-auth-sdk install --frozen-lockfile && \
    pnpm --dir xynes-i18n install --frozen-lockfile && \
    pnpm --dir xynes-auth-app install --frozen-lockfile --ignore-workspace
# OverlayFS cannot atomically rename a lower-layer generated directory. Give
# the reviewed generator a fresh writable copy, preserving its source bytes.
RUN cp -a /app/lumia-ds/packages/icons/src/generated /tmp/icons-generated && \
    rm -rf /app/lumia-ds/packages/icons/src/generated && \
    cp -a /tmp/icons-generated /app/lumia-ds/packages/icons/src/generated && \
    rm -rf /tmp/icons-generated && \
    (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm --filter @lumia-ui/components... --filter @lumia-ui/forms... \
      --filter @lumia-ui/icons... --filter @lumia-ui/layout... --filter @lumia-ui/marketing... build) && \
    pnpm --dir xynes-auth-sdk build && \
    pnpm --dir xynes-i18n build && \
    NODE_ENV=production pnpm --dir xynes-auth-app exec next build && \
    mkdir -p /tmp/lumia-runtime && \
    for package in /app/lumia-ds/packages/*; do \
      if test -d "$package/dist"; then \
        mkdir -p "/tmp/lumia-runtime/$(basename "$package")" && \
        cp -a "$package/dist" "/tmp/lumia-runtime/$(basename "$package")/dist"; \
      fi; \
    done

FROM base AS production-dependencies
COPY . .
RUN (cd /app/lumia-ds && test "$(pnpm --version)" = 10.23.0 && pnpm install --prod --frozen-lockfile) && \
    pnpm --dir xynes-auth-sdk install --prod --frozen-lockfile && \
    pnpm --dir xynes-i18n install --prod --frozen-lockfile && \
    pnpm --dir xynes-auth-app install --prod --frozen-lockfile --ignore-workspace
# Retain production dependencies and linked package manifests/CSS. First-party
# source/build tooling is removed before this stage is copied into the runtime.
RUN for package in /app/xynes-auth-app /app/xynes-auth-sdk /app/xynes-i18n; do \
      find "$package" -mindepth 1 -maxdepth 1 ! -name node_modules ! -name package.json \
        ! -name messages -exec rm -rf {} +; \
    done && \
    find /app/lumia-ds -mindepth 1 -maxdepth 1 ! -name node_modules ! -name package.json \
      ! -name packages -exec rm -rf {} + && \
    for package in /app/lumia-ds/packages/*; do \
      find "$package" -mindepth 1 -maxdepth 1 ! -name node_modules ! -name package.json \
        ! -name '*.css' -exec rm -rf {} +; \
    done && \
    rm -f /app/auth-app.Dockerfile /app/auth-app.Dockerfile.dockerignore

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
ENV PORT=3100
ENV XYNES_BUILD_VERSION=$XYNES_BUILD_VERSION
COPY --from=production-dependencies --chown=nextjs:nodejs /app/ /app/
COPY --from=build --chown=nextjs:nodejs /app/xynes-auth-sdk/dist/ /app/xynes-auth-sdk/dist/
COPY --from=build --chown=nextjs:nodejs /app/xynes-i18n/dist/ /app/xynes-i18n/dist/
COPY --from=build --chown=nextjs:nodejs /tmp/lumia-runtime/ /app/lumia-ds/packages/
COPY --from=build --chown=nextjs:nodejs /app/xynes-auth-app/.next/ /app/xynes-auth-app/.next/
COPY --from=build --chown=nextjs:nodejs /app/xynes-auth-app/public/ /app/xynes-auth-app/public/
COPY --from=build --chown=nextjs:nodejs /app/xynes-auth-app/next.config.mjs /app/xynes-auth-app/next.config.mjs
WORKDIR /app/xynes-auth-app
RUN rm -rf .next/cache && mkdir -p /tmp/next-cache && chown nextjs:nodejs /tmp/next-cache && \
    ln -s /tmp/next-cache .next/cache && \
    rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
      /usr/local/bin/pnpm /usr/local/bin/pnpx /usr/local/bin/yarn /usr/local/bin/yarnpkg /opt/yarn-v1.22.22
USER nextjs
EXPOSE 3100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3100/api/health || exit 1
ENTRYPOINT ["sh", "-c", "mkdir -p /tmp/next-cache && exec \"$@\"", "--"]
CMD ["node", "node_modules/next/dist/bin/next", "start", "--hostname", "0.0.0.0", "--port", "3100"]
