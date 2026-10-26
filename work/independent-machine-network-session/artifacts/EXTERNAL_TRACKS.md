# External validation tracks — reported separately, never counted as passed

| track | status | what exists | what is missing |
|---|---|---|---|
| Real second host, separate operator | **blocked** | `docs/native/TWO_HOST.md`; `python -m metacoin_service.native.hostconfig generate/check` (tested statically: TLS beyond loopback, distinct ports, no key material; sample in `examples/hostconfig-sample/`) | no second host is authorized for MetaCoin and reachable from this session; no network scanning was performed; nothing was inferred from IP addresses |
| Separate OS identities for service / signers / witness | **not performed** | process separation on one host under one user (`isolation_mode: process-separated`) | creating privileged accounts or changing host security is outside this order |
| BPv7 (RFC 9171) transport | **blocked** | transport-agnostic envelope/bundle layer; application-level relay service (group F) with explicit "not BPv7" labels; `bpv7-probe.json` | no installable daemon (dtnd / dtn7 / µD3TN / ION) in this environment; no cargo toolchain |

Local completion of the ten required groups and the 50 journeys does not imply independent multi-host or BPv7 deployment.
