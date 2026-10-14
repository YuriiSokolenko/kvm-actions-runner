import fs from 'node:fs';

const secretKey = /(^|[_-])(access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key|authorization|password|credential|cookie|set-cookie|gh[_-]?token|github[_-]?token|token|secret|private[_-]?key)([_-]|$)/i;

export function sanitizeDiagnostic(value, key = '') {
  if (secretKey.test(key)) return '[REDACTED]';
  if (Array.isArray(value)) return value.map(item => sanitizeDiagnostic(item));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, sanitizeDiagnostic(item, name)]));
  }
  if (typeof value !== 'string') return value;
  let sanitized = value
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [REDACTED]')
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----/g, '[REDACTED PRIVATE KEY]')
    .replace(/::add-mask::[^\r\n]*/gi, '::add-mask::[REDACTED]')
    .replace(/\b([A-Za-z_][A-Za-z0-9_-]*)(["']?)\s*([=:])\s*(?:"[^"]*"|'[^']*'|[^\s&,;]+)/g, (match, name, quote, separator) =>
      secretKey.test(name) ? `${name}${quote}${separator}[REDACTED]` : match)
    .replace(/\b(gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,})\b/g, '[REDACTED]');
  const knownSecrets = Object.entries(process.env)
    .filter(([name, secret]) => secretKey.test(name) && typeof secret === 'string' && secret.length > 0)
    .map(([, secret]) => secret)
    .sort((left, right) => right.length - left.length);
  for (const secret of knownSecrets) sanitized = sanitized.replaceAll(secret, '[REDACTED]');
  return sanitized;
}

/** Best-effort append; diagnostics must never change the runtime result. */
export function appendDiagnostic(file, record) {
  if (!file) return false;
  try {
    fs.appendFileSync(file, `${JSON.stringify(sanitizeDiagnostic(record))}\n`, { mode: 0o600 });
    return true;
  } catch {
    return false;
  }
}
