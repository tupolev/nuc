FROM ubuntu:24.04 AS build

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    git \
    glslc \
    libvulkan-dev \
    nodejs \
    npm \
    spirv-headers \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g pnpm@10.15.1

WORKDIR /src
RUN git clone --depth 1 --recurse-submodules https://github.com/leejet/stable-diffusion.cpp.git .

RUN cmake -S . -B build \
    -DSD_VULKAN=ON \
    -DSD_BUILD_SHARED_LIBS=ON \
    -DGGML_NATIVE=OFF \
    -DSD_BUILD_SHARED_GGML_LIB=ON \
    -DGGML_BACKEND_DL=ON \
    -DGGML_CPU_ALL_VARIANTS=ON \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    -DCMAKE_INSTALL_RPATH='$ORIGIN' \
    && cmake --build build --config Release -j"$(nproc)"

FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    libegl1 \
    libgl1 \
    libglvnd0 \
    libgles2 \
    libgomp1 \
    libvulkan1 \
    mesa-vulkan-drivers \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/build/bin /opt/stable-diffusion/bin

ENTRYPOINT ["/opt/stable-diffusion/bin/sd-server"]
