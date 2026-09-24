"""Service configuration: one private home directory, loopback by default, fail closed."""
from dataclasses import dataclass, field
import os
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state')) / 'metacoin-service'
PROVIDER_MODES = ('simulation', 'test-http', 'production')
LIMITS = {
    'max_body_bytes': 256 * 1024, 'max_json_depth': 12, 'max_json_nodes': 5000, 'max_segments': 128,
    'max_candidates': 16, 'max_total_segments': 512, 'max_label_chars': 128, 'page_size': 50,
    'max_upload_bytes': 2 * 1024 * 1024, 'job_timeout_seconds': 30, 'job_max_retries': 2,
    'job_lease_seconds': 60, 'job_output_bytes': 1024 * 1024, 'worker_cpu_seconds': 20,
    'worker_address_space_bytes': 1024 * 1024 * 1024, 'max_queued_per_workspace': 200,
    'session_seconds': 8 * 3600, 'credential_seconds': 90 * 24 * 3600, 'max_envelope_bytes': 64 * 1024,
    'facilitator_timeout_seconds': 10, 'batch_max_items': 20,
}


@dataclass
class Settings:
    home: Path = DEFAULT_HOME
    host: str = '127.0.0.1'
    port: int = 8402
    dev_http_loopback: bool = True         # cookies without Secure ONLY on loopback over plain HTTP
    provider_mode: str = 'simulation'
    campaign_cap: int = 10
    # production provider (all required when provider_mode == 'production'; otherwise ignored)
    facilitator_url: str = ''
    x402_network: str = ''
    x402_asset: str = ''
    x402_pay_to: str = ''
    facilitator_credential_file: str = ''
    # production BUYER (agent pays a remote resource); all required for that adapter, never for anything else
    buyer_resource_url: str = ''
    buyer_key_file: str = ''
    buyer_network: str = ''            # CAIP-2, e.g. eip155:84532
    buyer_asset: str = ''              # token contract address
    buyer_asset_token: str = 'usdc-test-identifier'   # the contract's asset token that maps to buyer_asset
    buyer_max_amount: int = 0
    buyer_pay_to: str = ''             # optional pinned recipient
    limits: dict = field(default_factory=lambda: dict(LIMITS))

    @classmethod
    def from_env(cls, **overrides):
        env = os.environ
        s = cls(home=Path(env.get('METACOIN_SERVICE_HOME', DEFAULT_HOME)),
                host=env.get('METACOIN_SERVICE_HOST', '127.0.0.1'),
                port=int(env.get('METACOIN_SERVICE_PORT', '8402')),
                dev_http_loopback=env.get('METACOIN_SERVICE_DEV_HTTP', '1') == '1',
                provider_mode=env.get('METACOIN_PROVIDER_MODE', 'simulation'),
                campaign_cap=int(env.get('METACOIN_CAMPAIGN_CAP', '10')),
                facilitator_url=env.get('METACOIN_FACILITATOR_URL', ''),
                x402_network=env.get('METACOIN_X402_NETWORK', ''),
                x402_asset=env.get('METACOIN_X402_ASSET', ''),
                x402_pay_to=env.get('METACOIN_X402_PAY_TO', ''),
                facilitator_credential_file=env.get('METACOIN_FACILITATOR_CREDENTIAL_FILE', ''),
                buyer_resource_url=env.get('METACOIN_BUYER_RESOURCE_URL', ''), buyer_key_file=env.get('METACOIN_BUYER_KEY_FILE', ''),
                buyer_network=env.get('METACOIN_BUYER_NETWORK', ''), buyer_asset=env.get('METACOIN_BUYER_ASSET', ''),
                buyer_asset_token=env.get('METACOIN_BUYER_ASSET_TOKEN', 'usdc-test-identifier'),
                buyer_max_amount=int(env.get('METACOIN_BUYER_MAX_AMOUNT', '0') or 0), buyer_pay_to=env.get('METACOIN_BUYER_PAY_TO', ''))
        for key, value in overrides.items():
            setattr(s, key, value)
        # Operator override of bounded limits (integers only), e.g. METACOIN_LIMITS_JSON='{"job_timeout_seconds": 5}'
        if env.get('METACOIN_LIMITS_JSON'):
            import json
            for key, value in json.loads(env['METACOIN_LIMITS_JSON']).items():
                if key not in s.limits or type(value) not in (int, float) or value <= 0:
                    raise ValueError('invalid limits override: ' + str(key))
                s.limits[key] = value
        s.validate()
        return s

    def validate(self):
        if self.provider_mode not in PROVIDER_MODES:
            raise ValueError('unknown provider mode')
        if self.host not in ('127.0.0.1', '::1', 'localhost') and self.dev_http_loopback:
            raise ValueError('plain-HTTP development mode is limited to loopback; remote deployment requires TLS')
        if self.provider_mode == 'production':
            missing = [k for k in ('facilitator_url', 'x402_network', 'x402_asset', 'x402_pay_to', 'facilitator_credential_file')
                       if not getattr(self, k)]
            if missing:
                raise ValueError('production provider configuration incomplete: ' + ', '.join(missing))
            if not self.facilitator_url.startswith('https://'):
                raise ValueError('production facilitator URL must use https')

    # derived paths
    @property
    def db_path(self): return self.home / 'service.sqlite'
    @property
    def journal_path(self): return self.home / 'journal.sqlite'
    @property
    def keys_dir(self): return self.home / 'keys'
    @property
    def artifacts_dir(self): return self.home / 'artifacts'
    @property
    def credentials_dir(self): return self.home / 'credentials'
    @property
    def logs_dir(self): return self.home / 'logs'
    @property
    def run_dir(self): return self.home / 'run'
