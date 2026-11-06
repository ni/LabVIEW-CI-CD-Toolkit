# syntax=docker/dockerfile:1
# =============================================================================
# toimages render image for the in-place VI Browser.
#
# Public LabVIEW base + Xvfb + the lvctl `toimages` render engine (native VI
# Server TCP client + Go-side PNG encoding) + a one-shot batch runner. Each repo
# builds its OWN image from this directory via the "Build VI Browser render
# image" workflow and publishes it to that repo's GHCR with the built-in
# GITHUB_TOKEN; the render workflow then pulls it with the same token.
# =============================================================================

# ---- build stage: compile lvctl (render engine) + the batch runner ----------
FROM golang:1.26.3-alpine AS builder
RUN apk add --no-cache git zip
WORKDIR /src

# lvctl render engine (vendored) + its shared dep. The layout mirrors
# testhub's src/ so lvctl's `replace ../../shared/go/labview` resolves.
COPY _ni/labview/lvctl ./_ni/labview/lvctl
COPY _ni/shared/go/labview ./_ni/shared/go/labview
# Regenerate the embedded toimages.zip the //go:embed directive expects, then
# build a static lvctl (gopsutil/purego are CGO-free on Linux; go-ole is
# Windows-only and build-tagged out).
RUN cd _ni/labview/lvctl/vis && rm -f toimages.zip \
 && zip -rq toimages.zip toimages -x "*.DS_Store"
RUN cd _ni/labview/lvctl && CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/lvctl .

# The one-shot batch runner (pure stdlib; shells out to lvctl).
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/runner .

# ---- runtime stage: LabVIEW Linux + Xvfb + lvctl + runner --------------------
FROM nationalinstruments/labview:latest-linux
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb ca-certificates \
      fonts-dejavu-core fonts-liberation fonts-noto-core xfonts-base xfonts-75dpi xfonts-100dpi fontconfig \
 && (fc-cache -f >/dev/null 2>&1 || true) \
 && rm -rf /var/lib/apt/lists/*

# Render engine + batch runner.
COPY --from=builder /out/lvctl /app/lvctl
COPY --from=builder /out/runner /app/runner
# Headless LabVIEW config (VI Server TCP + dialog suppression) and entrypoint.
COPY labview.conf /app/labview.conf
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /app/lvctl /app/runner

ENV LABVIEW_PATH=/usr/local/natinst/LabVIEW-2026-64 \
    LABVIEW_CONF=/app/labview.conf \
    LABVIEW_HOST=127.0.0.1:3363 \
    LABVIEW_LOG=/tmp/labview.log \
    LVCTL=/app/lvctl \
    LVCTL_CACHE_DIR=/tmp/lvctl-cache \
    WORKSPACE=/work \
    WORKLIST=/out/worklist.tsv \
    OUT_BY_BLOB=/out/by-blob

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
