import { DEFAULT_REDACT_PARAMS } from '../redact.js';

const DEFAULTS = {
  endpoint: '/ingest',
  token: null,

  remoteConfig: true,
  configUrl: '/config',
  remoteConfigTtlMs: 60 * 60 * 1000,

  flushIntervalMs: 5000,
  maxBatchSize: 50,
  maxBatchBytes: 900 * 1024,
  idleThresholdMs: 5 * 60 * 1000,
  checkoutEveryNms: 5 * 60 * 1000,
  sessionIdleTimeoutMs: 30 * 60 * 1000,
  sessionMaxLengthMs: 24 * 60 * 60 * 1000,

  sampleRate: 1,
  triggers: null,
  minDurationMs: 0,
  maxBufferBytes: 2 * 1024 * 1024,

  maskTextSelector: null,
  blockSelector: null,
  redactUrlParams: DEFAULT_REDACT_PARAMS,

  requireConsent: false,
  respectGlobalPrivacyControl: true,
};

export let config = Object.assign({}, DEFAULTS, window.CAPTURE_CONFIG || {});

export const REMOTE_CONFIG_KEY = 'capture_remote_config';

export async function resolveConfig() {
  const inline = window.CAPTURE_CONFIG || {};
  if (config.remoteConfig === false) {
    config = Object.assign({}, DEFAULTS, inline);
    return;
  }

  let remote = null;
  try {
    const cached = JSON.parse(localStorage.getItem(REMOTE_CONFIG_KEY));
    if (cached && Date.now() - cached.fetchedAt < config.remoteConfigTtlMs) {
      remote = cached.config;
    }
  } catch {
  }
  if (!remote) {
    try {
      const configUrl = config.token
        ? `${config.configUrl}${config.configUrl.includes('?') ? '&' : '?'}token=${encodeURIComponent(config.token)}`
        : config.configUrl;
      const res = await fetch(configUrl);
      if (res.ok) {
        remote = await res.json();
        localStorage.setItem(
          REMOTE_CONFIG_KEY,
          JSON.stringify({ config: remote, fetchedAt: Date.now() })
        );
      }
    } catch {
    }
  }
  config = Object.assign({}, DEFAULTS, remote || {}, inline);
}
