FROM node:24-bookworm-slim AS build
WORKDIR /app
RUN npm install --global pnpm@11.25.0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY . .
RUN pnpm exec nuxt prepare && pnpm run build

FROM node:24-bookworm-slim
ENV NODE_ENV=production HOST=127.0.0.1 PORT=3000
WORKDIR /app
COPY --from=build --chown=node:node /app/.output ./.output
USER node
CMD ["node", ".output/server/index.mjs"]
