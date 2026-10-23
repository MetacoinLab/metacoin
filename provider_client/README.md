# metacoin-provider

Portable provider client for the MetaCoin native economy (order 2026-10-23 group H). Standard library only; it never imports
the service, never reads its database and never holds a private key: payments are signed by the operator-controlled signer.

    pip install ./provider_client
    metacoin-provider --help

Flows: enrol through the signer (operator step with an administrator credential), publish resource offers, activate and deliver
reservations (compute executed locally by an operator-chosen executor and reported as a provider assertion; the service verifies
the product against the public reference), submit work offers, settle, pay with a signer-held key, fetch receipts and verify
them offline under explicitly pinned roots. `metacoin-provider conformance` runs the versioned fixtures (`conformance/v1`)
that pin this implementation's wire format and verifier to the service's.
