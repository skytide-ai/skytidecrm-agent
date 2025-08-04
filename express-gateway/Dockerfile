# Stage 1: Build
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .

# Stage 2: Production
FROM node:20-alpine
WORKDIR /app
COPY --from=build /app .
EXPOSE 8080
CMD ["node", "src/index.js"] 