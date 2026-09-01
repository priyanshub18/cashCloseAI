FROM node:22-alpine AS dependencies

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund


FROM node:22-alpine AS builder

WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

ARG NEXT_PUBLIC_CASHCLOSE_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_CASHCLOSE_API_URL=${NEXT_PUBLIC_CASHCLOSE_API_URL}

COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN npm run build


FROM node:22-alpine AS runner

WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOST=0.0.0.0 \
    PORT=3000

RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 --ingroup nodejs cashclose

# Vinext's standalone output currently externalizes framework peer packages
# (including React), so keep the deterministic npm-ci dependency tree beside it.
COPY --from=dependencies --chown=cashclose:nodejs /app/node_modules ./node_modules
COPY --from=builder --chown=cashclose:nodejs /app/dist/standalone ./

USER cashclose
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
