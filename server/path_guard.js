// TMP_NAMESPACE_GUARD (0.2.9): the service runs under systemd PrivateTmp=yes,
// so a path under /tmp, /var/tmp, or /dev/shm written by the service lands in
// a private mount namespace that is invisible to the Hermes agent (and a file
// the agent drops there is unreadable by the service). Export/import against
// those paths "succeeds" yet the agent sees a stale/empty file — reject them
// with an actionable error instead of letting the namespace silently split the
// write from the read.

const TMP_NAMESPACE_ROOTS = ["/tmp", "/var/tmp", "/dev/shm", "tmp", "var/tmp", "dev/shm"];

export function isTmpNamespacePath(raw) {
  const p = (typeof raw === "string" ? raw : "").replace(/\\/g, "/");
  return TMP_NAMESPACE_ROOTS.some((root) => p === root || p.startsWith(root + "/"));
}

export function assertSafeFilePath(name, args) {
  if (name !== "lorekeeper_export" && name !== "lorekeeper_import") return;
  const raw = typeof args?.path === "string" ? args.path : "";
  if (!raw) return;
  if (isTmpNamespacePath(raw)) {
    throw new Error(
      `${name} refused path "${raw}": it is under a systemd private-tmp ` +
      `namespace (/tmp, /var/tmp, /dev/shm \u2014 the service runs with ` +
      `PrivateTmp=yes), so the file would be invisible to your process. ` +
      `Use a stable workspace path instead (e.g. /root/.hermes/workspace/...).`
    );
  }
}