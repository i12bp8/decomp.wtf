# The compiler-only image: a bare Linux that runs the games' x86 compilers, nothing of decomp.wtf itself.
# decomp.wtf on macOS builds it from this file on the first Linux-only toolchain install (linuxbox.prepare) and runs
# one long-lived container from it; the toolchain folder is mounted read-only and the compile folders read-write.
#
#   docker build --platform linux/amd64 -t decomp-wtf-compiler - < linuxbox.Dockerfile
#
# The compilers are x86 Linux programs: on Apple Silicon this runs as linux/amd64 (Rosetta or QEMU).
FROM --platform=linux/amd64 debian:trixie-slim
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      ca-certificates coreutils libc6-i386 lib32stdc++6 lib32z1 \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 -s /bin/sh decomp
USER decomp
WORKDIR /tmp
ENV LANG=C.UTF-8
CMD ["sleep", "infinity"]
