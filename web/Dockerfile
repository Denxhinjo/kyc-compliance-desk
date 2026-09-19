# The web service.
#
# Built from the REPOSITORY ROOT, not from /web. heroku.yml sets the context.
# The root context keeps this file honest about the fact that the two services
# share a repository even though they share nothing else.

FROM node:22-alpine AS deps
WORKDIR /app/web
# Copy the manifests alone first. Docker caches layers by their inputs, so an
# edit to application code does not invalidate the dependency install — which
# is the difference between a 10-second rebuild and a 90-second one.
COPY web/package.json web/package-lock.json ./
RUN npm ci

FROM node:22-alpine AS build
WORKDIR /app/web
COPY --from=deps /app/web/node_modules ./node_modules
COPY web/ ./
# next.config.ts reads ../.env, which does not exist in the image. dotenv is
# silent about a missing file, and Heroku injects the real values as config
# vars, so there is nothing to do here.
RUN npm run build

FROM node:22-alpine AS run
WORKDIR /app/web
ENV NODE_ENV=production
# Do not run as root. Alpine's node image ships a `node` user for this.
USER node
COPY --from=deps --chown=node:node /app/web/node_modules ./node_modules
COPY --from=build --chown=node:node /app/web/.next ./.next
COPY --from=build --chown=node:node /app/web/public ./public
COPY --from=build --chown=node:node /app/web/package.json ./package.json
COPY --from=build --chown=node:node /app/web/next.config.ts ./next.config.ts

# Heroku assigns $PORT at runtime and routes to it. Binding to a hardcoded port
# is the classic reason a deploy builds fine and then never becomes reachable.
EXPOSE 3000
CMD ["sh", "-c", "npx next start -p ${PORT:-3000} -H 0.0.0.0"]
