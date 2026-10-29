# syntax=docker/dockerfile:1.7
# =============================================================================
# LabVIEW CI Linux image
# =============================================================================
# Extends the official NI LabVIEW Linux container with native VIPM, applies repo
# VIPC dependency files, AND bakes in the VI Browser render engine (lvctl +
# the batch runner) so position-aware 2.0 screenshots render inside this worker
# with no separate image to build -- a client just copies this worker.
# =============================================================================

ARG VIPM_DEB_URL=https://traffic.libsyn.com/secure/jkinc/vipm_26.3.0-3954_amd64.deb
ARG LV_CONTAINER_TAG=2026q1patch2-linux

# ---- build stage: compile the VI Browser render engine (lvctl + runner) ---
# Mirrors .github/labview/toimages/Dockerfile so the engine is byte-for-byte the
# same as the standalone render image used to be -- just baked into the worker.
FROM golang:1.26.3-alpine AS toimages-builder
RUN apk add --no-cache git zip
WORKDIR /src
# lvctl render engine (vendored) + its shared dep; the layout lets lvctl's
# `replace ../../shared/go/labview` resolve.
COPY .github/labview/toimages/_ni/labview/lvctl ./_ni/labview/lvctl
COPY .github/labview/toimages/_ni/shared/go/labview ./_ni/shared/go/labview
RUN cd _ni/labview/lvctl/vis && rm -f toimages.zip \
 && zip -rq toimages.zip toimages -x "*.DS_Store"
RUN cd _ni/labview/lvctl && CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/lvctl .
# The one-shot batch runner (pure stdlib; shells out to lvctl).
COPY .github/labview/toimages/go.mod ./
COPY .github/labview/toimages/main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/runner .

# ---- worker image ------------------------------------------------------------
# Pinned NI container release, passed by build-labview-linux-image.yml from the
# catalog's containers[] entry - never float on latest-linux (see the Windows
# base Dockerfile for the 2026-07-27 latest-tag incident this prevents). The
# default here only covers ad-hoc local builds.
FROM nationalinstruments/labview:${LV_CONTAINER_TAG}

ARG VIPM_DEB_URL
ARG CI_WORKER_VERSION=dev
ARG LABVIEW_VERSION=2026

COPY .github/labview/vipm/install-vipc-linux.sh /opt/lvci/vipm/install-vipc-linux.sh
COPY .github/labview/vipm/ /opt/lvci/vipc/

RUN set -eux; \
      chmod +x /opt/lvci/vipm/install-vipc-linux.sh; \
      export DEBIAN_FRONTEND=noninteractive; \
      apt-get update; \
                  apt-get install -y --no-install-recommends ca-certificates curl unzip xvfb \
        fonts-dejavu-core fonts-liberation fonts-noto-core xfonts-base xfonts-75dpi xfonts-100dpi fontconfig; \
      fc-cache -f >/dev/null 2>&1 || true; \
      curl -fL --retry 3 --retry-delay 2 -o /tmp/vipm.deb "${VIPM_DEB_URL}"; \
      dpkg -i /tmp/vipm.deb || apt-get install -f -y --no-install-recommends; \
      rm -f /tmp/vipm.deb; \
      rm -rf /var/lib/apt/lists/*; \
      command -v vipm; \
      vipm --version || true

RUN --mount=type=secret,id=vipm_serial,required=false \
      --mount=type=secret,id=vipm_full_name,required=false \
      --mount=type=secret,id=vipm_email,required=false \
      set -eux; \
      if [ -f /run/secrets/vipm_serial ]; then export VIPM_SERIAL_NUMBER="$(cat /run/secrets/vipm_serial)"; fi; \
      if [ -f /run/secrets/vipm_full_name ]; then export VIPM_FULL_NAME="$(cat /run/secrets/vipm_full_name)"; fi; \
      if [ -f /run/secrets/vipm_email ]; then export VIPM_EMAIL="$(cat /run/secrets/vipm_email)"; fi; \
      export VIPC_DIR=/opt/lvci/vipc; \
      export LABVIEW_VERSION="${LABVIEW_VERSION}"; \
      /opt/lvci/vipm/install-vipc-linux.sh

# VI Browser render engine, baked in so 2.0 screenshots render inside this
# worker with no separate image. The render workflow invokes it explicitly with
# --entrypoint /usr/local/bin/toimages-entrypoint.sh, so the worker's DEFAULT
# behavior (used by every other CI activity) is unchanged. xvfb is installed
# with the VIPM layer above.
COPY --from=toimages-builder /out/lvctl /app/lvctl
COPY --from=toimages-builder /out/runner /app/runner
COPY .github/labview/toimages/labview.conf /app/labview.conf
COPY .github/labview/toimages/docker-entrypoint.sh /usr/local/bin/toimages-entrypoint.sh
RUN chmod +x /usr/local/bin/toimages-entrypoint.sh /app/lvctl /app/runner

ENV CI_WORKER_VERSION=${CI_WORKER_VERSION}
LABEL com.cotc.ci-worker.version=${CI_WORKER_VERSION} \
      com.cotc.ci-worker.platform=linux
