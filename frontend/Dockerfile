# Frontend image: a production Next.js build. The NEXT_PUBLIC_* values are
# compiled into the bundle, so they are build arguments; they are the addresses
# the *browser* uses, which is why they stay localhost in Compose too.
FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG NEXT_PUBLIC_BACKEND_WS_URL=ws://localhost:8010/ws/chat
ARG NEXT_PUBLIC_BACKEND_HTTP_URL=http://localhost:8010
ARG NEXT_PUBLIC_DEBUG_UI=1
ENV NEXT_PUBLIC_BACKEND_WS_URL=$NEXT_PUBLIC_BACKEND_WS_URL \
    NEXT_PUBLIC_BACKEND_HTTP_URL=$NEXT_PUBLIC_BACKEND_HTTP_URL \
    NEXT_PUBLIC_DEBUG_UI=$NEXT_PUBLIC_DEBUG_UI \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
