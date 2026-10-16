# Infra-owned runtime refresh; derived from xynes-gateway at 866a3c4c699a78cc907ceb65d4bfd5be9ddc8cc1.
# Original source COPY/install/command contract is retained.
FROM oven/bun:1.4.2-alpine@sha256:d888c0ae6c86d7866ff10c5aafdd9077b36aee6455b33dd270fb93c0dd5cef6f AS base
WORKDIR /app
COPY package.json bun.lock ./

FROM base AS dev
RUN bun install --frozen-lockfile
COPY . .
EXPOSE 4100
CMD ["bun", "--watch", "src/index.ts"]

FROM oven/bun:1.4.2-alpine@sha256:d888c0ae6c86d7866ff10c5aafdd9077b36aee6455b33dd270fb93c0dd5cef6f AS prod
WORKDIR /app

RUN apk add --no-cache --upgrade libcrypto3=3.5.9-r0 libssl3=3.5.9-r0

RUN addgroup -S -g 1001 xynes && \
    adduser  -S -u 1001 -G xynes -H xynes

COPY package.json bun.lock tsconfig.json ./
COPY eslint.config.mjs ./
COPY src ./src
COPY index.ts ./index.ts
RUN bun install --frozen-lockfile && \
    rm -rf node_modules && \
    bun install --production --frozen-lockfile && \
    rm -rf /root/.bun /tmp/* && \
    chown -R xynes:xynes /app

USER xynes
EXPOSE 4100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD bun run healthcheck || exit 1

CMD ["bun", "run", "src/index.ts"]
