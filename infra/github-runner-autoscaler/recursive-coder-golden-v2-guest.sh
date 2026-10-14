#!/usr/bin/env bash
# Runs ONLY inside the restricted, credential-free golden-image builder VM.
# This is a second immutable layer on top of the digest-pinned v1 golden.
set -euo pipefail
umask 077
proxy=http://10.0.2.100:3128
export http_proxy="$proxy" https_proxy="$proxy" HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy"
base=/opt/recursive-coder-kvm
gradle_sha=dce76f55f8e251a3a1f130eb120f30b3d271de2b76c9b0729d316b5a1b6dc01f
node_sha=680d3f30b24a7ff24b98db5e96f294c0070f8f9078df658da1bce1b9c9873c88
source_sha=3028a05e602a9572ba41bc1f3daa28d0fdb762c0
stage() { echo "$1=$(cut -d' ' -f1 /proc/uptime)" >>/dev/ttyS0; }
# Cloud-init scripts_user does not power off on a nonzero exit. Always
# terminate a failed disposable builder guest, so the host can preserve its
# serial failure evidence instead of hitting the 20-minute lifetime limit.
fail_and_poweroff() {
  code="$?"
  trap - ERR
  printf 'BUILDER_V2_FAILED_EXIT=%s\n' "$code" >>/dev/ttyS0 || true
  systemctl poweroff --force || poweroff -f || true
  exit "$code"
}
trap fail_and_poweroff ERR
stage BUILDER_BOOTSTRAP_START
cat >/etc/apt/apt.conf.d/90builder-proxy <<'APT'
Acquire::http::Proxy "http://10.0.2.100:3128/";
Acquire::https::Proxy "http://10.0.2.100:3128/";
APT

stage BUILDER_APT_START
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3.12-venv unzip xz-utils procps
stage BUILDER_APT_END
test -f "$base/prepared.manifest"
grep -Fxq 'schema=1 runner=2.338.0 sha256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32' "$base/prepared.manifest"
test "$(java -version 2>&1 | head -n 1 | grep -c '21')" -eq 1
test "$(python3.12 -c 'import sys; print(sys.version_info.major * 100 + sys.version_info.minor)')" = 312

# All external executable archives are pinned and verified before extraction.
stage BUILDER_RUNNER_INSTALL_START
curl -fsSL --retry 3 https://services.gradle.org/distributions/gradle-9.8.1-bin.zip -o /tmp/gradle.zip
echo "$gradle_sha  /tmp/gradle.zip" | sha256sum -c -
unzip -q /tmp/gradle.zip -d "$base"
curl -fsSL --retry 3 https://nodejs.org/dist/v22.21.1/node-v22.21.1-linux-x64.tar.xz -o /tmp/node.tar.xz
echo "$node_sha  /tmp/node.tar.xz" | sha256sum -c -
tar -xJf /tmp/node.tar.xz -C "$base"
python3.12 -m venv "$base/python"
"$base/python/bin/python" -m pip install --no-cache-dir 'jsonschema==4.25.1'
test "$("$base/node-v22.21.1-linux-x64/bin/node" --version)" = v22.21.1
"$base/gradle-9.8.1/bin/gradle" --version | grep -Fq 'Gradle 9.8.1'
"$base/python/bin/python" -c 'import jsonschema; from importlib.metadata import version; assert version("jsonschema") == "4.25.1"'
rm -f /tmp/gradle.zip /tmp/node.tar.xz
stage BUILDER_RUNNER_INSTALL_END

# All JVM tiers share the 'test' source set. The real tasks are testClasses,
# unitTest, integrationTest and e2eTest; there is no integrationTestClasses or
# e2eTestClasses. Compile testClasses without executing suites and prebuild the
# CLI distribution; this warms Gradle/Java/Kotlin artifacts for all three tiers.
# Resolve and compile trusted, pinned public source only. Never bake the
# checkout, a work directory, registration tokens or Git identity into image.
useradd --create-home --uid 1001 --shell /bin/bash runner
install -d -o runner -g runner -m 0755 "$base/gradle-home"
install -d -o runner -g runner -m 0755 /tmp/recursive-coder-warm
stage BUILDER_V2_CACHE_START
runuser -u runner -- git -c http.proxy="$proxy" -c credential.helper= clone \
  --branch dev --single-branch https://github.com/YuriiSokolenko/recursive-coder.git \
  /tmp/recursive-coder-warm/src
test "$(runuser -u runner -- git -C /tmp/recursive-coder-warm/src rev-parse "$source_sha^{commit}")" = "$source_sha"
runuser -u runner -- git -C /tmp/recursive-coder-warm/src checkout --detach "$source_sha"
runuser -u runner -- env \
  PATH="$base/gradle-9.8.1/bin:$base/node-v22.21.1-linux-x64/bin:/usr/local/bin:/usr/bin:/bin" \
  GRADLE_USER_HOME="$base/gradle-home" \
  JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
  JAVA_TOOL_OPTIONS='-Dhttp.proxyHost=10.0.2.100 -Dhttp.proxyPort=3128 -Dhttps.proxyHost=10.0.2.100 -Dhttps.proxyPort=3128 -Dhttp.nonProxyHosts=localhost|127.*' \
  http_proxy="$proxy" https_proxy="$proxy" HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" \
  bash -c 'cd /tmp/recursive-coder-warm/src &&
    ./gradlew --no-daemon --max-workers=1 -Dorg.gradle.jvmargs=-Xmx1g \
      testClasses :app:cli:installDist --stacktrace'
stage BUILDER_V2_CACHE_END
# Repeat the sandbox probe on the v2 layer; never mark it successful without executing it.
sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
runuser -u runner -- /usr/bin/bwrap --die-with-parent --new-session --unshare-all \
  --tmpfs / --proc /proc --dev /dev --ro-bind /usr /usr --ro-bind /bin /bin \
  --ro-bind /lib /lib --ro-bind /lib64 /lib64 -- /usr/bin/true
echo BUILDER_BWRAP_PROBE=PASS >>/dev/ttyS0
# Gradle --no-daemon starts a disposable Gradle process, but Kotlin compiler
# daemons may outlive it. Stop *only* this guest's temporary runner UID before
# deleting the warm source tree and removing the account.
if pgrep -u runner >/dev/null; then
  echo BUILDER_V2_STOPPING_COMPILER_DAEMONS=START >>/dev/ttyS0
  pkill -TERM -u runner || true
  for ((attempt=0; attempt<40; attempt++)); do
    pgrep -u runner >/dev/null || break
    sleep 0.25
  done
  if pgrep -u runner >/dev/null; then
    pkill -KILL -u runner || true
    for ((attempt=0; attempt<20; attempt++)); do
      pgrep -u runner >/dev/null || break
      sleep 0.25
    done
  fi
  if pgrep -u runner >/dev/null; then
    echo BUILDER_V2_STOPPING_COMPILER_DAEMONS=FAIL >>/dev/ttyS0
    exit 1
  fi
  echo BUILDER_V2_STOPPING_COMPILER_DAEMONS=PASS >>/dev/ttyS0
fi
# Only artifact/dependency caches remain; discard source, compiler outputs,
# Gradle daemon locks, runner home, git config, logs and identities.
rm -rf /tmp/recursive-coder-warm /home/runner
find "$base/gradle-home" -type f -name '*.lock' -delete
# The builder runs under umask 077. Export root-owned toolchain trees with
# world-readable/traversable paths, while Gradle caches remain runner-owned.
# Otherwise ephemeral Actions processes see EACCES under python/bin and the
# toolchain verification job exits without executing any tests.
chmod -R a+rX "$base/python" "$base/gradle-9.8.1" "$base/node-v22.21.1-linux-x64"
userdel runner
cat >"$base/toolchain-v2.manifest" <<MANIFEST
schema=2 gradle=9.8.1 gradle_sha256=$gradle_sha node=22.21.1 node_sha256=$node_sha python=3.12 jsonschema=4.25.1 source=$source_sha
MANIFEST
chmod 0444 "$base/toolchain-v2.manifest"
sha256sum "$base/toolchain-v2.manifest" | awk '{print "BUILDER_V2_MANIFEST_SHA256="$1}' >>/dev/ttyS0
echo BUILDER_V2_TOOLCHAIN=PASS >>/dev/ttyS0

# A clone/virtualenv is never allowed to carry a secret into the golden image.
dpkg-query -W -f='${binary:Package}=${Version}\n' | sort >/var/lib/dpkg-package-manifest.txt
chmod 0444 /var/lib/dpkg-package-manifest.txt
sha256sum /var/lib/dpkg-package-manifest.txt | awk '{print "BUILDER_PACKAGES_SHA256="$1}' >>/dev/ttyS0
rm -f /etc/apt/apt.conf.d/90builder-proxy /etc/ssh/ssh_host_* /var/lib/dbus/machine-id
rm -f /var/lib/kvm-image-builder.sh
cloud-init clean --logs --machine-id --seed
rm -rf /var/lib/cloud /var/log/cloud-init.log /var/log/cloud-init-output.log
truncate -s 0 /etc/machine-id
sync
echo BUILDER_IMAGE_READY=PASS >>/dev/ttyS0
systemctl poweroff --force
