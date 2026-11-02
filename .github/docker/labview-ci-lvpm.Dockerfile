# syntax=docker/dockerfile:1.7
ARG LV_BASE_IMAGE=nationalinstruments/labview:2026q1patch2-linux

FROM rust:1.92.0-alpine AS lvpm-builder
RUN apk add --no-cache git musl-dev build-base cmake perl
WORKDIR /src/lvpm
RUN git init && git remote add origin https://github.com/Flydroid/lvpm.git \
 && git fetch --depth 1 origin 8e8bd7e390b7d4a77558af12cfa52f79079df409 \
 && git checkout --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = 8e8bd7e390b7d4a77558af12cfa52f79079df409 \
 && cargo build --release --locked --package lvpm \
 && target/release/lvpm --version

FROM golang:1.26.3-alpine AS toimages-builder
RUN apk add --no-cache git zip
WORKDIR /src
COPY .github/labview/toimages/_ni/labview/lvctl ./_ni/labview/lvctl
COPY .github/labview/toimages/_ni/shared/go/labview ./_ni/shared/go/labview
RUN cd _ni/labview/lvctl/vis && rm -f toimages.zip \
 && zip -rq toimages.zip toimages -x "*.DS_Store"
RUN cd _ni/labview/lvctl && CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/lvctl .
COPY .github/labview/toimages/go.mod ./
COPY .github/labview/toimages/main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/runner .

FROM ${LV_BASE_IMAGE}
ARG CI_WORKER_VERSION=dev
ARG LABVIEW_VERSION=2026
ENV LVPM_CONFIG_CACHE=/opt/lvci/lvpm/cache \
    LV_RTE_HEADLESS=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 python3-yaml python3-tomli unzip xvfb \
    fonts-dejavu-core fonts-liberation fonts-noto-core xfonts-base xfonts-75dpi xfonts-100dpi fontconfig \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*
COPY --from=lvpm-builder /src/lvpm/target/release/lvpm /usr/local/bin/lvpm
COPY --from=lvpm-builder /src/lvpm/LICENSE-MIT /src/lvpm/LICENSE-APACHE /usr/share/licenses/lvpm/
COPY .github/labview/lvpm/adapter.py /opt/lvci/lvpm/adapter.py
COPY ci-out/lvpm-plan/ /opt/lvci/lvpm/plan/
RUN python3 /opt/lvci/lvpm/adapter.py install \
    --plan /opt/lvci/lvpm/plan/requirements.json --out /opt/lvci/lvpm
COPY --from=toimages-builder /out/lvctl /app/lvctl
COPY --from=toimages-builder /out/runner /app/runner
COPY .github/labview/toimages/labview.conf /app/labview.conf
COPY .github/labview/toimages/docker-entrypoint.sh /usr/local/bin/toimages-entrypoint.sh
RUN chmod +x /usr/local/bin/toimages-entrypoint.sh /app/lvctl /app/runner
ENV CI_WORKER_VERSION=${CI_WORKER_VERSION}
LABEL com.cotc.ci-worker.version=${CI_WORKER_VERSION} \
      com.cotc.ci-worker.platform=linux \
      com.cotc.ci-worker.installer=lvpm \
      com.cotc.ci-worker.lvpm-revision=8e8bd7e390b7d4a77558af12cfa52f79079df409
